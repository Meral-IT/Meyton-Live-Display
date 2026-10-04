"""Run with: PYTHONPATH=backend python -m unittest discover -s backend."""

import os
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock
from xml.etree.ElementTree import ParseError

from fastapi.testclient import TestClient

from app.domain import MAX_XML_BYTES, RangeState, from_db, parse_sdf, score, shooter_name, stamp, target_id
from app.main import create_app
from app.profiles import Profile, ProfileStore, seeds
from app.sources import LANAReader, SDFReader, lana_lanes, read_startlist_ids
from app.storage import ResultStore


def target(number=1, at=100, *, mode=2, position=1):
    return {"id": -1, "lane": 1, "shooter": "Test, Ada", "discipline": "LG Auflage 30", "discipline_id": 10111030,
            "modified": at, "source": "db", "full": True, "total": score(100 * number), "decimal_total": score(105 * number, "ZehntelRing"),
            "shots": {(position, n): {"position": position, "number": n, "series": (n - 1) // 10 + 1,
                       "x_mm": 1.25, "y_mm": -2.5, "score": score(105, "ZehntelRing"), "whole_score": score(100),
                       "mode": mode, "inner_ten": True, "invalid": False, "time": at - number + n} for n in range(1, number + 1)},
            "series": {(position, 1): {"position": position, "number": 1, "score": score(100 * number), "decimal_score": None}}}


def xml(status="LIVE_UPDATE", position=1):
    return f'''<ResultList xmlns="http://meyton.org/2011/Schema/ResultList" Version="0.2.2">
    <ResultRecord TargetID="ffffffff" LaneNo="1" StartID="1" StartListID="1" ResultStatus="{status}">
    <Shooter><FamilyName>Test</FamilyName><GivenName>Ada</GivenName></Shooter>
    <Discipline><Name>LG Auflage 30</Name><ID>10111030</ID></Discipline>
    <Total><Result>100</Result><Unit>Ring</Unit></Total><ShotNoTotal>30</ShotNoTotal>
    <Aimings><AimingData AimingID="{position}"><Shot ShotID="1" IsInnerTen="true">
    <Coordinate><CCoordinate Resolution="100"><X>125</X><Y>-250</Y></CCoordinate></Coordinate>
    <RingValue><Result>105</Result><Unit>ZehntelRing</Unit></RingValue>
    <TimeStamp><DateTime>2026-09-30T20:12:59Z</DateTime><Hundredth>27</Hundredth></TimeStamp>
    </Shot></AimingData></Aimings></ResultRecord></ResultList>'''.encode()


