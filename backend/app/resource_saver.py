"""Global resource-saver settings, cross-process signals and polling policy."""

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


INACTIVITY_SECONDS = 30 * 60
OUTAGE_SECONDS = 20 * 60
NO_VIEWER_SECONDS = 5 * 60
PRESENCE_HEARTBEAT_SECONDS = 15


class ResourceSaverSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    poll_interval_seconds: int = Field(default=10, ge=2, le=300)


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    return json.loads(Path(path).read_text())


class ResourceSaverStore:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists():
            self.settings = ResourceSaverSettings.model_validate(read_json(self.path))
        else:
            self.settings = ResourceSaverSettings()
            self.save(self.settings)

    def save(self, settings):
        settings = ResourceSaverSettings.model_validate(settings)
        atomic_json(self.path, settings.model_dump())
        self.settings = settings
        return settings


class ResourceSaverPolicy:
    """Coordinates adaptive source waits without touching vendor resources itself."""

    def __init__(self, stop, settings_path, presence_path, status_path, last_stand_change_at,
                 *, wall_clock=time.time):
        self.stop = stop
        self.settings_path = Path(settings_path)
        self.presence_path = Path(presence_path)
        self.status_path = Path(status_path)
        self.wall_clock = wall_clock
        self.condition = threading.Condition()
        self.settings = ResourceSaverSettings()
        self.last_stand_change_at = last_stand_change_at or wall_clock()
        self.last_viewer_seen_at = wall_clock()
        self.viewer_count = 0
        self.sources = {}
        self.offline_since = None
        self.active = False
        self.reasons = []
        self.last_transition_at = wall_clock()
        self._settings_fingerprint = None
        self._presence_fingerprint = None
        self._thread = None
        self._load_settings(initial=True)
        self._load_presence(initial=True)
        self._refresh_state()

    @staticmethod
    def _fingerprint(path):
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _load_settings(self, initial=False):
        fingerprint = self._fingerprint(self.settings_path)
        if not initial and fingerprint == self._settings_fingerprint:
            return False
        self._settings_fingerprint = fingerprint
        try:
            settings = ResourceSaverSettings.model_validate(read_json(self.settings_path))
        except (OSError, ValueError, json.JSONDecodeError):
            if initial:
                settings = ResourceSaverSettings()
            else:
                return False  # Preserve the last valid settings during an atomic replacement or bad edit.
        changed = settings != self.settings
        self.settings = settings
        return changed

    def _load_presence(self, initial=False):
        fingerprint = self._fingerprint(self.presence_path)
        if not initial and fingerprint == self._presence_fingerprint:
            return False
        self._presence_fingerprint = fingerprint
        try:
            payload = read_json(self.presence_path)
            count = payload["viewer_count"]
            updated_at = payload["updated_at"]
            last_seen_at = payload.get("last_viewer_seen_at", updated_at)
            if (type(count) is not int or count < 0 or not isinstance(updated_at, (int, float))
                    or not isinstance(last_seen_at, (int, float))):
                raise ValueError("Invalid viewer presence")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        previous = self.viewer_count
        self.viewer_count = count
        self.last_viewer_seen_at = float(last_seen_at)
        return count != previous

    def _all_sources_disconnected(self):
        configured = [state for state in self.sources.values() if state != "disabled"]
        return bool(configured) and all(state == "disconnected" for state in configured)

    def _refresh_state(self):
        now = self.wall_clock()
        if self._all_sources_disconnected():
            if self.offline_since is None:
                self.offline_since = now
        else:
            self.offline_since = None
        reasons = []
        viewerless = now - self.last_viewer_seen_at >= NO_VIEWER_SECONDS
        if viewerless:
            self.viewer_count = 0  # Treat a stale positive heartbeat as a crashed/disconnected backend.
        if self.settings.enabled:
            if now - self.last_stand_change_at >= INACTIVITY_SECONDS:
                reasons.append("inactivity")
            if self.offline_since is not None and now - self.offline_since >= OUTAGE_SECONDS:
                reasons.append("outage")
            if viewerless:
                reasons.append("no_viewers")
        active = bool(reasons)
        changed = (active, reasons) != (self.active, self.reasons)
        if active != self.active:
            self.last_transition_at = now
        self.active, self.reasons = active, reasons
        if changed:
            self._write_status()
        return changed

    def _write_status(self):
        try:
            atomic_json(self.status_path, self.status())
        except OSError:
            pass  # Runtime reporting must never stop collection.

    def status(self):
        return {
            "enabled": self.settings.enabled,
            "active": self.active,
            "reasons": list(self.reasons),
            "poll_interval_seconds": self.settings.poll_interval_seconds,
            "viewer_count": self.viewer_count,
            "last_viewer_seen_at": self.last_viewer_seen_at,
            "last_stand_change_at": self.last_stand_change_at,
            "last_transition_at": self.last_transition_at,
        }

    def refresh_controls(self):
        with self.condition:
            settings_changed = self._load_settings()
            viewers_changed = self._load_presence()
            state_changed = self._refresh_state()
            if settings_changed or viewers_changed or state_changed:
                self._write_status()
                self.condition.notify_all()

    def monitor(self):
        while not self.stop.wait(1):
            self.refresh_controls()

    def start(self):
        self._write_status()
        self._thread = threading.Thread(target=self.monitor, daemon=True)
        self._thread.start()

    def close(self):
        with self.condition:
            self.condition.notify_all()
        if self._thread:
            self._thread.join(timeout=2)

    def wait(self, normal_seconds):
        with self.condition:
            self._refresh_state()
            timeout = max(normal_seconds, self.settings.poll_interval_seconds) if self.active else normal_seconds
            self.condition.wait(timeout)
        return self.stop.is_set()

    def interval(self, normal_seconds):
        with self.condition:
            self._refresh_state()
            return max(normal_seconds, self.settings.poll_interval_seconds) if self.active else normal_seconds

    def source_status(self, source, state):
        with self.condition:
            changed = self.sources.get(source) != state
            self.sources[source] = state
            state_changed = self._refresh_state()
            if changed or state_changed:
                self._write_status()
                self.condition.notify_all()

    def record_activity(self, changed_at=None):
        with self.condition:
            self.last_stand_change_at = self.wall_clock() if changed_at is None else changed_at
            self._refresh_state()
            self._write_status()
            self.condition.notify_all()
