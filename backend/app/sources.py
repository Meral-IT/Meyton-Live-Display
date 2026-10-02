"""Read-only SQL snapshots and recursive SMB change notifications."""

import collections
import json
import os
import queue
import threading
import time
from contextlib import contextmanager

import pymysql
import smbclient
from smbprotocol.change_notify import ChangeNotifyFlags, CompletionFilter, FileSystemWatcher
from smbprotocol.connection import Connection
from smbprotocol.open import (CreateDisposition, CreateOptions, DirectoryAccessMask,
                              FileAttributes, ImpersonationLevel, Open, ShareAccess)
from smbprotocol.session import Session
from smbprotocol.tree import TreeConnect
from uuid import uuid4
from websockets.sync.client import connect

from datetime import datetime
from zoneinfo import ZoneInfo

from .domain import MAX_XML_BYTES, from_db, parse_sdf, shooter_name, stamp


def lana_lanes(response, sequence):
    if (response.get("Prot"), response.get("VerP"), response.get("SubProt"), response.get("VerSP"),
        response.get("Rsp"), response.get("SeqNoRsp")) != ("MEWS", 2, "LA", 2, "GetLaneInfo", sequence):
        raise ValueError("Unexpected LANA response")
    if response.get("RV") != 0:
        # No free lanes (-14) doesn't identify occupied or offline lanes.
        raise ValueError("LANA did not supply lane status")
    data = response.get("Data")
    if not isinstance(data, list) or len(data) > 32767:
        raise ValueError("Invalid LANA lane list")
    lanes = {}
    for entry in data:
        lane, free = entry.get("LaneNo"), entry.get("Free")
        if type(lane) is not int or not 1 <= lane <= 32767 or type(free) is not bool or lane in lanes:
            raise ValueError("Invalid LANA lane status")
        name = entry.get("Shooter")
        if name is not None and (not isinstance(name, str) or len(name) > 512):
            raise ValueError("Invalid LANA shooter name")
        family, _, given = (name or "").partition(",")
        lanes[lane] = {"state": "free" if free else "occupied", "shooter": shooter_name(family, given)}
    return lanes


class LANAReader:
    def __init__(self, stop, deliver, status):
        self.stop, self.deliver, self.status = stop, deliver, status

    def run(self):
        host = os.getenv("SM_LANA_HOST") or os.getenv("SM_DB_HOST")
        if not host:
            self.status("lana", "disabled", "SM_DB_HOST fehlt")
            return
        port = int(os.getenv("SM_LANA_PORT", "53088"))
        sequence = 0
        startlists, refresh_at = [], 0
        while not self.stop.is_set():
            try:
                # One shared persistent connection; only the read-only GetLaneInfo command.
                with connect(f"ws://{host}:{port}", proxy=None, compression=None, open_timeout=5,
                             close_timeout=1, max_size=128 * 1024, max_queue=4) as socket:
                    while not self.stop.is_set():
                        now = time.monotonic()
                        if now >= refresh_at:
                            try:
                                startlists = read_startlist_ids()
                            except Exception:
                                pass  # Keep discovered IDs usable while SQL recovers.
                            refresh_at = now + 5
                        lanes, conflicts, confirmed = {}, set(), False
                        for startlist in startlists:
                            if self.stop.is_set():
                                break
                            sequence += 1
                            socket.send(json.dumps({"Prot": "MEWS", "VerP": 2, "SubProt": "LA", "VerSP": 2,
                                                    "SeqNo": sequence, "Cmd": "GetLaneInfo",
                                                    "Data": {"StartlistID": startlist}}))
                            try:
                                incoming = lana_lanes(json.loads(socket.recv(timeout=5)), sequence)
                            except ValueError:
                                continue  # A deleted/invalid event must not block other contexts.
                            confirmed = True
                            for lane, entry in incoming.items():
                                if lane in lanes and lanes[lane] != entry:
                                    conflicts.add(lane)
                                lanes[lane] = entry
                        self.deliver({lane: entry for lane, entry in lanes.items() if lane not in conflicts})
                        if confirmed:
                            self.status("lana", "connected", "Live-Belegung teilweise bestätigt" if conflicts else "Live-Belegung verbunden", heartbeat=True)
                        else:
                            self.status("lana", "degraded", "Belegung nicht bestätigt" if startlists else "Warte auf Starterlisten in SSMDB2", heartbeat=True)
                        self.stop.wait(1)
            except Exception as error:
                self.status("lana", "disconnected", f"Belegung nicht erreichbar ({type(error).__name__})")
                self.stop.wait(3)