class DomainChecks(unittest.TestCase):
    def test_live_occupancy_and_cleared_archive(self):
        response = {"Prot": "MEWS", "VerP": 2, "SubProt": "LA", "VerSP": 2, "Rsp": "GetLaneInfo",
                    "SeqNoRsp": 1, "RV": 0, "Data": [{"LaneNo": 1, "Free": True, "Shooter": ""}]}
        free = lana_lanes(response, 1)
        state = RangeState()
        profile = Profile(id="test", name="Test", rows=[[1]])
        state.set_occupancy(free)  # LANA may arrive before the first DB snapshot.
        state.apply(target())
        self.assertIsNone(state.public(1, profile))
        occupied = {1: {"state": "occupied", "shooter": "Test, Ada"}}
        state.set_occupancy(occupied)
        self.assertIsNone(state.public(1, profile))  # Don't resurrect the cleared target.
        correction = target()
        correction["modified"] = 200
        state.apply(correction)
        self.assertIsNone(state.public(1, profile))
        state.apply(target(2, 201))
        self.assertEqual(state.public(1, profile)["shot_count"], 2)
        state.set_occupancy({1: {"state": "occupied", "shooter": "New, Shooter"}})
        self.assertIsNone(state.public(1, profile))
        replacement = target(at=210)
        replacement.update(id=-2, shooter="New, Shooter")
        state.apply(replacement)
        self.assertEqual(state.public(1, profile)["shooter"], "New, Shooter")
        state.set_occupancy({})  # Missing lanes aren't proof of free/offline state.
        self.assertIsNotNone(state.public(1, profile))
        state.set_occupancy(free)
        state.set_occupancy({})
        self.assertIsNone(state.public(1, profile))  # Lost occupancy must not resurrect a confirmed clear.
        for changes in [{"RV": -14}, {"SeqNoRsp": 2}, {"Data": [{"LaneNo": 1, "Free": "true"}]},
                        {"Data": [{"LaneNo": 0, "Free": True}]}, {"Data": None}]:
            with self.assertRaises(ValueError):
                lana_lanes({**response, **changes}, 1)

    def test_unknown_shooter_names(self):
        for family, given in [("unknown", "unknown"), ("unkown", "unkown"), (" UNKNOWN ", "Unkown"), (None, None)]:
            self.assertEqual(shooter_name(family, given), "Unbekannt")
        self.assertEqual(shooter_name("Test", "Ada"), "Test, Ada")
        self.assertEqual(shooter_name("unknown", "Ada"), "Ada")
        anonymous = xml().replace(b">Test<", b">unknown<").replace(b">Ada<", b">unknown<")
        self.assertEqual(parse_sdf(anonymous)[0]["shooter"], "Unbekannt")

    def test_standard_training_targets(self):
        state = RangeState()
        state.apply(target())
        profile = Profile(id="test", name="Test", rows=[[1]])
        for discipline_id, name, expected in [
            (10111030, "LG Auflage 30", "lg"),
            (4, "LG Auflage 30", "lg"),
            (41211010, "KK 5P+10W", "kk"),
            (41209005, "KK 3P+5W", "kk"),
            (41209010, "KK 3P+10W", "kk"),
            (11011010, "LG 5P+10W", "lg"),
            (11009005, "LG 3P+5W", "lg"),
            (11012003, "LG 3W", "lg"),
            (18052005, "LG Schach 10x10 5W", "schach10"),
            (18052010, "LG Schach 10x10", "schach10"),
            (16, "LG Schach 10x10 5W", "schach10"),
            (2, "LG Schach 5W", "schach10"),
            (18054010, "LG Dart", None),
            (10210040, "LP 40", "lp"),
            (6, "LP 40", "lp"),
            (4, "LG Schach 10x10 5W", "schach10"),
            (4, "LG Dart", None),
            (4, "Eigene Scheibe", None),
            (18054010, "LG Auflage 30", None),  # A nonstandard ID takes precedence.
            (50135030, "KK 100m 30", None),
        ]:
            with self.subTest(discipline=name):
                state.targets[1]["discipline_id"] = discipline_id
                state.targets[1]["discipline"] = name
                self.assertEqual(state.public(1, profile)["target_kind"], expected)

    def test_vendor_encodings_and_safe_xml(self):
        db = from_db({"ScheibenID": -1, "StandNr": 54, "Nachname": "Test", "Vorname": "Ada", "Disziplin": "Test",
                      "DisziplinID": 9, "Zeitstempel": None, "TotalRing": 820, "TotalRing01": 853}, [],
                     [{"Stellung": 1, "Serie": 1, "Ring": 82, "Ring01": 853}])
        self.assertEqual(db["total"]["value"] / db["total"]["scale"], 82)
        self.assertEqual(db["series"][(1, 1)]["score"]["value"] / db["series"][(1, 1)]["score"]["scale"], 82)
        self.assertEqual(db["series"][(1, 1)]["decimal_score"]["value"] / db["series"][(1, 1)]["decimal_score"]["scale"], 85.3)
        self.assertEqual(target_id("ffffffff"), -1)
        self.assertEqual(target_id("f53ad4d4"), -180693804)
        self.assertEqual(target_id("7fffffff"), 2147483647)
        with self.assertRaises(ValueError):
            target_id("-123")
        parsed = parse_sdf(xml())[0]
        hit = parsed["shots"][(1, 1)]
        self.assertEqual((hit["x_mm"], hit["y_mm"]), (1.25, -2.5))
        self.assertEqual(hit["score"]["value"] / hit["score"]["scale"], 10.5)
        self.assertEqual(parsed["total"]["value"] / parsed["total"]["scale"], 10)
        self.assertEqual(len(parsed["shots"]), 1)  # ShotNoTotal=30 is not actual count.
        self.assertIsNone(hit["series"])
        with patch.dict(os.environ, {"SM_TIMEZONE": "Europe/Berlin", "SM_SDF_TIMEZONE": "Europe/Berlin"}):
            self.assertEqual(stamp("2026-09-30 20:12:59", 270), hit["time"])
        for data in [b"<!DOCTYPE x [<!ENTITY y 'x'>]><x/>", "<!DOCTYPE x><x/>".encode("utf-16"), b"x" * (MAX_XML_BYTES + 1), xml()[:100]]:
            with self.assertRaises((ValueError, ParseError)):
                parse_sdf(data)
        self.assertIn((0, 1), parse_sdf(xml(position=0))[0]["shots"])

    def test_merge_duplicates_rollbacks_corrections_and_removals(self):
        state = RangeState()
        state.apply(target())
        self.assertFalse(state.apply(target()))
        live = target(2, 102)
        live["source"] = "sdf"
        live["full"] = False
        live["shots"] = {(1, 2): live["shots"][(1, 2)]}
        self.assertTrue(state.apply(live))
        self.assertEqual(len(state.targets[1]["shots"]), 2)
        self.assertFalse(state.apply(live))
        state.apply(target())  # DB has not exported second shot yet.
        self.assertEqual(len(state.targets[1]["shots"]), 2)
        corrected = target(2, 102)
        corrected["modified"] = 104
        corrected["shots"][(1, 2)]["score"] = score(95, "ZehntelRing")
        state.apply(corrected)
        self.assertEqual(state.targets[1]["shots"][(1, 2)]["score"]["value"], 95)
        self.assertFalse(state.apply(live))  # Repeated XML cannot undo correction.
        removed = target(1, 100)
        removed["modified"] = 105
        state.apply(removed)
        self.assertEqual(len(state.targets[1]["shots"]), 1)
        older = target(at=90)
        older["id"] = 2147483000  # Numerically bigger does not make a newer target.
        self.assertFalse(state.apply(older))
        newer = target(at=110)
        newer["id"] = -2000000000
        state.apply(newer)
        self.assertEqual(state.targets[1]["id"], -2000000000)

    def test_full_sdf_removes_missing_hits(self):
        state = RangeState()
        current = target(3, 103)
        current["source"] = "sdf"
        state.apply(current)
        update = target(1, 104)
        update["source"] = "sdf"
        state.apply(update)
        self.assertEqual(len(state.targets[1]["shots"]), 1)

    def test_practice_positions_series_and_concealment(self):
        state = RangeState()
        data = target(12, 112)
        practice = target(2, 120, position=0)
        data["shots"].update(practice["shots"])
        data["modified"] = 120
        state.apply(data)
        profile = Profile(id="test", name="Test", rows=[[1]])
        public = state.public(1, profile)
        self.assertTrue(public["practice"])
        self.assertEqual(public["scored_shot_count"], 12)
        self.assertEqual(public["shot_count"], 2)
        self.assertEqual(public["total"]["value"], 1200)
        profile.practice = False
        public = state.public(1, profile)
        self.assertEqual([s["number"] for s in public["shots"]], [11, 12])
        self.assertEqual(public["position"], 1)
        for mode in [None, 0, 5, 10]:
            state.targets[1] = target(mode=mode)
            public = state.public(1, profile)
            self.assertIsNone(public["total"])
            self.assertIsNone(public["latest"]["score"])
            self.assertIsNone(public["latest"]["whole_score"])
            if mode != 10:
                self.assertIsNone(public["shots"][0]["x_mm"])
                self.assertIsNone(public["shots"][0]["y_mm"])
            else:
                self.assertEqual(public["shots"][0]["x_mm"], 1.25)
        state.targets[1] = target()
        state.targets[1]["discipline_id"] = 9
        state.targets[1]["discipline"] = "Eigene Scheibe"
        self.assertIsNone(state.public(1, profile)["target_kind"])


