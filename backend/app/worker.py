"""Standalone ingestion worker. Source threads queue batches to one SQLite writer."""

import argparse
import collections
import json
import os
import queue
import signal
import threading
import time

from .storage import ResultStore, connect_reader


class Worker:
    def __init__(self, path):
        sdf_enabled = os.getenv("SM_SDF_ENABLED", "true").strip().lower()
        if sdf_enabled not in ("true", "false"):
            raise ValueError("SM_SDF_ENABLED must be true or false")
        self.sdf_enabled = sdf_enabled == "true"
        self.store = ResultStore(path)
        self.stop = threading.Event()
        self.ready = threading.Event()
        if self.store.started_at is not None:
            self.ready.set()
        # Backpressure bounds memory; a complete batch uses one SQLite transaction.
        self.queue = queue.Queue(maxsize=256)
        self.latest = {}
        self.pending = collections.deque()
        self.pending_ids = set()
        self.history_cursor = -(2**31) - 1
        self.history_due = 0
        self.status_lock = threading.Lock()
        self.status_seen = {}

    def enqueue(self, kind, data):
        while not self.stop.is_set():
            try:
                self.queue.put((kind, data), timeout=0.1)
                return True
            except queue.Full:
                pass
        return False

    def status(self, source, state, message, **kwargs):
        now = time.monotonic()
        with self.status_lock:
            previous = self.status_seen.get(source)
            if previous and previous[:2] == (state, message) and now - previous[2] < 5:
                return
            self.status_seen[source] = (state, message, now)
        self.enqueue("status", (source, state, message))

    def index(self, rows):
        first = self.store.started_at is None
        self.store.baseline(rows)
        self.ready.set()
        now = time.time()
        for row in rows:
            old = self.latest.get(row["lane"])
            if old and old["id"] == row["id"] and row["last_shot"] is None:
                row = {**row, "last_shot": old["last_shot"]}
            key = row["last_shot"] if row["last_shot"] is not None else row["modified"] or 0
            old_key = (old["last_shot"] if old["last_shot"] is not None else old["modified"] or 0) if old else -1
            if old is None or key > old_key or old["id"] == row["id"]:
                self.latest[row["lane"]] = row
            if not first and self.store.eligible(row["id"], row["last_shot"], row["modified"], now) and row["id"] not in self.pending_ids:
                self.pending.append(row["id"])
                self.pending_ids.add(row["id"])
        live = {row["id"] for row in self.latest.values()
                if self.store.eligible(row["id"], row["last_shot"], row["modified"], now)}
        background = set()
        while self.pending and len(background) < 50:
            target_id = self.pending.popleft()
            self.pending_ids.discard(target_id)
            if target_id not in live:
                background.add(target_id)
        if time.monotonic() >= self.history_due and len(background) < 50:
            retained = self.store.connection.execute("SELECT id FROM targets WHERE id>? ORDER BY id LIMIT ?",
                                                      (self.history_cursor, 50 - len(background))).fetchall()
            self.history_cursor = retained[-1][0] if retained else -(2**31) - 1
            background.update(row[0] for row in retained if row[0] not in live)
            self.history_due = time.monotonic() + 10
        return live, background

    def poll(self):
        from .sources import read_db, read_db_index

        if not os.getenv("SM_DB_HOST"):
            self.status("db", "disabled", "SM_DB_HOST fehlt")
            return
        since = None
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                rows = read_db_index(since)
                response = queue.Queue(maxsize=1)
                if not self.enqueue("index", (rows, response)):
                    return
                while not self.stop.is_set():
                    try:
                        live, background = response.get(timeout=0.1)
                        break
                    except queue.Empty:
                        pass
                else:
                    return
                watermark = max((max(r["modified"] or 0, r["last_shot"] or 0) for r in rows), default=0)
                if watermark:
                    since = max(since or 0, watermark - 5)
                for offset in range(0, len(live), 50):
                    targets = read_db(ids=sorted(live)[offset:offset + 50])
                    self.enqueue("targets", targets)
                # Live reads and callbacks stay ahead of bounded historical reconciliation.
                if background:
                    self.enqueue("targets", read_db(ids=background))
                self.status("db", "connected", "SSMDB2 verbunden")
            except Exception as error:
                since = None  # Re-discover targets missed during a failed read or disconnect.
                self.status("db", "disconnected", f"DB nicht erreichbar ({type(error).__name__})")
            self.stop.wait(max(0, 1 - (time.monotonic() - started)))

    def after_baseline(self, run):
        while not self.stop.is_set():
            if self.ready.wait(0.1):
                run()
                return

    def run(self):
        from .sources import LANAReader, SDFReader

        self.store.cleanup()
        self.store.start_sources(("db", "sdf", "lana"))
        self.store.heartbeat()
        lana = LANAReader(self.stop, lambda lanes: self.enqueue("occupancy", lanes), self.status)
        sources = [self.poll, lambda: self.after_baseline(lana.run)]
        if self.sdf_enabled:
            sdf = SDFReader(self.stop, lambda targets: self.enqueue("targets", targets), self.status)
            sources.append(lambda: self.after_baseline(sdf.run))
        else:
            self.store.source_status("sdf", "disabled", "SDF deaktiviert (SM_SDF_ENABLED=false)")
        threads = [threading.Thread(target=run, daemon=True) for run in sources]
        for thread in threads:
            thread.start()
        heartbeat_due, cleanup_due = time.monotonic() + 5, time.monotonic() + 60
        try:
            while not self.stop.is_set():
                try:
                    kind, data = self.queue.get(timeout=0.1)
                    if kind == "targets":
                        self.store.ingest(data)
                    elif kind == "occupancy":
                        self.store.occupancy(data)
                    elif kind == "status":
                        self.store.source_status(*data)
                    elif kind == "index":
                        rows, response = data
                        response.put(self.index(rows))
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now >= heartbeat_due:
                    self.store.heartbeat()
                    heartbeat_due = now + 5
                if now >= cleanup_due:
                    self.store.cleanup()
                    cleanup_due = now + 60
        finally:
            self.stop.set()
            for thread in threads:
                thread.join(timeout=2)
            self.store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--backup", metavar="PATH", help="Write a consistent SQLite backup without stopping collection")
    args = parser.parse_args()
    path = os.getenv("RESULT_DB_PATH", "/results/results.sqlite3")
    if args.healthcheck or args.backup:
        connection = connect_reader(path)
        try:
            if args.backup:
                import sqlite3
                with sqlite3.connect(args.backup) as backup:
                    connection.backup(backup)
                print("Result database backup complete")
            else:
                heartbeat = connection.execute("SELECT heartbeat FROM meta WHERE id=1").fetchone()[0]
                if time.time() - heartbeat > 15:
                    raise SystemExit("Worker heartbeat is stale")
        finally:
            connection.close()
        return
    worker = Worker(path)
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: worker.stop.set())
    print(json.dumps({"message": "Result worker started", "retention_days": worker.store.days}), flush=True)
    worker.run()


if __name__ == "__main__":
    main()
