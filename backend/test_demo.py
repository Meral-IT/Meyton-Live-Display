"""Demo simulation follows configured ranges and the real SQLite/display path."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.demo_worker import DemoWorker
from app.main import create_app
from app.profiles import Profile, ProfileStore
from app.storage import ResultStore
from test_app import target


class DemoChecks(unittest.TestCase):
    def test_shared_database_practice_scoring_clear_replacement_and_profile_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path, profiles_path = Path(directory) / "results.sqlite3", Path(directory) / "profiles.json"
            profiles = ProfileStore(profiles_path)
            profiles.save([Profile(id="alles", name="Alles", rows=[[1, 2], [51, 52]]),
                           Profile(id="kleinkaliber", name="Kleinkaliber", rows=[[51, 52]])])
            now = time.time()
            original = ResultStore(path)
            original.baseline([], now=now - 100)
            original.ingest([target(at=now - 10)], now=now)
            started = original.started_at
            original.close()
            worker = DemoWorker(path, profiles_path, interval=0.1)
            try:
                worker.tick(now)
                self.assertEqual(set(worker.sessions), {1, 2, 51, 52})
                self.assertIsNotNone(worker.store.target(-1))  # Real records are retained.
                self.assertEqual(worker.store.started_at, started)
                self.assertEqual(worker.store.ranges.targets[51]["discipline_id"], 41211010)
                self.assertTrue(worker.store.ranges.public(1, profiles.profiles[0])["practice"])
                for _ in range(5):
                    now += 1
                    worker.tick(now)
                current = worker.store.ranges.targets[1]
                scored = [shot for shot in current["shots"].values() if shot["position"] == 1]
                self.assertEqual(len(scored), 1)
                self.assertEqual(current["total"]["value"], sum(s["whole_score"]["value"] for s in scored))
                self.assertEqual(current["decimal_total"]["value"], sum(s["score"]["value"] for s in scored))
                self.assertTrue(all(0 <= s["score"]["value"] <= 109 for s in current["shots"].values()))
                app = create_app(profiles_path, result_path=path)
                with TestClient(app) as client:
                    snapshot = client.get("/api/ranges/1").json()
                    entry = snapshot["rows"][0][0]
                    self.assertEqual(entry["occupancy"], "occupied")
                    self.assertEqual(entry["target"]["source"], "demo")
                    self.assertFalse(entry["target"]["practice"])
                for _ in range(9):
                    now += 1
                    worker.tick(now)
                old_id = worker.sessions[51]["target"]["id"]
                self.assertEqual(worker.sessions[51]["phase"], "finished")
                now += 8
                worker.tick(now)
                self.assertIsNone(worker.store.ranges.public(51, profiles.profiles[1]))
                self.assertEqual(worker.store.ranges.occupancy[51], "free")
                now += 5
                worker.tick(now)
                self.assertLess(worker.sessions[51]["target"]["id"], old_id)
                self.assertIsNotNone(worker.store.target(old_id))
                profiles.profiles[0].rows[0].append(77)
                profiles.save(profiles.profiles)
                worker.tick(now + 1)
                self.assertIn(77, worker.store.ranges.targets)
                worker.store.start_sources(("db", "sdf", "lana"))
                worker.store.source_status("db", "disconnected", "offline")
                sources = json.loads(worker.store.connection.execute("SELECT sources FROM meta").fetchone()[0])
                self.assertEqual(set(sources), {"db", "sdf", "lana"})
            finally:
                worker.store.close()

    def test_demo_does_not_initialize_vendor_baseline_and_ids_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path, profiles = Path(directory) / "results.sqlite3", Path(directory) / "missing-profiles.json"
            worker = DemoWorker(path, profiles)
            now = time.time()
            try:
                worker.tick(now)
                self.assertIsNone(worker.store.started_at)
                lowest = min(t["id"] for t in worker.store.ranges.targets.values())
                self.assertLess(lowest, -(2**31))
                self.assertFalse(profiles.exists())  # Startup fallback never writes profile files.
            finally:
                worker.store.close()
            worker = DemoWorker(path, profiles)
            try:
                worker.tick(now + 1)
                self.assertLess(max(t["id"] for t in worker.store.ranges.targets.values()), lowest)
                self.assertIsNone(worker.store.started_at)
            finally:
                worker.store.close()
            for interval in (0, -1, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    DemoWorker(path, profiles, interval=interval)


if __name__ == "__main__":
    unittest.main()
