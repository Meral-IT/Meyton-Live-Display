"""Live demo worker using the same SQLite store as real ingestion; no vendor I/O."""

import math
import os
import random
import signal
import threading
import time
from pathlib import Path

from .domain import score
from .profiles import ProfileStore, seeds
from .storage import ResultStore

NAMES = ("Becker, Anna", "Berg, Leon", "Fischer, Emma", "Klein, Paul", "Koch, Mia",
         "Lang, Felix", "Meyer, Lena", "Neumann, Jonas", "Weber, Clara", "Wolf, Ben")
# Match the verified SVG geometry; these scores belong only to synthetic results.
TARGETS = {"0111": ("LG Auflage 30", 10111030, 30, 2.5, 2.5, 0.8),
           "1211": ("KK 5P+10W", 41211010, 10, 8.0, 8.0, 3.2)}


class DemoWorker:
    def __init__(self, database_path, profile_path, *, interval=3, seed=42):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("SM_DEMO_SHOT_INTERVAL must be a positive, finite number")
        self.store = ResultStore(database_path)
        self.profile_path = Path(profile_path)
        self.interval = interval
        self.random = random.Random(seed)
        self.sessions = {}
        self.occupancies = {}
        self.profile_stamp = None
        # IDs below signed 32-bit vendor IDs cannot collide with Meyton targets.
        minima = [self.store.connection.execute(f"SELECT MIN(id) FROM {table}").fetchone()[0]
                  for table in ("targets", "baselines")]
        self.next_id = min([-2**32] + [value for value in minima if value is not None]) - 1
        self.store.start_sources(("demo",))
        self.heartbeat_due = self.cleanup_due = 0

    def new_session(self, lane, kind, now):
        name = self.random.choice(NAMES) + " (Demo)"
        discipline, discipline_id, limit, _, _, spread = TARGETS[kind]
        target = {"id": self.next_id, "lane": lane, "shooter": name,
                  "discipline": discipline, "discipline_id": discipline_id, "modified": now,
                  "shots": {}, "series": {}, "total": score(0), "decimal_total": score(0, "ZehntelRing"),
                  "source": "demo", "full": True}
        self.next_id -= 1
        self.sessions[lane] = {"target": target, "kind": kind, "limit": limit, "count": 0,
                               "phase": "shooting", "due": now,
                               "spread": spread * self.random.uniform(0.8, 1.6),
                               "offset": (self.random.gauss(0, spread / 3), self.random.gauss(0, spread / 3))}
        self.occupancies[lane] = {"state": "occupied", "shooter": name}

    def profiles(self, now):
        stat = self.profile_path.stat() if self.profile_path.exists() else None
        stamp = (stat.st_mtime_ns, stat.st_size) if stat else "defaults"
        if stamp == self.profile_stamp:
            return
        profiles = ProfileStore(self.profile_path).profiles if stat else seeds()
        lanes = {lane for profile in profiles for row in profile.rows for lane in row}
        small = {lane for profile in profiles if profile.id == "kleinkaliber" for row in profile.rows for lane in row}
        for lane in set(self.sessions) - lanes:
            del self.sessions[lane]
            self.occupancies[lane] = {"state": "free", "shooter": "Unbekannt"}
        for lane in sorted(lanes):
            kind = "1211" if lane in small else "0111"
            if lane not in self.sessions or self.sessions[lane]["kind"] != kind:
                self.new_session(lane, kind, now)
        self.profile_stamp = stamp

    def shot(self, session, now):
        session["count"] += 1
        count = session["count"]
        position, number = (0, count) if count <= 5 else (1, count - 5)
        spread = session["spread"] * (1.4 if position == 0 else 1)
        x, y = (round(self.random.gauss(offset, spread), 2) for offset in session["offset"])
        radius = math.hypot(x, y)
        _, _, _, ten_radius, ring_width, _ = TARGETS[session["kind"]]
        whole = max(0, min(10, 10 - math.ceil((radius - ten_radius) / ring_width)))
        outer = ten_radius + (10 - whole) * ring_width
        decimal = whole * 10 + max(0, min(9, int((outer - radius) / (ring_width / 10)))) if whole else 0
        target = session["target"]
        target["shots"][(position, number)] = {
            "position": position, "number": number, "series": (number - 1) // 10 + 1,
            "x_mm": x, "y_mm": y, "score": score(decimal, "ZehntelRing"), "whole_score": score(whole * 10),
            "mode": 2, "inner_ten": radius <= ten_radius / 2, "invalid": False, "time": now,
        }
        scored = [s for s in target["shots"].values() if s["position"] != 0]
        target.update(modified=now, received_at=now,
                      total=score(sum(s["whole_score"]["value"] for s in scored)),
                      decimal_total=score(sum(s["score"]["value"] for s in scored), "ZehntelRing"))
        key = (position, (number - 1) // 10 + 1)
        hits = [s for s in target["shots"].values() if (s["position"], s["series"]) == key]
        target["series"][key] = {"position": key[0], "number": key[1],
                                 "score": score(sum(s["whole_score"]["value"] for s in hits) // 10, scale=1),
                                 "decimal_score": score(sum(s["score"]["value"] for s in hits), "ZehntelRing")}
        session["due"] = now + self.interval * self.random.uniform(0.7, 1.3)
        if count == 5 + session["limit"]:
            session.update(phase="finished", due=now + 8)

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.profiles(now)
        updates = []
        for lane, session in list(self.sessions.items()):
            if now < session["due"]:
                continue
            if session["phase"] == "finished":
                self.occupancies[lane] = {"state": "free", "shooter": "Unbekannt"}
                session.update(phase="free", due=now + 5)
            elif session["phase"] == "free":
                self.new_session(lane, session["kind"], now)
                self.shot(self.sessions[lane], now)
                updates.append(self.sessions[lane]["target"])
            else:
                self.shot(session, now)
                updates.append(session["target"])
        self.store.occupancy(self.occupancies)
        if updates:
            self.store.ingest(updates)
        if now >= self.heartbeat_due:
            self.store.source_status("demo", "connected", "Demobetrieb · simulierte Treffer, keine Meyton-Verbindung", now=now)
            self.store.heartbeat(now)
            self.heartbeat_due = now + 5
        if now >= self.cleanup_due:
            self.store.cleanup(now)
            self.cleanup_due = now + 60


def main():
    worker = DemoWorker(os.getenv("RESULT_DB_PATH", "/results/results.sqlite3"),
                        os.getenv("PROFILE_PATH", "/profiles/profiles.json"),
                        interval=float(os.getenv("SM_DEMO_SHOT_INTERVAL", "3")))
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    print("Demo worker started: simulated results in the shared SQLite database", flush=True)
    try:
        while not stop.is_set():
            try:
                worker.tick()
            except (ValueError, OSError) as error:
                worker.store.source_status("demo", "degraded", f"Demoprofile nicht lesbar ({type(error).__name__})")
                worker.store.heartbeat()
                stop.wait(1)
            stop.wait(0.1)
    finally:
        worker.store.close()


if __name__ == "__main__":
    main()
