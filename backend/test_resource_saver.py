"""Resource-saver settings, trigger and wake-up regression checks."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.resource_saver import (INACTIVITY_SECONDS, NO_VIEWER_SECONDS, OUTAGE_SECONDS,
                                ResourceSaverPolicy, ResourceSaverSettings,
                                ResourceSaverStore, atomic_json, read_json)


class Clock:
    def __init__(self, value=1_000_000):
        self.value = value

    def __call__(self):
        return self.value


class ResourceSaverChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.settings = self.root / "settings.json"
        self.presence = self.root / "viewers.json"
        self.status = self.root / "status.json"
        self.stop = threading.Event()
        self.clock = Clock()

    def tearDown(self):
        self.stop.set()
        self.directory.cleanup()

    def policy(self, enabled=True):
        atomic_json(self.settings, {"enabled": enabled, "poll_interval_seconds": 10})
        policy = ResourceSaverPolicy(self.stop, self.settings, self.presence, self.status,
                                     self.clock(), wall_clock=self.clock)
        for source in ("db", "sdf", "lana"):
            policy.source_status(source, "starting")
        return policy

    def test_atomic_settings_defaults_persistence_and_validation(self):
        store = ResourceSaverStore(self.settings)
        self.assertEqual(store.settings, ResourceSaverSettings())
        self.assertEqual(read_json(self.settings), {"enabled": False, "poll_interval_seconds": 10})
        store.save({"enabled": True, "poll_interval_seconds": 25})
        self.assertEqual(ResourceSaverStore(self.settings).settings.poll_interval_seconds, 25)
        for interval in (1, 301, 2.5):
            with self.subTest(interval=interval), self.assertRaises(ValidationError):
                store.save({"enabled": True, "poll_interval_seconds": interval})

    def test_each_trigger_or_combination_and_recovery(self):
        policy = self.policy()
        self.clock.value += INACTIVITY_SECONDS
        policy.refresh_controls()
        self.assertEqual(policy.reasons, ["inactivity", "no_viewers"])
        self.assertEqual(policy.interval(1), 10)

        atomic_json(self.presence, {"runtime_id": "one", "viewer_count": 1, "updated_at": self.clock()})
        policy.refresh_controls()
        self.assertEqual(policy.reasons, ["inactivity"])
        policy.record_activity()
        self.assertFalse(policy.active)
        self.assertEqual(policy.interval(1), 1)

        policy.settings = ResourceSaverSettings(enabled=True, poll_interval_seconds=2)
        self.clock.value += INACTIVITY_SECONDS
        self.assertEqual(policy.interval(5), 5)  # Saver settings never accelerate a slower normal task.
        policy.record_activity()

        policy.source_status("sdf", "disabled")
        policy.source_status("db", "disconnected")
        policy.source_status("lana", "disconnected")
        self.clock.value += OUTAGE_SECONDS
        atomic_json(self.presence, {"runtime_id": "one", "viewer_count": 1, "updated_at": self.clock()})
        policy.refresh_controls()
        self.assertEqual(policy.reasons, ["outage"])
        policy.source_status("lana", "connected")
        self.assertFalse(policy.active)

        policy.source_status("lana", "degraded")
        self.clock.value += NO_VIEWER_SECONDS
        policy.refresh_controls()
        self.assertEqual(policy.reasons, ["no_viewers"])
        atomic_json(self.settings, {"enabled": False, "poll_interval_seconds": 10})
        policy.refresh_controls()
        self.assertFalse(policy.active)

    def test_viewer_arrival_wakes_waiters_and_bad_files_keep_last_valid_values(self):
        policy = self.policy()
        policy.start()
        returned = threading.Event()
        waiter = threading.Thread(target=lambda: (policy.wait(100), returned.set()))
        waiter.start()
        time.sleep(0.02)
        atomic_json(self.presence, {"runtime_id": "one", "viewer_count": 1, "updated_at": self.clock()})
        policy.refresh_controls()
        self.assertTrue(returned.wait(1))
        self.assertEqual(policy.viewer_count, 1)

        self.settings.write_text("not-json")
        self.presence.write_text(json.dumps({"viewer_count": -1, "updated_at": "bad"}))
        policy.refresh_controls()
        self.assertTrue(policy.settings.enabled)
        self.assertEqual(policy.viewer_count, 1)
        self.assertEqual(read_json(self.status)["poll_interval_seconds"], 10)
        self.stop.set()
        policy.close()
        waiter.join(1)

    def test_stale_positive_presence_becomes_viewerless(self):
        atomic_json(self.presence, {"runtime_id": "dead", "viewer_count": 3,
                                   "last_viewer_seen_at": self.clock(), "updated_at": self.clock()})
        policy = self.policy()
        policy._load_presence(initial=True)
        self.clock.value += NO_VIEWER_SECONDS
        policy.refresh_controls()
        self.assertEqual(policy.viewer_count, 0)
        self.assertIn("no_viewers", policy.reasons)


if __name__ == "__main__":
    unittest.main()
