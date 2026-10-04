"""Read-only local result display, profile API and server-sent events."""

import asyncio
import base64
import json
import os
import secrets
import threading
import time
from pathlib import Path as FilePath
from typing import Annotated
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field

from .disciplines import DisciplineMappings, DisciplineStore, public_catalog
from .domain import RangeState, iso
from .profiles import Profile, ProfileStore
from .resource_saver import (PRESENCE_HEARTBEAT_SECONDS, ResourceSaverSettings,
                             ResourceSaverStore, atomic_json, read_json)
from .sponsors import IMAGE_POLICY, MAX_REQUEST_BYTES, TYPES, LogoStore, SponsorStore
from .storage import connect_reader, read_snapshot


class SponsorText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=300)


class Runtime:
    def __init__(self, path, result_path=None):
        self.id = secrets.token_hex(16)
        self.profiles = ProfileStore(path)
        self.disciplines = DisciplineStore(self.profiles.path.parent / "disciplines.json")
        data_directory = self.profiles.path.parent
        result_directory = FilePath(result_path).parent if result_path else data_directory
        self.resource_saver = ResourceSaverStore(os.getenv("SETTINGS_PATH", str(data_directory / "settings.json")))
        self.viewer_presence_path = FilePath(os.getenv("VIEWER_PRESENCE_PATH", str(data_directory / "viewers.json")))
        self.resource_saver_status_path = FilePath(os.getenv(
            "RESOURCE_SAVER_STATUS_PATH", str(result_directory / "resource-saver-status.json")))
        self.sponsors = SponsorStore(self.profiles.path.parent / "sponsors")
        self.logo = LogoStore(self.profiles.path.parent / "logo.json")
        self.ranges = RangeState()
        self.status = {source: {"state": "starting", "message": "Verbindung wird hergestellt", "last_check_at": None}
                       for source in ("db", "sdf", "lana")}
        self.listeners = set()
        self.last_viewer_seen_at = time.time()
        self._published_viewer_count = 0
        self.stop = threading.Event()
        self.revision = 0
        self.status["worker"] = {"state": "starting", "message": "Warte auf Ergebnis-Worker", "last_check_at": None}
        self.status["local_db"] = {"state": "starting", "message": "Lokale Datenbank wird geöffnet", "last_check_at": None}

    def write_viewer_presence(self, viewer_count=None):
        count = len(self.listeners) if viewer_count is None else viewer_count
        now = time.time()
        if count > 0 or self._published_viewer_count > 0:
            self.last_viewer_seen_at = now
        self._published_viewer_count = count
        try:
            atomic_json(self.viewer_presence_path, {
                "runtime_id": self.id,
                "viewer_count": count,
                "last_viewer_seen_at": self.last_viewer_seen_at,
                "updated_at": now,
            })
        except OSError:
            pass  # Presence reporting must not interrupt a display stream.

    def resource_saver_response(self):
        try:
            status = read_json(self.resource_saver_status_path)
        except (OSError, ValueError, json.JSONDecodeError):
            status = None
        return {"settings": self.resource_saver.settings.model_dump(), "status": status}

    def notify(self):
        self.revision += 1
        # ponytail: snapshots coalesce slow viewers; add a replay log only if replay is required.
        for listener in self.listeners:
            listener.set()

    def load(self, state, sources, meta):
        self.ranges = state
        self.status = sources | {
            "local_db": {"state": "connected", "message": "Lokale Ergebnisdatenbank verbunden", "last_check_at": iso(time.time())},
            "worker": {"state": "connected" if time.time() - meta["heartbeat"] <= 15 else "degraded",
                       "message": "Ergebnis-Worker aktiv" if time.time() - meta["heartbeat"] <= 15 else "Ergebnis-Worker ohne aktuelles Lebenszeichen",
                       "last_check_at": iso(meta["heartbeat"]) if meta["heartbeat"] else None},
        }
        self.notify()

    def local_failure(self):
        if self.status["local_db"]["state"] != "disconnected":
            self.status["local_db"] = {**self.status["local_db"], "state": "disconnected", "message": "Lokale Datenbank nicht lesbar · Ergebnisse bleiben erhalten"}
            self.status["worker"] = {**self.status["worker"], "state": "degraded", "message": "Ergebnis-Worker nicht bestätigt"}
            self.notify()

    def snapshot(self, profile_id=None, *, profile=None, lane=None):
        profile = profile or self.profiles.get(profile_id)
        if not profile:
            raise HTTPException(404, "Profil nicht gefunden")
        if lane is not None:
            profile = profile.model_copy(update={"rows": [[lane]]})
        occupancy_confirmed = all(self.status.get(source, {}).get("state") == "connected" for source in ("worker", "local_db")) and any(
            self.status.get(source, {}).get("state") == "connected" for source in ("lana", "demo"))
        return {"runtime_id": self.id, "profile": profile.model_dump(), "revision": self.revision,
                "rule_catalog_revision": public_catalog()["revision"],
                "rows": [[{"lane": lane, "target": self.ranges.public(lane, profile, self.disciplines.rules),
                           "live_shooter": self.ranges.live_shooters.get(lane),
                           "occupancy": self.ranges.occupancy.get(lane, "unknown") if occupancy_confirmed else "unknown"}
                          for lane in row] for row in profile.rows],
                "sources": self.status, "sponsors": self.sponsors.images, "logo": self.logo.metadata, "server_time": iso(time.time())}