@contextmanager
def database():
    connection = pymysql.connect(
        host=os.environ["SM_DB_HOST"], user=os.environ["SM_DB_USER"],
        password=os.environ["SM_DB_PASS"], database=os.getenv("SM_DB_NAME", "SSMDB2"),
        port=int(os.getenv("SM_DB_PORT", "3306")), charset="utf8mb4",
        connect_timeout=5, read_timeout=5, write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            yield cursor
    finally:
        connection.rollback()
        connection.close()


def read_startlist_ids():
    """Discover available query contexts; result age does not prove an active event."""
    with database() as cursor:
        cursor.execute("SELECT DISTINCT StarterlistenID FROM Scheiben WHERE StarterlistenID > 0 ORDER BY StarterlistenID")
        return [int(row["StarterlistenID"]) for row in cursor.fetchall()]


def read_db_index(since=None):
    """Metadata only on first connection; subsequent discovery scans changed rows."""
    cutoff = datetime.fromtimestamp(since, ZoneInfo(os.getenv("SM_TIMEZONE", "Europe/Berlin"))).replace(tzinfo=None) if since is not None else None
    with database() as cursor:
        if cutoff is None:
            batches = [None]
        else:
            cursor.execute("SELECT ScheibenID FROM Scheiben WHERE Zeitstempel>=%s AND StandNr BETWEEN 1 AND 32767", (cutoff,))
            ids = {int(row["ScheibenID"]) for row in cursor.fetchall()}
            cursor.execute("SELECT DISTINCT ScheibenID FROM Treffer WHERE Zeitstempel>=%s", (cutoff,))
            ids.update(int(row["ScheibenID"]) for row in cursor.fetchall())
            ids = sorted(ids)
            batches = [ids[offset:offset + 50] for offset in range(0, len(ids), 50)]
        index = []
        for batch in batches:
            placeholders = ",".join(["%s"] * len(batch)) if batch is not None else ""
            # Discovery is incremental; last-shot age must still use the complete target.
            cursor.execute("SELECT s.ScheibenID,s.StandNr,s.Zeitstempel,h.last_shot FROM Scheiben s LEFT JOIN "
                           "(SELECT ScheibenID,MAX(TIMESTAMPADD(MICROSECOND,COALESCE(Millisekunden,0)*1000,Zeitstempel)) AS last_shot "
                           "FROM Treffer " + (f"WHERE ScheibenID IN ({placeholders}) " if batch is not None else "") +
                           "GROUP BY ScheibenID) h ON h.ScheibenID=s.ScheibenID "
                           "WHERE s.StandNr BETWEEN 1 AND 32767 " +
                           (f"AND s.ScheibenID IN ({placeholders})" if batch is not None else ""),
                           tuple(batch + batch) if batch is not None else ())
            index.extend({"id": int(row["ScheibenID"]), "lane": int(row["StandNr"]),
                          "modified": stamp(row["Zeitstempel"]), "last_shot": stamp(row["last_shot"])}
                         for row in cursor.fetchall())
        return index


def read_db(lanes=None, ids=None):
    values = sorted(ids if ids is not None else lanes)
    if not values:
        return []
    column = "ScheibenID" if ids is not None else "StandNr"
    placeholders = ",".join(["%s"] * len(values))
    with database() as cursor:
        cursor.execute(f"SELECT ScheibenID,StandNr,Nachname,Vorname,Disziplin,DisziplinID,TotalRing,TotalRing01,Zeitstempel "
                       f"FROM Scheiben WHERE {column} IN ({placeholders}) ORDER BY Zeitstempel DESC", values)
        rows = cursor.fetchall()
        latest = {}
        for row in rows:
            latest.setdefault(row["StandNr"], row)
        rows = rows if ids is not None else list(latest.values())
        if not rows:
            return []
        ids = [row["ScheibenID"] for row in rows]
        placeholders = ",".join(["%s"] * len(ids))
        cursor.execute(f"SELECT ScheibenID,Stellung,Treffer,Serie,x,y,Innenzehner,Wertung,Ring,Ring01,Zeitstempel,Millisekunden "
                       f"FROM Treffer WHERE ScheibenID IN ({placeholders})", ids)
        hits = collections.defaultdict(list)
        for hit in cursor.fetchall():
            hits[hit["ScheibenID"]].append(hit)
        cursor.execute(f"SELECT ScheibenID,Stellung,Serie,Ring,Ring01 FROM Serien WHERE ScheibenID IN ({placeholders})", ids)
        series = collections.defaultdict(list)
        for item in cursor.fetchall():
            series[item["ScheibenID"]].append(item)
        received_at = time.time()
        return [{**from_db(row, hits[row["ScheibenID"]], series[row["ScheibenID"]]), "received_at": received_at}
                for row in rows]


def enrich_sdf(records):
    """Only DB can verify shot concealment and series membership absent from SDF."""
    known = {target["id"]: target for target in read_db(ids={r["id"] for r in records})}
    for record in records:
        db = known.get(record["id"])
        if not db:
            continue
        # Seed a newly collected session with all its prior shots, not only the XML delta.
        record["_db_snapshot"] = db
        for key in ("shooter", "discipline", "discipline_id"):
            if record[key] is None:
                record[key] = db[key]
        for key, shot in record["shots"].items():
            reference = db["shots"].get(key)
            if reference:
                for field in ("mode", "series", "whole_score"):
                    shot[field] = reference[field]
        # Do not replace the feed's live totals with potentially older DB totals.
    return records


class SDFReader:
    def __init__(self, stop, deliver, status):
        self.stop, self.deliver, self.status = stop, deliver, status
        self.host = os.getenv("SM_SMB_HOST", "")
        self.port = int(os.getenv("SM_SMB_PORT", "445"))
        self.share = os.getenv("SM_SMB_SHARE", "xml_result")
        self.base = f"\\\\{self.host}\\{self.share}"
        self.index = {}
        self.pending = {}
        self.cache = {}
        self.watcher = None
        self.discovered = queue.SimpleQueue()
        self.rescan = threading.Event()
        self.scan_error = None
        self.invalid = set()

    def report(self, heartbeat=False):
        state = "degraded" if self.invalid else "connected"
        message = "XML nicht lesbar; letzte Ergebnisse bleiben erhalten" if self.invalid else "SMB-Überwachung aktiv"
        self.status("sdf", state, message, heartbeat=heartbeat)

    def arm(self, directory):
        watcher = FileSystemWatcher(directory)
        watcher.start(
            CompletionFilter.FILE_NOTIFY_CHANGE_FILE_NAME |
            CompletionFilter.FILE_NOTIFY_CHANGE_DIR_NAME |
            CompletionFilter.FILE_NOTIFY_CHANGE_SIZE |
            CompletionFilter.FILE_NOTIFY_CHANGE_LAST_WRITE,
            ChangeNotifyFlags.SMB2_WATCH_TREE,
        )
        self.watcher = watcher
        return watcher

    def scan(self, done, initial=False):
        queue = collections.deque([self.base])
        found = {}
        while queue and not self.stop.is_set() and not done.is_set():
            path = queue.popleft()
            for entry in smbclient.scandir(path, port=self.port, connection_cache=self.cache):
                if self.stop.is_set() or done.is_set():
                    return
                if entry.is_dir(follow_symlinks=False):
                    queue.append(entry.path)
                elif entry.name.lower().endswith(".xml") and entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    found[entry.path] = (stat.st_mtime_ns, stat.st_size)
        for path, fingerprint in found.items():
            if not initial and self.index.get(path) != fingerprint:
                self.discovered.put(path)
        self.index = found

    def scan_loop(self, done):
        try:
            self.scan(done, initial=not self.index)
            while not done.is_set() and not self.stop.is_set():
                if self.rescan.wait(30):
                    self.rescan.clear()
                if not done.is_set() and not self.stop.is_set():
                    self.scan(done)
        except Exception as error:
            self.scan_error = error

    def changed(self, actions):
        for action in actions:
            relative = action["file_name"].get_value().replace("/", "\\")
            parts = relative.split("\\")
            if not relative or any(p in ("", ".", "..") for p in parts) or ":" in relative:
                continue
            path = self.base + "\\" + relative
            if relative.lower().endswith(".xml"):
                self.pending[path] = (time.monotonic(), 0)

    def read_pending(self):
        for path, (due, attempts) in list(self.pending.items()):
            if self.stop.is_set() or time.monotonic() < due:
                continue
            try:
                with smbclient.open_file(path, mode="rb", port=self.port, connection_cache=self.cache) as file:
                    data = file.read(MAX_XML_BYTES + 1)
                records = parse_sdf(data)
                for record in records:
                    record["received_at"] = time.time()
                try:
                    enrich_sdf(records)
                except Exception:
                    # Fail closed for modes/series; the next SQL reconciliation fills these in.
                    pass
                self.deliver(records)
                del self.pending[path]
                self.invalid.discard(path)
                self.report()
            except FileNotFoundError:
                self.pending.pop(path, None)
                self.invalid.discard(path)
            except Exception as error:
                # Partial writes can precede the next notification. Retain last valid target.
                if attempts < 8:
                    self.pending[path] = (time.monotonic() + min(0.05 * 2**attempts, 2), attempts + 1)
                else:
                    del self.pending[path]
                    self.invalid.add(path)
                    self.report()

    def run(self):
        if not self.host:
            self.status("sdf", "disabled", "SM_SMB_HOST fehlt")
            return
        while not self.stop.is_set():
            connection = None
            directory = None
            scan_done = threading.Event()
            scanner = None
            try:
                self.scan_error = None
                smbclient.register_session(self.host, username=os.environ["SM_SMB_USER"],
                    password=os.environ["SM_SMB_PASS"], port=self.port, connection_timeout=5,
                    connection_cache=self.cache)
                connection = Connection(uuid4(), self.host, self.port, require_signing=True)
                connection.connect(timeout=5)
                session = Session(connection, username=os.environ["SM_SMB_USER"], password=os.environ["SM_SMB_PASS"])
                session.connect()
                tree = TreeConnect(session, self.base)
                tree.connect()
                directory = Open(tree, "")
                directory.create(ImpersonationLevel.Impersonation,
                    DirectoryAccessMask.FILE_LIST_DIRECTORY | DirectoryAccessMask.FILE_READ_ATTRIBUTES,
                    FileAttributes.FILE_ATTRIBUTE_DIRECTORY,
                    ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE | ShareAccess.FILE_SHARE_DELETE,
                    CreateDisposition.FILE_OPEN, CreateOptions.FILE_DIRECTORY_FILE)
                watcher = self.arm(directory)
                # Historical files are indexed, not replayed over the fresh SQL snapshot.
                # Rescans run separately so archive size cannot block live notifications.
                scanner = threading.Thread(target=self.scan_loop, args=(scan_done,), daemon=True)
                scanner.start()
                self.report()
                while not self.stop.is_set():
                    if self.scan_error:
                        raise self.scan_error
                    if watcher.response_event.wait(0.02):
                        actions = watcher.result
                        watcher = self.arm(directory)  # Re-arm before parsing or SQL work.
                        if actions:
                            self.changed(actions)
                        else:
                            self.rescan.set()  # Overflow means events may have been lost.
                    while not self.discovered.empty():
                        self.pending[self.discovered.get()] = (time.monotonic(), 0)
                    self.read_pending()
                    self.report(heartbeat=True)
            except Exception as error:
                self.status("sdf", "disconnected", f"SMB nicht erreichbar ({type(error).__name__})")
            finally:
                scan_done.set()
                self.rescan.set()
                if scanner:
                    scanner.join(timeout=6)
                if self.watcher:
                    try:
                        self.watcher.cancel()
                    except Exception:
                        pass
                if connection:
                    connection.disconnect(close=False)
                smbclient.reset_connection_cache(connection_cache=self.cache)
            self.stop.wait(2)