class ProfileAndAPIChecks(unittest.TestCase):
    def test_shared_lana_reader_reuses_connection_and_read_only_command(self):
        stop = threading.Event()
        socket = MagicMock()
        socket.recv.side_effect = [json.dumps({"Prot": "MEWS", "VerP": 2, "SubProt": "LA", "VerSP": 2,
                                             "Rsp": "GetLaneInfo", "SeqNoRsp": sequence, "RV": 0,
                                             "Data": [{"LaneNo": 1, "Free": True}]}) for sequence in (1, 2)] + [
            json.dumps({"Prot": "MEWS", "VerP": 2, "SubProt": "LA", "VerSP": 2, "Rsp": "GetLaneInfo",
                        "SeqNoRsp": 3, "RV": -14}), ConnectionError("offline")]
        updates = []
        status = Mock()
        def report(source, state, message, **kwargs):
            status(source, state, message, **kwargs)
            if state == "disconnected":
                stop.set()
        with patch.dict(os.environ, {"SM_DB_HOST": "test"}), patch("app.sources.read_startlist_ids", return_value=[2]), \
             patch("app.sources.connect") as connect, patch.object(stop, "wait", return_value=False):
            connect.return_value.__enter__.return_value = socket
            LANAReader(stop, updates.append, report).run()
        connect.assert_called_once()
        self.assertEqual(len(updates), 3)
        self.assertEqual(updates[-1], {})
        self.assertEqual([call.args[1] for call in status.call_args_list], ["connected", "connected", "degraded", "disconnected"])
        commands = [json.loads(call.args[0]) for call in socket.send.call_args_list]
        self.assertEqual([c["Cmd"] for c in commands], ["GetLaneInfo"] * 4)
        self.assertEqual([c["SeqNo"] for c in commands], [1, 2, 3, 4])
        self.assertEqual(commands[0]["Data"], {"StartlistID": 2})

    def test_lana_discovers_event_changes_and_omits_conflicting_lanes(self):
        stop, socket, updates = threading.Event(), MagicMock(), []
        def response(sequence, entries, rv=0):
            return json.dumps({"Prot": "MEWS", "VerP": 2, "SubProt": "LA", "VerSP": 2,
                               "Rsp": "GetLaneInfo", "SeqNoRsp": sequence, "RV": rv, "Data": entries})
        socket.recv.side_effect = [response(1, None, -2), response(2, [{"LaneNo": 1, "Free": True}, {"LaneNo": 2, "Free": True}]),
                                  response(3, [{"LaneNo": 1, "Free": False, "Shooter": "Test, Ada"}, {"LaneNo": 2, "Free": True}]),
                                  response(4, [{"LaneNo": 1, "Free": True}, {"LaneNo": 3, "Free": True}])]
        def deliver(lanes):
            updates.append(lanes)
            if len(updates) == 2:
                stop.set()
        with patch.dict(os.environ, {"SM_DB_HOST": "test"}), patch("app.sources.connect") as connect, \
             patch("app.sources.read_startlist_ids", side_effect=[[2, 8], [12, 13]]), \
             patch("app.sources.time.monotonic", side_effect=[0, 5]), patch.object(stop, "wait", return_value=False):
            connect.return_value.__enter__.return_value = socket
            LANAReader(stop, deliver, Mock()).run()
        self.assertEqual(set(updates[0]), {1, 2})  # One invalid list does not block a valid one.
        self.assertEqual(set(updates[1]), {2, 3})  # Conflicting context responses stay unknown.
        self.assertEqual([json.loads(call.args[0])["Data"]["StartlistID"] for call in socket.send.call_args_list], [2, 8, 12, 13])
        connect.assert_called_once()

    def test_lana_recovers_when_database_initially_has_no_startlists(self):
        stop, socket, updates = threading.Event(), MagicMock(), []
        socket.recv.return_value = json.dumps({"Prot": "MEWS", "VerP": 2, "SubProt": "LA", "VerSP": 2,
                                               "Rsp": "GetLaneInfo", "SeqNoRsp": 1, "RV": 0,
                                               "Data": [{"LaneNo": 1, "Free": True}]})
        def deliver(lanes):
            updates.append(lanes)
            if lanes:
                stop.set()
        with patch.dict(os.environ, {"SM_DB_HOST": "test"}), patch("app.sources.connect") as connect, \
             patch("app.sources.read_startlist_ids", side_effect=[[], [42]]), \
             patch("app.sources.time.monotonic", side_effect=[0, 5]), patch.object(stop, "wait", return_value=False):
            connect.return_value.__enter__.return_value = socket
            LANAReader(stop, deliver, Mock()).run()
        self.assertEqual(updates[0], {})
        self.assertEqual(updates[1][1]["state"], "free")
        self.assertEqual(json.loads(socket.send.call_args.args[0])["Data"], {"StartlistID": 42})
        with patch("app.sources.database") as database:
            cursor = database.return_value.__enter__.return_value
            cursor.fetchall.return_value = [{"StarterlistenID": 2}, {"StarterlistenID": 42}]
            self.assertEqual(read_startlist_ids(), [2, 42])
            self.assertIn("StarterlistenID > 0", cursor.execute.call_args.args[0])

    def test_single_range_snapshot_validation_and_shared_watch(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "results.sqlite3"
            store = ResultStore(result_path)
            now = time.time()
            store.baseline([], now=now - 10)
            store.ingest([target(at=now)])
            store.heartbeat()
            app = create_app(Path(directory) / "profiles.json", result_path=result_path)
            with TestClient(app) as client:
                runtime = app.state.runtime
                response = client.get("/api/ranges/1")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["profile"]["rows"], [[1]])
                self.assertEqual(len(response.json()["rows"]), 1)
                self.assertEqual(response.json()["rows"][0][0]["target"]["shot_count"], 1)
                self.assertEqual(runtime.profiles.get("alles").rows, seeds()[0].rows)
                self.assertEqual(client.get("/api/ranges/77").status_code, 200)
                extra = target(at=now)
                extra["lane"] = 77
                extra["id"] = -77
                store.ingest([extra])
                self.wait_for(lambda: client.get("/api/ranges/77").json()["rows"][0][0]["target"])
                self.assertEqual(client.get("/api/ranges/77").json()["rows"][0][0]["target"]["lane"], 77)
                store.occupancy({77: {"state": "free", "shooter": "Unbekannt"}})
                store.source_status("lana", "connected", "ok")
                self.wait_for(lambda: client.get("/api/ranges/77").json()["rows"][0][0]["occupancy"] == "free")
                self.assertIsNone(client.get("/api/ranges/77").json()["rows"][0][0]["target"])
                for lane in (0, -1, 32768, "abc"):
                    self.assertEqual(client.get(f"/api/ranges/{lane}").status_code, 422)
                self.assertEqual(client.get("/api/events?lane=0").status_code, 422)
                self.assertEqual(client.get("/api/ranges/1?profile=missing").status_code, 404)
                self.assertFalse(hasattr(runtime, "requested_lanes"))
            store.close()

    @staticmethod
    def wait_for(check):
        deadline = time.monotonic() + 2
        while not check():
            if time.monotonic() >= deadline:
                raise AssertionError("SQLite watcher did not deliver committed state")
            time.sleep(0.01)

    def test_source_xml_failure_recovery_and_scan_failure(self):
        status = Mock()
        deliver = Mock()
        reader = SDFReader(threading.Event(), deliver, status)
        path = "sample.xml"
        file = MagicMock()
        file.__enter__.return_value.read.return_value = xml()[:100]
        reader.pending[path] = (time.monotonic(), 8)
        with patch("app.sources.smbclient.open_file", return_value=file), patch("app.sources.enrich_sdf"):
            reader.read_pending()
            self.assertEqual(status.call_args.args[1], "degraded")
            deliver.assert_not_called()
            reader.report(heartbeat=True)
            self.assertEqual(status.call_args.args[1], "degraded")
            file.__enter__.return_value.read.return_value = xml()
            reader.pending[path] = (time.monotonic(), 0)
            reader.read_pending()
            self.assertEqual(status.call_args.args[1], "connected")
            deliver.assert_called_once()
        with patch.object(reader, "scan", side_effect=OSError("offline")):
            reader.scan_loop(threading.Event())
        self.assertIsInstance(reader.scan_error, OSError)

    def test_local_sdf_scan_and_partial_write_recovery(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SM_SDF_DIRECTORY": directory}):
            path = Path(directory) / "nested" / "result.XML"
            path.parent.mkdir()
            path.write_bytes(xml())
            deliver, status = Mock(), Mock()
            reader = SDFReader(threading.Event(), deliver, status)
            done = threading.Event()
            with patch("app.sources.smbclient.scandir") as smb_scan, patch("app.sources.smbclient.open_file") as smb_open, patch("app.sources.enrich_sdf"):
                reader.scan(done, initial=True)
                self.assertTrue(reader.discovered.empty())  # Existing archives are not replayed.
                path.write_bytes(xml()[:100])
                reader.scan(done)
                self.assertEqual(reader.discovered.get(), str(path))
                reader.pending[str(path)] = (time.monotonic(), 8)
                reader.read_pending()
                self.assertEqual(status.call_args.args[1], "degraded")
                deliver.assert_not_called()
                path.write_bytes(xml())
                reader.scan(done)
                reader.pending[reader.discovered.get()] = (time.monotonic(), 0)
                reader.read_pending()
                self.assertEqual(deliver.call_args.args[0][0]["id"], -1)
                self.assertEqual(status.call_args.args[1], "connected")
                reader.scan(done)
                self.assertTrue(reader.discovered.empty())
                path.unlink()
                reader.pending[str(path)] = (time.monotonic(), 0)
                reader.read_pending()
                self.assertFalse(reader.pending)
                smb_scan.assert_not_called()
                smb_open.assert_not_called()

    def test_local_sdf_run_discovers_new_files_without_smb(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SM_SDF_DIRECTORY": directory, "SM_SMB_HOST": ""}):
            stop, ready, delivered = threading.Event(), threading.Event(), threading.Event()
            reader = SDFReader(stop, lambda records: delivered.set(), lambda *args, **kwargs: ready.set())
            with patch("app.sources.enrich_sdf"), patch("app.sources.smbclient.register_session") as smb:
                thread = threading.Thread(target=reader.run, daemon=True)
                thread.start()
                try:
                    self.assertTrue(ready.wait(2))
                    (Path(directory) / "new.xml").write_bytes(xml())
                    self.assertTrue(delivered.wait(2))
                    smb.assert_not_called()
                finally:
                    stop.set()
                    thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_local_sdf_missing_directory_reports_disconnected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SM_SDF_DIRECTORY": str(Path(directory) / "missing")}):
            stop, status = threading.Event(), Mock()
            status.side_effect = lambda *args, **kwargs: stop.set()
            SDFReader(stop, Mock(), status).run()
            self.assertEqual(status.call_args.args[1], "disconnected")

    def test_atomic_profiles_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            store = ProfileStore(path)
            self.assertEqual(store.get("alles").rows, seeds()[0].rows)
            self.assertFalse(store.get("alles").discipline_inline)
            self.assertEqual(store.get("alles").shot_highlight, "none")
            self.assertFalse(store.get("alles").hide_unavailable)
            self.assertFalse(store.get("alles").confetti_enabled)
            self.assertEqual(store.get("alles").confetti_threshold, 10.5)
            for changes in [{"rows": [[1], [1]]}, {"rows": [[]]}, {"rows": [[0]]}, {"name": " "}, {"theme": {"text": "url(bad)"}}, {"discipline_inline": "sideways"}, {"shot_highlight": "flash"}, {"hide_unavailable": "maybe"}]:
                with self.assertRaises(ValueError):
                    Profile.model_validate({**store.profiles[0].model_dump(), **changes})
            before = path.read_bytes()
            for value in (-0.1, 11, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    Profile.model_validate({**store.profiles[0].model_dump(), "confetti_threshold": value})
            for value in (0, 10.5, 10.9):
                self.assertEqual(Profile.model_validate({**store.profiles[0].model_dump(), "confetti_threshold": value}).confetti_threshold, value)
            with patch("app.profiles.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    store.save(store.profiles[:1])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            store.profiles[0].name = "Updated"
            store.profiles[0].discipline_inline = True
            store.profiles[0].shot_highlight = "border"
            store.profiles[0].hide_unavailable = True
            store.profiles[0].confetti_enabled = True
            store.profiles[0].confetti_threshold = 10.9
            store.save(store.profiles)
            self.assertEqual(ProfileStore(path).get("alles").name, "Updated")
            self.assertTrue(ProfileStore(path).get("alles").discipline_inline)
            self.assertEqual(ProfileStore(path).get("alles").shot_highlight, "border")
            self.assertTrue(ProfileStore(path).get("alles").hide_unavailable)
            self.assertTrue(ProfileStore(path).get("alles").confetti_enabled)
            self.assertEqual(ProfileStore(path).get("alles").confetti_threshold, 10.9)

    def test_api_auth_persistence_and_source_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SM_ADMIN_PASSWORD": "test-password"}):
            path = Path(directory) / "profiles.json"
            result_path = Path(directory) / "results.sqlite3"
            store = ResultStore(result_path)
            now = time.time()
            store.baseline([], now=now - 10)
            store.ingest([target(at=now)])
            store.heartbeat()
            app = create_app(path, result_path=result_path)
            with TestClient(app) as client:
                self.assertEqual(client.get("/healthz").status_code, 200)
                self.assertEqual(client.get("/api/admin/auth").status_code, 401)
                self.assertEqual(client.get("/api/admin/settings/resource-saver").status_code, 401)
                self.assertEqual(client.get("/api/admin/profiles").status_code, 401)
                profiles = client.get("/api/profiles").json()
                profile = {**profiles[0], "id": "test", "name": "Test"}
                self.assertEqual(client.post("/api/admin/profiles", json=profile).status_code, 401)
                auth = ("admin", "test-password")
                resource = client.get("/api/admin/settings/resource-saver", auth=auth).json()
                self.assertEqual(resource["settings"], {"enabled": False, "poll_interval_seconds": 10})
                saved_resource = client.put("/api/admin/settings/resource-saver", auth=auth,
                    json={"enabled": True, "poll_interval_seconds": 15}).json()
                self.assertEqual(saved_resource["settings"], {"enabled": True, "poll_interval_seconds": 15})
                self.assertEqual(client.put("/api/admin/settings/resource-saver", auth=auth,
                    json={"enabled": True, "poll_interval_seconds": 1}).status_code, 422)
                self.assertEqual(client.post("/api/admin/profiles", json=profile, auth=auth).status_code, 201)
                self.assertEqual(client.post("/api/admin/profiles", json=profile, auth=auth).status_code, 409)
                self.assertEqual(client.put("/api/admin/profiles/test", json=profile, auth=auth, headers={"Origin": "https://other.invalid"}).status_code, 403)
                self.assertEqual(client.delete("/api/admin/profiles/test", auth=auth, headers={"Content-Type": "application/json"}).status_code, 204)
                runtime = app.state.runtime
                draft = {**profile, "rows": [[1, 55]], "hits": "all"}
                preview = client.post("/api/admin/profiles/preview", json=draft, auth=auth)
                self.assertEqual(preview.status_code, 200)
                self.assertEqual([entry["lane"] for entry in preview.json()["rows"][0]], [1, 55])
                self.assertIsNone(runtime.profiles.get("test"))  # Preview does not persist.
                store.source_status("db", "connected", "ok")
                self.wait_for(lambda: runtime.status["db"]["state"] == "connected")
                revision = store.connection.execute("SELECT revision FROM meta").fetchone()[0]
                store.source_status("db", "connected", "ok")
                self.assertEqual(store.connection.execute("SELECT revision FROM meta").fetchone()[0], revision)
                before = client.get("/api/snapshot").json()
                runtime_id = before["runtime_id"]
                self.assertTrue(runtime_id)
                self.assertEqual(client.get("/api/ranges/1").json()["runtime_id"], runtime_id)
                self.assertEqual(preview.json()["runtime_id"], runtime_id)
                store.source_status("db", "disconnected", "offline")
                self.wait_for(lambda: runtime.status["db"]["state"] == "disconnected")
                after = client.get("/api/snapshot").json()
                self.assertEqual(after["runtime_id"], runtime_id)
                self.assertEqual(before["rows"], after["rows"])
                self.assertEqual(after["sources"]["db"]["state"], "disconnected")
                self.assertEqual(after["sources"]["db"]["last_check_at"], before["sources"]["db"]["last_check_at"])
                self.assertEqual(client.get("/api/snapshot?profile=missing").status_code, 404)
            self.assertEqual(ProfileStore(path).get("alles").id, "alles")
            with TestClient(app) as client:
                self.assertNotEqual(client.get("/api/snapshot").json()["runtime_id"], runtime_id)
            store.close()


if __name__ == "__main__":
    unittest.main()