def create_app(profile_path=None, *, result_path=None):
    @asynccontextmanager
    async def lifespan(app):
        path = result_path or os.getenv("RESULT_DB_PATH", "/results/results.sqlite3")
        runtime = Runtime(profile_path or os.getenv("PROFILE_PATH", "/data/profiles.json"), path)
        app.state.runtime = runtime
        loop = asyncio.get_running_loop()
        # One watcher for all viewers; the cache contains only committed SQLite state.
        def watch():
            connection = None
            previous = None
            reload_at = 0
            while not runtime.stop.is_set():
                try:
                    if connection is None:
                        connection = connect_reader(path)
                    meta = connection.execute("SELECT revision,heartbeat FROM meta WHERE id=1").fetchone()
                    key = (meta["revision"], meta["heartbeat"], time.time() - meta["heartbeat"] <= 15)
                    if key != previous or time.monotonic() >= reload_at:
                        state, sources, meta = read_snapshot(connection)
                        loop.call_soon_threadsafe(runtime.load, state, sources, meta)
                        previous = key
                        reload_at = time.monotonic() + 60  # Expiry also applies while the writer is stopped.
                except Exception:
                    loop.call_soon_threadsafe(runtime.local_failure)
                    if connection:
                        connection.close()
                    connection = None
                    previous = None
                    runtime.stop.wait(1)
                runtime.stop.wait(0.05)
            if connection:
                connection.close()

        try:
            def initial():
                connection = connect_reader(path)
                try:
                    return read_snapshot(connection)
                finally:
                    connection.close()
            runtime.load(*await asyncio.to_thread(initial))
        except Exception:
            runtime.local_failure()
        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        async def presence_heartbeat():
            while True:
                runtime.write_viewer_presence()
                await asyncio.sleep(PRESENCE_HEARTBEAT_SECONDS)
        presence_task = asyncio.create_task(presence_heartbeat())
        try:
            yield
        finally:
            runtime.stop.set()
            presence_task.cancel()
            try:
                await presence_task
            except asyncio.CancelledError:
                pass
            runtime.write_viewer_presence(0)
            await asyncio.to_thread(thread.join, 2)

    app = FastAPI(title="Meyton Live Display", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic(realm="Meyton Profilverwaltung", auto_error=False)

    def admin(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)):
        password = os.getenv("SM_ADMIN_PASSWORD", "")
        if not password:
            raise HTTPException(503, "SM_ADMIN_PASSWORD ist nicht konfiguriert")
        valid_user = secrets.compare_digest((credentials.username if credentials else "").encode(), b"admin")
        valid_password = secrets.compare_digest((credentials.password if credentials else "").encode(), password.encode())
        if not (valid_user and valid_password):
            raise HTTPException(401, "Anmeldung erforderlich", headers={"WWW-Authenticate": 'Basic realm="Meyton Profilverwaltung", charset="UTF-8"'})
        if request.method not in ("GET", "HEAD"):
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "Fremder Ursprung")
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                raise HTTPException(403, "Fremder Ursprung")
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise HTTPException(415, "JSON erforderlich")

    @app.get("/healthz")
    def health(request: Request):
        return {"status": "ok", "sources": request.app.state.runtime.status}

    @app.get("/api/profiles")
    def profiles(request: Request):
        return [p.model_dump() for p in request.app.state.runtime.profiles.profiles]

    @app.get("/api/target-rules")
    def target_rules():
        return public_catalog()

    @app.get("/api/snapshot")
    async def snapshot(request: Request, profile: str = "alles"):
        return request.app.state.runtime.snapshot(profile)

    @app.get("/api/ranges/{lane}")
    async def single_range(request: Request, lane: Annotated[int, Path(ge=1, le=32767)], profile: str = "alles"):
        return request.app.state.runtime.snapshot(profile, lane=lane)

    @app.get("/api/events")
    async def events(request: Request, profile: str = "alles", lane: Annotated[int | None, Query(ge=1, le=32767)] = None):
        runtime = request.app.state.runtime
        runtime.snapshot(profile, lane=lane)  # Validate before sending response headers.
        async def stream():
            listener = asyncio.Event()
            runtime.listeners.add(listener)
            runtime.write_viewer_presence()
            previous = None
            loop = asyncio.get_running_loop()
            heartbeat_at = loop.time() + 10
            try:
                while not await request.is_disconnected():
                    listener.clear()
                    if not runtime.profiles.get(profile):
                        yield 'event: deleted\ndata: {}\n\n'
                        return
                    snapshot = runtime.snapshot(profile, lane=lane)
                    meaningful = {key: value for key, value in snapshot.items() if key not in ("revision", "server_time")}
                    meaningful["sources"] = {
                        name: {key: value for key, value in source.items() if key != "last_check_at"}
                        for name, source in snapshot["sources"].items()
                    }
                    current = json.dumps(meaningful, ensure_ascii=False, separators=(",", ":"))
                    if current != previous:
                        previous = current
                        payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                        heartbeat_at = loop.time() + 10
                        yield f"event: snapshot\ndata: {payload}\n\n"
                    try:
                        await asyncio.wait_for(listener.wait(), max(0, heartbeat_at - loop.time()))
                    except TimeoutError:
                        heartbeat_at = loop.time() + 10
                        yield ": heartbeat\n\n"
            finally:
                runtime.listeners.discard(listener)
                runtime.write_viewer_presence()
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/admin/auth", dependencies=[Depends(admin)])
    def authenticate():
        return Response(status_code=204)

    @app.get("/api/admin/settings/resource-saver", dependencies=[Depends(admin)])
    def resource_saver_settings(request: Request):
        return request.app.state.runtime.resource_saver_response()

    @app.put("/api/admin/settings/resource-saver", dependencies=[Depends(admin)])
    async def update_resource_saver(settings: ResourceSaverSettings, request: Request):
        runtime = request.app.state.runtime
        await asyncio.to_thread(runtime.resource_saver.save, settings)
        return runtime.resource_saver_response()

    @app.get("/api/logo")
    def logo_image(request: Request):
        image = request.app.state.runtime.logo.image
        if image is None:
            raise HTTPException(404, "Kein eigenes Logo")
        return Response(base64.b64decode(image["data"]), media_type=TYPES[image["type"]], headers={
            "Content-Security-Policy": IMAGE_POLICY, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    @app.get("/api/admin/logo", dependencies=[Depends(admin)])
    def logo_settings(request: Request):
        return request.app.state.runtime.logo.metadata

    async def image_payload(request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_REQUEST_BYTES:
                raise HTTPException(413, "Bild zu groß · maximal 5 MiB")
            data.extend(chunk)
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict) or set(payload) != {"name", "data"}:
                raise ValueError("Bildname und Bilddaten erforderlich")
            return payload
        except ValueError as error:
            raise HTTPException(422, str(error))

    @app.put("/api/admin/logo", dependencies=[Depends(admin)])
    async def upload_logo(request: Request):
        payload = await image_payload(request)
        runtime = request.app.state.runtime
        try:
            image = await asyncio.to_thread(runtime.logo.set, payload["name"], payload["data"])
        except ValueError as error:
            raise HTTPException(422, str(error))
        except OSError:
            raise HTTPException(503, "Logo konnte nicht gespeichert werden")
        runtime.notify()
        return image

    @app.delete("/api/admin/logo", dependencies=[Depends(admin)])
    async def delete_logo(request: Request):
        runtime = request.app.state.runtime
        try:
            await asyncio.to_thread(runtime.logo.delete)
        except OSError:
            raise HTTPException(503, "Logo konnte nicht entfernt werden")
        runtime.notify()
        return Response(status_code=204)

    @app.get("/api/sponsors/{image_id}")
    def sponsor_image(image_id: str, request: Request):
        try:
            path = request.app.state.runtime.sponsors.path(image_id)
        except FileNotFoundError:
            raise HTTPException(404, "Bild nicht gefunden")
        return FileResponse(path, media_type=TYPES[path.suffix[1:]], headers={
            "Content-Security-Policy": IMAGE_POLICY, "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/api/admin/sponsors", dependencies=[Depends(admin)])
    def sponsors(request: Request):
        return request.app.state.runtime.sponsors.images

    @app.post("/api/admin/sponsors", status_code=201, dependencies=[Depends(admin)])
    async def add_sponsor(request: Request):
        payload = await image_payload(request)
        runtime = request.app.state.runtime
        try:
            image = await asyncio.to_thread(runtime.sponsors.add, payload["name"], payload["data"])
        except ValueError as error:
            raise HTTPException(422, str(error))
        except OSError:
            raise HTTPException(503, "Bild konnte nicht gespeichert werden")
        runtime.notify()
        return image

    @app.put("/api/admin/sponsors/{image_id}", dependencies=[Depends(admin)])
    async def edit_sponsor_text(image_id: str, payload: SponsorText, request: Request):
        runtime = request.app.state.runtime
        try:
            image = await asyncio.to_thread(runtime.sponsors.set_text, image_id, payload.text)
        except FileNotFoundError:
            raise HTTPException(404, "Bild nicht gefunden")
        except OSError:
            raise HTTPException(503, "Text konnte nicht gespeichert werden")
        runtime.notify()
        return image

    @app.delete("/api/admin/sponsors/{image_id}", dependencies=[Depends(admin)])
    async def delete_sponsor(image_id: str, request: Request):
        runtime = request.app.state.runtime
        try:
            await asyncio.to_thread(runtime.sponsors.delete, image_id)
        except FileNotFoundError:
            raise HTTPException(404, "Bild nicht gefunden")
        except OSError:
            raise HTTPException(503, "Bild konnte nicht entfernt werden")
        runtime.notify()
        return Response(status_code=204)

    @app.get("/api/admin/profiles", dependencies=[Depends(admin)])
    def admin_profiles(request: Request):
        return profiles(request)

    @app.get("/api/admin/disciplines", dependencies=[Depends(admin)])
    def discipline_mappings(request: Request):
        return {**request.app.state.runtime.disciplines.configuration.model_dump(),
                "rules": public_catalog()["rules"]}

    @app.put("/api/admin/disciplines", dependencies=[Depends(admin)])
    async def save_discipline_mappings(configuration: DisciplineMappings, request: Request):
        runtime = request.app.state.runtime
        try:
            await asyncio.to_thread(runtime.disciplines.save, configuration)
        except OSError:
            raise HTTPException(503, "Disziplinzuordnung konnte nicht gespeichert werden")
        runtime.notify()
        return discipline_mappings(request)

    @app.post("/api/admin/profiles/preview", dependencies=[Depends(admin)])
    async def preview_profile(profile: Profile, request: Request):
        return request.app.state.runtime.snapshot(profile=profile)

    @app.post("/api/admin/profiles", status_code=201, dependencies=[Depends(admin)])
    async def add_profile(profile: Profile, request: Request):
        runtime = request.app.state.runtime
        if runtime.profiles.get(profile.id):
            raise HTTPException(409, "Profil-ID existiert bereits")
        if len(runtime.profiles.profiles) >= 64:
            raise HTTPException(409, "Maximal 64 Profile")
        runtime.profiles.save([*runtime.profiles.profiles, profile])
        runtime.notify()
        return profile.model_dump()

    @app.put("/api/admin/profiles/{profile_id}", dependencies=[Depends(admin)])
    async def edit_profile(profile_id: str, profile: Profile, request: Request):
        runtime = request.app.state.runtime
        if profile.id != profile_id:
            raise HTTPException(422, "Profil-ID kann nicht geändert werden")
        if not runtime.profiles.get(profile_id):
            raise HTTPException(404, "Profil nicht gefunden")
        runtime.profiles.save([profile if p.id == profile_id else p for p in runtime.profiles.profiles])
        runtime.notify()
        return profile.model_dump()

    @app.delete("/api/admin/profiles/{profile_id}", dependencies=[Depends(admin)])
    async def delete_profile(profile_id: str, request: Request):
        runtime = request.app.state.runtime
        if not runtime.profiles.get(profile_id):
            raise HTTPException(404, "Profil nicht gefunden")
        if len(runtime.profiles.profiles) == 1:
            raise HTTPException(409, "Mindestens ein Profil muss erhalten bleiben")
        runtime.profiles.save([p for p in runtime.profiles.profiles if p.id != profile_id])
        runtime.notify()
        return Response(status_code=204)

    return app


app = create_app()
