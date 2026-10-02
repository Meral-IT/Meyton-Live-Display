"""Durable collection, retention and read-only display regression checks."""

import copy
from datetime import datetime
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.profiles import Profile
from app.storage import ResultStore, connect_reader, read_snapshot, retention_days
from app.worker import Worker
from app.sources import read_db_index
from test_app import target


class StorageChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "results.sqlite3"
        self.now = time.time()
        self.store = ResultStore(self.path)
        self.profile = Profile(id="test", name="Test", rows=[[1]], hits="all")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def start(self):
        self.store.baseline([], now=self.now - 1000)

    def test_no_backfill_baseline_survives_restart_and_imports_complete_session(self):
        old = target(2, self.now - 100)
        self.store.baseline([{"id": old["id"], "last_shot": self.now - 100}], now=self.now - 90)
        self.assertFalse(self.store.ingest([old], now=self.now))
        correction = copy.deepcopy(old)
        correction["modified"] = self.now
        correction["shots"][(1, 2)]["score"]["value"] = 90
        self.assertFalse(self.store.ingest([correction], now=self.now))
        self.store.close()
        self.store = ResultStore(self.path)
        self.store.baseline([], now=self.now + 1)
        self.assertEqual(self.store.started_at, self.now - 90)
        updated = target(3, self.now)
        self.assertTrue(self.store.ingest([updated], now=self.now))
        self.assertEqual(len(self.store.target(-1)["shots"]), 3)
        self.assertEqual(self.store.ranges.public(1, self.profile)["shot_count"], 3)

    def test_duplicates_corrections_removals_and_partial_updates(self):
        self.start()
        initial = target(2, self.now)
        self.assertTrue(self.store.ingest([initial], now=self.now))
        writes = self.store.connection.total_changes
        self.assertFalse(self.store.ingest([initial], now=self.now + 10))
        self.assertEqual(self.store.connection.total_changes, writes)
        correction = copy.deepcopy(initial)
        correction["shots"][(1, 1)]["x_mm"] = 9
        self.assertTrue(self.store.ingest([correction], now=self.now + 10))
        self.assertEqual(self.store.target(-1)["shots"][(1, 1)]["x_mm"], 9)
        del correction["shots"][(1, 2)]
        self.store.ingest([correction], now=self.now + 10)
        self.assertEqual(len(self.store.target(-1)["shots"]), 1)
        sdf = target(3, self.now + 1)
        sdf.update(source="sdf", full=False, received_at=self.now + 1)
        sdf["shots"] = {(1, 3): sdf["shots"][(1, 3)]}
        self.store.ingest([sdf], now=self.now + 1)
        self.assertEqual(len(self.store.target(-1)["shots"]), 2)
        writes = self.store.connection.total_changes
        sdf["received_at"] += 1
        self.assertFalse(self.store.ingest([sdf], now=self.now + 2))
        self.assertEqual(self.store.connection.total_changes, writes)

    def test_first_partial_export_seeds_complete_database_session(self):
        old = target(2, self.now - 100)
        self.store.baseline([{"id": -1, "last_shot": self.now - 100}], now=self.now - 90)
        sdf = target(3, self.now)
        sdf.update(source="sdf", full=False, _db_snapshot=old)
        sdf["shots"] = {(1, 3): sdf["shots"][(1, 3)]}
        self.store.ingest([sdf], now=self.now)
        self.assertEqual(len(self.store.target(-1)["shots"]), 3)
        self.assertNotIn("_db_snapshot", self.store.target(-1))

    def test_history_current_selection_and_durable_clear(self):
        self.start()
        old, new = target(at=self.now - 10), target(at=self.now)
        new["id"] = -20  # IDs do not determine chronological order.
        self.store.ingest([old, new], now=self.now)
        self.assertEqual(self.store.ranges.targets[1]["id"], -20)
        self.assertIsNotNone(self.store.target(-1))
        old["shots"][(1, 1)]["x_mm"] = 4
        old["modified"] = self.now + 10
        self.store.ingest([old], now=self.now)
        self.assertEqual(self.store.ranges.targets[1]["id"], -20)
        self.store.occupancy({1: {"state": "free", "shooter": "Unbekannt"}})
        self.store.close()
        self.store = ResultStore(self.path)
        self.store.occupancy({1: {"state": "occupied", "shooter": "Test, Ada"}})
        self.assertIsNone(self.store.ranges.public(1, self.profile))
        self.store.ingest([new], now=self.now)
        self.assertIsNone(self.store.ranges.public(1, self.profile))
        hit = target(2, self.now + 1)
        hit["id"] = -20
        self.store.ingest([hit], now=self.now + 1)
        self.assertEqual(self.store.ranges.public(1, self.profile)["shot_count"], 2)

    def test_retention_uses_last_shot_not_polling_and_prevents_resurrection(self):
        self.store.baseline([], now=self.now - 8 * 86400)
        recent = target(at=self.now - 6 * 86400)
        self.store.ingest([recent], now=self.now)
        recent["modified"] = self.now  # Correction time never extends last-shot retention.
        self.store.ingest([recent], now=self.now)
        self.assertEqual(self.store.cleanup(now=self.now + 2 * 86400), 1)
        self.assertIsNone(self.store.target(-1))
        self.assertFalse(self.store.ingest([recent], now=self.now + 2 * 86400))
        self.store.close()
        self.store = ResultStore(self.path, days=30)
        self.assertFalse(self.store.ingest([recent], now=self.now + 2 * 86400))
        self.assertIsNone(self.store.ranges.public(1, self.profile))
        for value in (0, -1, "1.5", "bad", True):
            with self.assertRaises(ValueError):
                retention_days(value)
        self.assertEqual(retention_days("3"), 3)

    def test_reader_is_read_only_atomic_and_masks_concealed_shots(self):
        self.start()
        self.store.ingest([target(at=self.now, mode=5)], now=self.now)
        reader = connect_reader(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("DELETE FROM targets")
            reader.rollback()
            self.store.connection.execute("BEGIN")
            self.store.connection.execute("UPDATE meta SET revision=revision+100")
            state, _, meta = read_snapshot(reader, self.now)
            self.assertLess(meta["revision"], 100)
            public = state.public(1, self.profile)
            self.assertIsNone(public["latest"]["score"])
            self.assertIsNone(public["latest"]["x_mm"])
            self.store.connection.rollback()
            self.store.ingest([target(2, self.now + 1, position=0)], now=self.now + 1)
            state, _, _ = read_snapshot(reader, self.now + 1)
            self.assertTrue(state.public(1, self.profile)["practice"])
            self.assertEqual(state.public(1, self.profile)["scored_shot_count"], 0)
        finally:
            reader.close()

    def test_backend_restores_offline_results_reports_stale_worker_and_unknown_occupancy(self):
        self.start()
        self.store.ingest([target(at=self.now)], now=self.now)
        self.store.occupancy({1: {"state": "occupied", "shooter": "Test, Ada"}})
        self.store.source_status("lana", "connected", "ok", now=self.now)
        self.store.heartbeat(now=self.now - 16)
        app = create_app(Path(self.directory.name) / "profiles.json", result_path=self.path)
        with TestClient(app) as client:
            data = client.get("/api/ranges/1").json()
            self.assertEqual(data["rows"][0][0]["target"]["shot_count"], 1)
            self.assertEqual(data["rows"][0][0]["occupancy"], "unknown")
            self.assertEqual(data["sources"]["worker"]["state"], "degraded")
            self.assertEqual(data["sources"]["local_db"]["state"], "connected")
            self.assertIsNotNone(data["rows"][0][0]["target"]["latest"]["persisted_at"])

    def test_worker_discovers_all_ranges_and_reconciles_sessions_missed_during_restart(self):
        self.store.close()
        worker = Worker(self.path)
        self.store = worker.store
        old_index = [{"id": -1, "lane": 77, "modified": self.now - 100, "last_shot": self.now - 100}]
        self.assertEqual(worker.index(old_index), (set(), set()))
        self.assertTrue(worker.ready.is_set())
        row = {"id": -1, "lane": 77, "modified": self.now + 1, "last_shot": self.now + 1}
        live, _ = worker.index([row])
        self.assertEqual(live, {-1})
        worker.store.close()
        worker = Worker(self.path)
        self.store = worker.store
        self.assertEqual(worker.index([row])[0], {-1})
        self.assertEqual(worker.store.started_at, self.store.started_at)

    def test_incremental_discovery_fetches_complete_last_shot_watermarks(self):
        row = {"ScheibenID": -1, "StandNr": 77, "Zeitstempel": datetime(2026, 10, 1, 12, 0),
               "last_shot": datetime(2026, 9, 30, 12, 0, 0, 270000)}
        with patch("app.sources.database") as database:
            cursor = database.return_value.__enter__.return_value
            cursor.fetchall.side_effect = [[{"ScheibenID": -1}], [], [row]]
            index = read_db_index(self.now)
            self.assertEqual(index[0]["lane"], 77)
            self.assertLess(index[0]["last_shot"], index[0]["modified"])
            sql, values = cursor.execute.call_args.args
            self.assertIn("WHERE ScheibenID IN", sql)
            self.assertNotIn("Zeitstempel>=%s", sql)
            self.assertEqual(values, (-1, -1))

    def test_worker_failed_read_resets_discovery_and_recovers(self):
        self.store.close()
        worker = Worker(self.path)
        self.store = worker.store
        old = {"id": -1, "lane": 1, "modified": self.now - 100, "last_shot": self.now - 100}
        new = {**old, "modified": self.now + 1, "last_shot": self.now + 1}
        states = []
        def enqueue(kind, data):
            if kind == "index":
                rows, response = data
                response.put(worker.index(rows))
            elif kind == "targets":
                worker.store.ingest(data)
            return True
        def status(source, state, message):
            states.append(state)
            if states.count("connected") == 2:
                worker.stop.set()
        worker.enqueue, worker.status = enqueue, status
        with patch.dict("os.environ", {"SM_DB_HOST": "mock"}), \
             patch("app.sources.read_db_index", side_effect=[[old], [new], [new]]) as discover, \
             patch("app.sources.read_db", side_effect=[ConnectionError("offline"), [target(2, self.now + 1)]]), \
             patch.object(worker.stop, "wait", return_value=False):
            worker.poll()
        self.assertEqual(states, ["connected", "disconnected", "connected"])
        self.assertIsNone(discover.call_args_list[-1].args[0])
        self.assertEqual(len(worker.store.target(-1)["shots"]), 2)


if __name__ == "__main__":
    unittest.main()
