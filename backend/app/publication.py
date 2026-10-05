"""Optional outbound SFTP publication; no public write API or vendor access."""

import asyncio
import base64
import copy
import hashlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .disciplines import public_catalog
from .public_assets import public_assets
from .resource_saver import atomic_json, read_json
from .sponsors import write_file

MAX_BUNDLE_BYTES = 16 * 1024 * 1024
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicationSettings:
    profiles: tuple[str, ...]
    names: bool
    host: str
    port: int
    user: str
    directory: str
    key_file: str
    password: str | None = field(default=None, repr=False)
    known_hosts: str = ""
    trust_file: str = ""
    interval_seconds: int = 60
    iframe_origins: str = ""


def parse_host_keys(text):
    import paramiko
    keys = paramiko.HostKeys()
    try:
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
            if entry is None or entry.key is None:
                raise ValueError("Invalid verified host key")
            for host in entry.hostnames:
                keys.add(host, entry.key.get_name(), entry.key)
    except (ValueError, paramiko.SSHException, paramiko.hostkeys.InvalidHostKey) as error:
        raise ValueError("Invalid verified host key") from error
    return keys


class PublicationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    interval_seconds: Literal[10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600] = 60
    enabled: bool = False
    profiles: list[str] = Field(default_factory=list, max_length=64)
    names: bool = False
    host: str = Field(default="", max_length=253, pattern=r"^[a-zA-Z0-9.:-]*$")
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(default="", max_length=128)
    directory: str = Field(default=".", max_length=512)
    password: str = Field(default="", max_length=4096, repr=False)
    known_hosts: str = Field(default="", max_length=32768)
    key_file: str = Field(default="", max_length=512)
    iframe_origins: str = Field(default="", max_length=2048)

    @model_validator(mode="after")
    def validate_destination(self):
        for origin in self.iframe_origins.split():
            if not re.fullmatch(r"https://[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?(?::[0-9]{1,5})?", origin):
                raise ValueError("Iframe origins must be exact HTTPS origins without paths")
            port = urlsplit(origin).port
            if port is not None and not 1 <= port <= 65535:
                raise ValueError("Invalid iframe origin port")
        self.iframe_origins = " ".join(self.iframe_origins.split())
        if (len(set(self.profiles)) != len(self.profiles)
                or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", p) for p in self.profiles)
                or any(c in self.user + self.directory for c in "\r\n\x00")
                or ".." in self.directory.split("/")):
            raise ValueError("Invalid publication destination or profiles")
        if self.enabled:
            if not self.profiles or not self.host or not self.user or not self.directory or not (self.password or self.key_file):
                raise ValueError("Publication requires profiles, destination and credentials")
            if self.key_file and not Path(self.key_file).is_file():
                raise ValueError("SSH key file is missing")
            if self.known_hosts.strip():
                keys = parse_host_keys(self.known_hosts)
                host = self.host if self.port == 22 else f"[{self.host}]:{self.port}"
                if not keys.lookup(host):
                    raise ValueError("Verified host key must match this destination")
        return self


class PublicationStore:
    def __init__(self, path):
        self.path = Path(path)
        try:
            if self.path.exists():
                self.settings = PublicationConfig.model_validate(read_json(self.path))
            else:
                self.settings = PublicationConfig()
                self.save(self.settings)
        except ValueError:
            raise ValueError("Invalid stored publication settings") from None

    def active(self):
        s = self.settings
        if not s.enabled:
            return None
        return PublicationSettings(tuple(s.profiles), s.names, s.host, s.port, s.user, s.directory,
                                   s.key_file, s.password or None, s.known_hosts,
                                   str(self.path.with_name("publication_known_hosts")), s.interval_seconds, s.iframe_origins)

    def response(self):
        return self.settings.model_dump(exclude={"password"}) | {"password_set": bool(self.settings.password)}

    def save(self, settings):
        settings = PublicationConfig.model_validate(settings)
        # atomic_json uses a 0600 temporary file, preserving private permissions on replacement.
        atomic_json(self.path, settings.model_dump())
        self.settings = settings


