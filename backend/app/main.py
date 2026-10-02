"""Read-only local result display, profile API and server-sent events."""

import asyncio
import base64
import json
import os
import secrets
import threading
import time
from typing import Annotated
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field

from .domain import RangeState, iso
from .profiles import Profile, ProfileStore
from .sponsors import IMAGE_POLICY, MAX_REQUEST_BYTES, TYPES, LogoStore, SponsorStore
from .storage import connect_reader, read_snapshot


class SponsorText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=300)


class Runtime:
    def __init__(self, path):
        self.id = secrets.token_hex(16)
        self.profiles = ProfileStore(path)
        self.sponsors = SponsorStore(self.profiles.path.parent / "sponsors")
        self.logo = LogoStore(self.profiles.path.parent / "logo.json")
        self.ranges = RangeState()
        self.status = {source: {"state": "starting", "message": "Verbindung wird hergestellt", "last_check_at": None}
                       for source in ("db", "sdf", "lana")}
        self.listeners = set()
        self.stop = threading.Event()
        self.revision = 0
        self.status["worker"] = {"state": "starting", "message": "Warte auf Ergebnis-Worker", "last_check_at": None}
        self.status["local_db"] = {"state": "starting", "message": "Lokale Datenbank wird geöffnet", "last_check_at": None}

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
                "rows": [[{"lane": lane, "target": self.ranges.public(lane, profile),
                           "live_shooter": self.ranges.live_shooters.get(lane),
                           "occupancy": self.ranges.occupancy.get(lane, "unknown") if occupancy_confirmed else "unknown"}
                          for lane in row] for row in profile.rows],
                "sources": self.status, "sponsors": self.sponsors.images, "logo": self.logo.metadata, "server_time": iso(time.time())}


def create_app(profile_path=None, *, result_path=None):
    @asynccontextmanager
    async def lifespan(app):
        runtime = Runtime(profile_path or os.getenv("PROFILE_PATH", "/data/profiles.json"))
        app.state.runtime = runtime
        loop = asyncio.get_running_loop()
        path = result_path or os.getenv("RESULT_DB_PATH", "/results/results.sqlite3")
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
        try:
            yield
        finally:
            runtime.stop.set()
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
            try:
                while not await request.is_disconnected():
                    listener.clear()
                    if not runtime.profiles.get(profile):
                        yield 'event: deleted\ndata: {}\n\n'
                        return
                    payload = json.dumps(runtime.snapshot(profile, lane=lane), ensure_ascii=False, separators=(",", ":"))
                    yield f"event: snapshot\ndata: {payload}\n\n"
                    try:
                        await asyncio.wait_for(listener.wait(), 10)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                runtime.listeners.discard(listener)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/admin/auth", dependencies=[Depends(admin)])
    def authenticate():
        return Response(status_code=204)

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
