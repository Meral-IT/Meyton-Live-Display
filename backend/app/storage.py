"""SQLite result storage: one worker writer, read-only display connections."""

import copy
import json
import os
import sqlite3
import time
from pathlib import Path

from .domain import RangeState, iso, last_hit, target_activity


def retention_days(value=None):
    value = os.getenv("SM_RETENTION_DAYS", "7") if value is None else value
    if not str(value).isdigit() or int(value) < 1:
        raise ValueError("SM_RETENTION_DAYS must be a positive integer")
    return int(value)


def encode(target):
    return json.dumps({**target, "shots": [target["shots"][key] for key in sorted(target["shots"])],
                       "series": [target["series"][key] for key in sorted(target["series"])]},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode(payload):
    target = json.loads(payload)
    target["shots"] = {(s["position"], s["number"]): s for s in target["shots"]}
    target["series"] = {(s["position"], s["number"]): s for s in target["series"]}
    return target


def connect_reader(path):
    connection = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def read_snapshot(connection, now=None):
    now = time.time() if now is None else now
    # Keep the revision, pointers and targets in the same short read transaction.
    with connection:
        connection.execute("BEGIN")
        meta = dict(connection.execute("SELECT * FROM meta WHERE id=1").fetchone())
        rows = connection.execute(
            "SELECT r.*, t.payload FROM ranges r LEFT JOIN targets t ON t.id=r.target_id AND t.activity_at>=?",
            (now - meta["retention_days"] * 86400,)).fetchall()
    state = RangeState()
    for row in rows:
        lane = row["lane"]
        if row["payload"]:
            state.targets[lane] = decode(row["payload"])
        if row["occupancy"] != "unknown":
            state.occupancy[lane] = row["occupancy"]
        if row["live_shooter"] is not None:
            state.live_shooters[lane] = row["live_shooter"]
        if row["cleared_id"] is not None:
            state.cleared[lane] = (row["cleared_id"], row["cleared_shot"])
        elif row["occupancy"] == "free":
            state.cleared[lane] = None
    return state, json.loads(meta["sources"]), meta


class ResultStore:
    def __init__(self, path, days=None):
        days = retention_days(days)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise ValueError("Unsupported local result database version")
        if version == 1:
            with self.connection:
                self.connection.execute("ALTER TABLE meta ADD COLUMN stand_changed_at REAL NOT NULL DEFAULT 0")
                self.connection.execute("UPDATE meta SET stand_changed_at=? WHERE id=1", (time.time(),))
                self.connection.execute("PRAGMA user_version=2")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0,
                heartbeat REAL NOT NULL DEFAULT 0, collection_started_at REAL,
                retention_days INTEGER NOT NULL, sources TEXT NOT NULL,
                stand_changed_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS baselines (id INTEGER PRIMARY KEY, shot_after REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY, lane INTEGER NOT NULL, activity_at REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS targets_expiry ON targets(activity_at);
            CREATE TABLE IF NOT EXISTS ranges (
                lane INTEGER PRIMARY KEY, target_id INTEGER REFERENCES targets(id) ON DELETE SET NULL,
                occupancy TEXT NOT NULL DEFAULT 'unknown', live_shooter TEXT,
                cleared_id INTEGER, cleared_shot REAL);
            PRAGMA user_version=2;
        """)
        sources = {name: {"state": "starting", "message": "Verbindung wird hergestellt", "last_check_at": None}
                   for name in ("db", "sdf", "lana")}
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO meta(id,retention_days,sources,stand_changed_at) VALUES(1,?,?,?)",
                                    (days, json.dumps(sources), time.time()))
            self.connection.execute("UPDATE meta SET retention_days=?,revision=revision+1 WHERE id=1 AND retention_days!=?",
                                    (days, days))
        self.days = days
        self.ranges, self.sources, meta = read_snapshot(self.connection)
        self.started_at = meta["collection_started_at"]
        self.stand_changed_at = meta["stand_changed_at"]
        self._checked_at = {}

    def close(self):
        self.connection.close()

    def baseline(self, index, now=None):
        if self.started_at is not None:
            return
        now = time.time() if now is None else now
        with self.connection:
            self.connection.executemany("INSERT INTO baselines(id,shot_after) VALUES(?,?)",
                                        [(row["id"], row["last_shot"] or 0) for row in index])
            self.connection.execute("UPDATE meta SET collection_started_at=?,revision=revision+1 WHERE id=1", (now,))
        self.started_at = now

    def eligible(self, target_id, last_shot, modified=None, now=None):
        now = time.time() if now is None else now
        activity = last_shot if last_shot is not None else modified
        if self.started_at is None or activity is None or activity < now - self.days * 86400:
            return False
        if self.connection.execute("SELECT 1 FROM targets WHERE id=?", (target_id,)).fetchone():
            return True
        baseline = self.connection.execute("SELECT shot_after FROM baselines WHERE id=?", (target_id,)).fetchone()
        if baseline:
            return last_shot is not None and last_shot > baseline[0]
        return activity >= self.started_at

    def target(self, target_id):
        row = self.connection.execute("SELECT payload FROM targets WHERE id=?", (target_id,)).fetchone()
        return decode(row[0]) if row else None

    def _save_ranges(self):
        lanes = set(self.ranges.targets) | set(self.ranges.occupancy) | set(self.ranges.cleared)
        lanes |= {row[0] for row in self.connection.execute("SELECT lane FROM ranges")}
        for lane in lanes:
            clear = self.ranges.cleared.get(lane) or (None, None)
            values = (lane, self.ranges.targets.get(lane, {}).get("id"), self.ranges.occupancy.get(lane, "unknown"),
                      self.ranges.live_shooters.get(lane), *clear)
            self.connection.execute("""
                INSERT INTO ranges VALUES(?,?,?,?,?,?) ON CONFLICT(lane) DO UPDATE SET
                target_id=excluded.target_id,occupancy=excluded.occupancy,live_shooter=excluded.live_shooter,
                cleared_id=excluded.cleared_id,cleared_shot=excluded.cleared_shot
                WHERE target_id IS NOT excluded.target_id OR occupancy IS NOT excluded.occupancy
                OR live_shooter IS NOT excluded.live_shooter OR cleared_id IS NOT excluded.cleared_id
                OR cleared_shot IS NOT excluded.cleared_shot
            """, values)

    def ingest(self, targets, now=None):
        now = time.time() if now is None else now
        changed = False
        # A source batch and its range pointers become visible in one commit.
        with self.connection:
            for incoming in targets:
                hit = last_hit(incoming)
                if incoming["source"] != "demo" and not self.eligible(incoming["id"], hit and hit["time"], incoming["modified"], now):
                    continue
                previous = self.target(incoming["id"])
                incoming = copy.deepcopy(incoming)
                seed = incoming.pop("_db_snapshot", None)
                merged = RangeState()
                if previous:
                    merged.targets[incoming["lane"]] = previous
                elif seed:
                    merged.apply(seed)
                    for key, shot in incoming["shots"].items():
                        seeded = merged.targets[incoming["lane"]]["shots"].get(key)
                        if seeded and seeded["time"] == shot["time"] and incoming.get("received_at"):
                            seeded.update(received_at=incoming["received_at"], received_source=incoming["source"])
                if not merged.apply(incoming) and previous:
                    continue
                target = merged.targets[incoming["lane"]]
                target.pop("received_at", None)
                if previous and encode(target) == encode(previous):
                    continue
                for key, shot in target["shots"].items():
                    before = previous["shots"].get(key) if previous else None
                    shot["persisted_at"] = before.get("persisted_at", now) if before and {
                        k: v for k, v in shot.items() if k != "persisted_at"} == {
                        k: v for k, v in before.items() if k != "persisted_at"} else now
                activity = target_activity(target)
                self.connection.execute("INSERT INTO targets VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                                        "lane=excluded.lane,activity_at=excluded.activity_at,payload=excluded.payload",
                                        (target["id"], target["lane"], activity, encode(target)))
                current = self.ranges.targets.get(target["lane"])
                if current is None or current["id"] == target["id"] or (target_activity(target) or 0) > (target_activity(current) or 0):
                    # The merged record already contains receipt times and enrichment.
                    self.ranges.targets[target["lane"]] = copy.deepcopy(target)
                    if self.ranges.occupancy.get(target["lane"]) == "free" and self.ranges.cleared.get(target["lane"]) is None:
                        self.ranges.cleared[target["lane"]] = (target["id"], (last_hit(target) or {}).get("time") or 0)
                changed = True
            if changed:
                self._save_ranges()
                self.connection.execute("UPDATE meta SET revision=revision+1,stand_changed_at=? WHERE id=1", (now,))
                self.stand_changed_at = now
        return changed

    def occupancy(self, lanes, now=None):
        if not self.ranges.set_occupancy(lanes):
            return False
        now = time.time() if now is None else now
        with self.connection:
            self._save_ranges()
            self.connection.execute("UPDATE meta SET revision=revision+1,stand_changed_at=? WHERE id=1", (now,))
        self.stand_changed_at = now
        return True

    def source_status(self, source, state, message, now=None):
        now = time.time() if now is None else now
        old = self.sources[source]
        if (state, message) == (old["state"], old["message"]):
            checked = old["last_check_at"]
            if state != "connected" or checked and now - self._checked_at.get(source, 0) < 5:
                return
        self._checked_at[source] = now
        self.sources[source] = {"state": state, "message": message,
                                "last_check_at": iso(now) if state == "connected" else old["last_check_at"]}
        with self.connection:
            self.connection.execute("UPDATE meta SET sources=?,revision=revision+1 WHERE id=1",
                                    (json.dumps(self.sources, ensure_ascii=False),))

    def start_sources(self, names):
        self.sources = {name: {"state": "starting", "message": "Verbindung wird hergestellt",
                               "last_check_at": self.sources.get(name, {}).get("last_check_at")} for name in names}
        self._checked_at = {}
        with self.connection:
            self.connection.execute("UPDATE meta SET sources=?,revision=revision+1 WHERE id=1",
                                    (json.dumps(self.sources, ensure_ascii=False),))

    def heartbeat(self, now=None):
        with self.connection:
            self.connection.execute("UPDATE meta SET heartbeat=? WHERE id=1", (time.time() if now is None else now,))

    def cleanup(self, now=None):
        now = time.time() if now is None else now
        cutoff = now - self.days * 86400
        with self.connection:
            # Minimal watermarks outlive results, preventing later re-import of expired archives.
            expired = self.connection.execute("SELECT id,activity_at FROM targets WHERE activity_at<?", (cutoff,)).fetchall()
            self.connection.executemany("INSERT INTO baselines VALUES(?,?) ON CONFLICT(id) DO UPDATE SET "
                                        "shot_after=MAX(shot_after,excluded.shot_after)", [(r[0], r[1]) for r in expired])
            self.connection.execute("DELETE FROM targets WHERE activity_at<?", (cutoff,))
            if expired:
                self.connection.execute("UPDATE meta SET revision=revision+1 WHERE id=1")
        for lane, target in list(self.ranges.targets.items()):
            activity = (last_hit(target) or {}).get("time")
            activity = target["modified"] if activity is None else activity
            if activity is None or activity < cutoff:
                del self.ranges.targets[lane]
        return len(expired)