def build_publication(runtime, settings):
    assets = {}

    def asset(data, extension):
        name = hashlib.sha256(data).hexdigest() + "." + extension
        assets[name] = data
        return "/" + name

    snapshots = {}
    for profile_id in settings.profiles:
        if not runtime.profiles.get(profile_id):
            continue  # Deleted profiles disappear in the next complete publication.
        snapshot = copy.deepcopy(runtime.snapshot(profile_id))
        snapshot["runtime_id"] += ":" + str(runtime.publication_frontend_revision)
        if not settings.names:
            snapshot["profile"]["fields"] = [f for f in snapshot["profile"]["fields"] if f != "shooter"]
            for row in snapshot["rows"]:
                for entry in row:
                    entry["live_shooter"] = None
                    if entry["target"]:
                        entry["target"]["shooter"] = None
        # Keep source states needed by the renderer, never export internal exception messages.
        snapshot["sources"] = {name: {"state": value["state"], "message": "", "last_check_at": None}
                               for name, value in snapshot["sources"].items()}
        for image in snapshot["sponsors"]:
            path = runtime.sponsors.path(image["id"])
            image["url"] = asset(path.read_bytes(), path.suffix[1:])
        if snapshot["logo"]:
            image = runtime.logo.image
            snapshot["logo"]["url"] = asset(base64.b64decode(image["data"], validate=True), image["type"])
        snapshots[profile_id] = snapshot
    bundle = {"version": 1, "published_at": time.time(), "interval_seconds": settings.interval_seconds, "catalog": public_catalog(), "snapshots": snapshots}
    encoded = json.dumps(bundle, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise ValueError("Publication exceeds 16 MiB")
    return encoded, assets


def publication_signature(bundle):
    payload = json.loads(bundle)
    payload.pop("published_at", None)
    for snapshot in payload["snapshots"].values():
        snapshot.pop("revision", None)
        snapshot.pop("server_time", None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).digest()


class SFTPPublisher:
    def __init__(self, settings):
        self.settings = settings
        self.client = None
        self.sftp = None
        self.uploaded = set()
        self.operation = "snapshot"
        self.last_signature = None
        self.server_time_offset = 0

    def close(self):
        if self.client:
            self.client.close()
        self.client = self.sftp = None
        self.uploaded.clear()

    def connect(self):
        import paramiko
        settings = self.settings
        self.operation = "host-key setup"
        self.client = paramiko.SSHClient()
        if settings.known_hosts.strip():
            keys = parse_host_keys(settings.known_hosts)
            for host in keys:
                for kind, key in keys[host].items():
                    self.client.get_host_keys().add(host, kind, key)
            self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            path = Path(settings.trust_file)
            if not settings.trust_file:
                raise ValueError("A persistent host-key store is required")
            if path.exists():
                self.client.load_host_keys(str(path))

            class PinFirstKey(paramiko.MissingHostKeyPolicy):
                def missing_host_key(self, client, hostname, key):
                    client.get_host_keys().add(hostname, key.get_name(), key)
                    data = "".join(f"{host} {kind} {known.get_base64()}\n"
                                   for host, keys in client.get_host_keys().items() for kind, known in keys.items())
                    write_file(path, data.encode())  # Pin atomically before sending credentials.

            self.client.set_missing_host_key_policy(PinFirstKey())
        self.operation = "SSH authentication"
        self.client.connect(settings.host, port=settings.port, username=settings.user, password=settings.password,
                            key_filename=settings.key_file or None, look_for_keys=False, allow_agent=False,
                            timeout=10, banner_timeout=10, auth_timeout=10)
        self.operation = "SFTP session"
        self.sftp = self.client.open_sftp()
        self.sftp.get_channel().settimeout(10)
        self.operation = "destination directory"
        self.sftp.chdir(settings.directory)  # Pre-provisioned, isolated publication directory.

    def deploy_frontend(self):
        self.operation = "frontend deployment"
        changed = 0
        # Manual deployment compares content and transfers only changed files.
        for name, data in public_assets(self.settings.iframe_origins).items():
            try:
                with self.sftp.open(name, "rb") as remote:
                    unchanged = remote.read(len(data) + 1) == data
            except FileNotFoundError:
                unchanged = False
            if unchanged:
                continue
            parent = str(Path(name).parent)
            if parent != ".":
                try:
                    self.sftp.stat(parent)
                except FileNotFoundError:
                    self.sftp.mkdir(parent)
            self.sftp.putfo(io.BytesIO(data), name + ".tmp", confirm=True)
            self.sftp.posix_rename(name + ".tmp", name)
            changed += 1
        # ponytail: files replace atomically one at a time, index.html last.
        # Use versioned release directories if deployments need all-file atomicity.
        return changed

    def upload(self, bundle, assets):
        try:
            if self.sftp is None:
                self.connect()
            signature = publication_signature(bundle)
            if signature == self.last_signature:
                self.operation = "publication heartbeat"
                now = time.time() + self.server_time_offset
                try:
                    self.sftp.utime("live.json", (now, now))
                    return False
                except FileNotFoundError:
                    pass  # Restore the complete snapshot if it was removed remotely.
            self.operation = "image upload"
            for name, data in assets.items():
                if name not in self.uploaded:
                    self.sftp.putfo(io.BytesIO(data), name + ".tmp", confirm=True)
                    self.sftp.posix_rename(name + ".tmp", name)
                    self.uploaded.add(name)
            self.operation = "snapshot upload"
            self.sftp.putfo(io.BytesIO(bundle), "live.json.tmp", confirm=True)
            # Never delete live.json first. Unsupported atomic replacement fails closed.
            self.operation = "atomic snapshot replacement"
            self.sftp.posix_rename("live.json.tmp", "live.json")
            # Calibrate metadata heartbeats against server time, not the range clock.
            self.server_time_offset = self.sftp.stat("live.json").st_mtime - time.time()
            self.last_signature = signature
            # ponytail: remove assets known to this process; clean restart orphans manually.
            # Add a persisted asset manifest if branding changes make orphan storage significant.
            self.operation = "old image cleanup"
            for name in self.uploaded - assets.keys():
                self.sftp.remove(name)
            self.uploaded = set(assets)
            return True
        except Exception:
            self.close()
            raise


def publication_failure(error, operation):
    if isinstance(error, FileNotFoundError):
        if operation == "destination directory":
            return "SFTP-Zielverzeichnis nicht gefunden. Den eingetragenen Pfad im SFTP-Konto prüfen; . verwendet das Anmeldeverzeichnis."
        if operation == "frontend deployment":
            return "Datei oder Verzeichnis bei der Bereitstellung der Webseite nicht gefunden. SFTP-Ziel und Schreibrechte prüfen."
        if operation == "snapshot":
            return "Lokale Bilddatei nicht gefunden. Logo und Sponsorenbilder prüfen."
        if operation in ("host-key setup", "SSH authentication"):
            return "Lokale SSH-Datei nicht gefunden. Schlüsseldatei und bereitgestellte Dateien prüfen."
        return "Datei oder Verzeichnis beim SFTP-Transfer nicht gefunden. Zielverzeichnis prüfen."
    return "SFTP-Übertragung fehlgeschlagen. Ziel, Zugangsdaten und Hostschlüssel prüfen."


async def publish(runtime):
    publisher = None
    delay = 60
    try:
        while True:
            settings = runtime.publication
            if publisher and publisher.settings != settings:
                publisher.close()
                publisher = None
                delay = 60
            if settings is None:
                runtime.publication_status["state"] = "disabled"
                await asyncio.sleep(1)
                continue
            if publisher is None:
                publisher = SFTPPublisher(settings)
                delay = settings.interval_seconds
                runtime.publication_status["state"] = "connecting"
            try:
                publisher.operation = "snapshot"
                bundle, assets = build_publication(runtime, settings)
                upload = asyncio.create_task(asyncio.to_thread(publisher.upload, bundle, assets))
                try:
                    await asyncio.shield(upload)
                except asyncio.CancelledError:
                    # Finish the bounded SFTP operation before closing its transport.
                    try:
                        await upload
                    except Exception:
                        pass
                    raise
                if runtime.publication == settings:
                    runtime.publication_status = {"state": "connected", "last_success_at": time.time(), "message": ""}
                delay = settings.interval_seconds
            except Exception as error:
                runtime.publication_status["state"] = "error"
                runtime.publication_status["message"] = publication_failure(error, publisher.operation)
                # Do not log secrets, remote paths, or identifying result payloads.
                log.warning("Public publication failed during %s (%s); retrying", publisher.operation, type(error).__name__)
                delay = min(max(settings.interval_seconds, delay * 2), 3600)
            try:
                await asyncio.wait_for(runtime.publication_changed.wait(), delay)
            except TimeoutError:
                pass
            runtime.publication_changed.clear()
    finally:
        if publisher:
            publisher.close()
