"""Vendor data normalization and the shared in-memory range state."""

import copy
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

MAX_XML_BYTES = 2 * 1024 * 1024
TARGET_RULES = {"10": "lg","10100": "lg", "10110": "lg", "10111": "lg", "10210": "lp",
               "11009": "lg", "11011": "lg", "11012": "lg",
               "18052": "schach10",
               "40110": "kk", "40140": "kk", "40141": "kk", "40180": "kk",
               "41209": "kk", "41211": "kk", "41212": "kk"}
# Local copies use short IDs. Match verified names, not weapon prefixes.
STANDARD_TARGET_NAMES = {"LG Auflage 30": "lg", "LG 20": "lg", "LP 40": "lp",
                         "LG 5P 10W": "lg",
                         "LG Schach 10x10": "schach10", "LG Schach 10x10 5W": "schach10",
                         "LG Schach 5W": "schach10"}


def stamp(value, milliseconds=0, *, sdf=False):
    if not value or str(value).startswith("0000-"):
        return None
    value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if sdf:
        # Observed ShootMaster 6.0 exports local wall time with a misleading Z.
        # An empty override honors the XML timezone on installations that export real UTC.
        override = os.getenv("SM_SDF_TIMEZONE", "Europe/Berlin")
        if override:
            value = value.replace(tzinfo=ZoneInfo(override))
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(os.getenv("SM_TIMEZONE", "Europe/Berlin")))
    return value.timestamp() + int(milliseconds or 0) / 1000


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def target_id(value):
    if not re.fullmatch(r"[0-9a-fA-F]{8}", value):
        raise ValueError("Invalid SDF TargetID")
    number = int(value, 16)
    return number - 2**32 if number >= 2**31 else number


def score(value, unit="Ring", *, scale=None):
    if value is None:
        return None
    return {"value": int(value), "scale": scale if scale is not None else (10 if unit in ("Ring", "ZehntelRing") else 1), "unit": unit}


def shooter_name(family, given):
    parts = [part.strip() for part in (family, given) if part and part.strip().casefold() not in ("unknown", "unkown")]
    return ", ".join(parts) or "Unbekannt"


def from_db(row, hits, series):
    shots = {}
    for hit in hits:
        position, number = int(hit["Stellung"]), int(hit["Treffer"])
        shots[(position, number)] = {
            "position": position, "number": number, "series": int(hit["Serie"]),
            "x_mm": hit["x"] / 100, "y_mm": hit["y"] / 100,
            "score": score(hit["Ring01"], "ZehntelRing"),
            "whole_score": score(hit["Ring"]), "mode": int(hit["Wertung"]),
            "inner_ten": bool(hit["Innenzehner"]), "invalid": False,
            "time": stamp(hit["Zeitstempel"], hit["Millisekunden"]),
        }
    return {
        "id": int(row["ScheibenID"]), "lane": int(row["StandNr"]),
        "shooter": shooter_name(row["Nachname"], row["Vorname"]),
        "discipline": row["Disziplin"], "discipline_id": int(row["DisziplinID"]),
        "modified": stamp(row["Zeitstempel"]), "shots": shots,
        "total": score(row["TotalRing"]), "decimal_total": score(row["TotalRing01"], "ZehntelRing"),
        "series": {(int(s["Stellung"]), int(s["Serie"])): {
            "position": int(s["Stellung"]), "number": int(s["Serie"]),
            "score": score(s["Ring"], scale=1), "decimal_score": score(s["Ring01"], "ZehntelRing"),
        } for s in series}, "source": "db", "full": True,
    }


def parse_sdf(data):
    if len(data) > MAX_XML_BYTES:
        raise ValueError("SDF XML exceeds size limit")
    # Reject DTD/entities before parsing, including UTF-16/32 encoded declarations.
    check = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in check or b"<!ENTITY" in check:
        raise ValueError("DTD and entities are not allowed")
    root = ET.fromstring(data)
    if root.tag.split("}")[-1] != "ResultList" or root.get("Version") != "0.2.2":
        raise ValueError("Unsupported SDF format (expected ResultList 0.2.2)")
    for element in root.iter():
        element.tag = element.tag.split("}")[-1]
    records = []
    for row in root.findall("ResultRecord"):
        lane = int(row.attrib["LaneNo"])
        if not 1 <= lane <= 32767:
            raise ValueError("Invalid range number")
        shots, series = {}, {}
        for aiming in row.findall("Aimings/AimingData"):
            position = int(aiming.attrib["AimingID"])
            if not 0 <= position <= 8:
                raise ValueError("Invalid position")
            for hit in aiming.findall("Shot"):
                number = int(hit.attrib["ShotID"])
                if not 1 <= number <= 400:
                    raise ValueError("Invalid shot number")
                coordinate = hit.find("Coordinate/CCoordinate")
                resolution = int(coordinate.get("Resolution", "100")) if coordinate is not None else 100
                if resolution not in (1, 10, 100, 1000):
                    raise ValueError("Invalid coordinate resolution")
                values = {s.findtext("Unit"): score(s.findtext("Result"), s.findtext("Unit"))
                          for s in hit.findall("RingValue")}
                shots[(position, number)] = {
                    "position": position, "number": number,
                    # SDF has no per-shot series membership. Resolve it from DB;
                    # never infer ten-shot series for custom disciplines.
                    "series": None,
                    "x_mm": int(coordinate.findtext("X")) / resolution if coordinate is not None else None,
                    "y_mm": int(coordinate.findtext("Y")) / resolution if coordinate is not None else None,
                    "score": values.get("ZehntelRing", values.get("Ring")),
                    "whole_score": values.get("Ring"), "mode": None,
                    "inner_ten": hit.get("IsInnerTen", "false") in ("true", "1"),
                    "invalid": hit.get("IsInValid", "false") in ("true", "1"),
                    "time": stamp(hit.findtext("TimeStamp/DateTime"),
                                  int(hit.findtext("TimeStamp/Hundredth", "0")) * 10, sdf=True),
                }
            for item in aiming.findall("Series"):
                number = int(item.attrib["SeriesID"])
                series[(position, number)] = {"position": position, "number": number,
                    "score": score(item.findtext("ValueSerie/Result"), item.findtext("ValueSerie/Unit", "Ring")),
                    "decimal_score": None}
        shooter = row.find("Shooter")
        records.append({
            "id": target_id(row.attrib["TargetID"]), "lane": lane,
            "shooter": shooter_name(shooter.findtext("FamilyName"), shooter.findtext("GivenName")) if shooter is not None else None,
            "discipline": row.findtext("Discipline/Name"),
            "discipline_id": int(row.findtext("Discipline/ID")) if row.findtext("Discipline/ID") else None,
            "modified": max((s["time"] for s in shots.values() if s["time"] is not None), default=None),
            "shots": shots, "series": series,
            "total": score(row.findtext("Total/Result"), row.findtext("Total/Unit", "Ring")),
            "decimal_total": None, "source": "sdf",
            "full": row.get("ResultStatus") in ("OFFICIAL", "LIVE_FULL"),
        })
    return records


def last_hit(target):
    return max(target["shots"].values(), key=lambda s: (s["time"] or 0, s["position"], s["number"]), default=None)


def ordering(target):
    hit = last_hit(target)
    return max(target.get("modified") or 0, (hit or {}).get("time") or 0)


def target_activity(target):
    hit = last_hit(target)
    return hit["time"] if hit and hit["time"] is not None else target.get("modified")


class RangeState:
    def __init__(self):
        self.targets = {}
        self.occupancy = {}
        self.live_shooters = {}
        self.cleared = {}

    def set_occupancy(self, lanes):
        old = self.occupancy
        old_names = self.live_shooters
        self.occupancy = {lane: entry["state"] for lane, entry in lanes.items()}
        self.live_shooters = {lane: entry["shooter"] for lane, entry in lanes.items() if entry["state"] == "occupied"}
        for lane, state in self.occupancy.items():
            if state == "free" and old.get(lane) != "free":
                target = self.targets.get(lane)
                self.cleared[lane] = (target["id"], (last_hit(target) or {}).get("time") or 0) if target else None
        return old != self.occupancy or old_names != self.live_shooters

    def apply(self, incoming):
        incoming = copy.deepcopy(incoming)
        lane = incoming["lane"]
        current = self.targets.get(lane)
        if self.occupancy.get(lane) == "free" and self.cleared.get(lane) is None:
            target = current or incoming
            self.cleared[lane] = (target["id"], (last_hit(target) or {}).get("time") or 0)
        for key, shot in incoming["shots"].items():
            previous = current["shots"].get(key) if current and current["id"] == incoming["id"] else None
            same_shot = previous and previous["time"] == shot["time"]
            shot["received_at"] = previous.get("received_at") if same_shot else incoming.get("received_at", time.time())
            shot["received_source"] = previous.get("received_source") if same_shot else incoming["source"]
            if same_shot and "persisted_at" in previous:
                shot["persisted_at"] = previous["persisted_at"]
        if current is None or current["id"] != incoming["id"]:
            if current and ordering(incoming) <= ordering(current):
                return False
            self.targets[lane] = incoming
            return True
        old = copy.deepcopy(current)
        if incoming["source"] == "db":
            # Old DB snapshots must not roll back an SDF shot awaiting DB export.
            live_newer = (last_hit(current) or {}).get("time") or 0
            db_newest = (last_hit(incoming) or {}).get("time") or 0
            if db_newest < live_newer and (incoming["modified"] or 0) < live_newer and current["source"] == "sdf":
                for key, hit in current["shots"].items():
                    if key not in incoming["shots"]:
                        incoming["shots"][key] = hit
                incoming["series"] = current["series"] | incoming["series"]
                incoming["total"] = current["total"]
                incoming["decimal_total"] = current["decimal_total"]
                incoming["source"] = "sdf"
            self.targets[lane] = incoming
        else:
            # Partial exports may omit metadata and historical hits.
            for key in ("shooter", "discipline", "discipline_id"):
                if incoming[key] is None:
                    incoming[key] = current[key]
            if ordering(incoming) < ((last_hit(current) or {}).get("time") or 0):
                return False
            if current["source"] == "db" and ((last_hit(incoming) or {}).get("time") or 0) <= ((last_hit(current) or {}).get("time") or 0):
                # SQL is authoritative for existing-shot corrections. A repeated
                # older XML export must not undo a jury correction or deletion.
                return False
            hits = {} if incoming["full"] else current["shots"].copy()
            for key, hit in incoming["shots"].items():
                previous = current["shots"].get(key, {})
                for field in ("mode", "series", "whole_score"):
                    if hit[field] is None:
                        hit[field] = previous.get(field)
                hits[key] = hit
            incoming["shots"] = hits
            if not incoming["full"]:
                incoming["series"] = current["series"] | incoming["series"]
            self.targets[lane] = incoming
        return old != self.targets[lane]

    def public(self, lane, profile):
        target = self.targets.get(lane)
        if target is None:
            return None
        if self.occupancy.get(lane) == "free":
            return None
        live_name = self.live_shooters.get(lane)
        if live_name and sorted(live_name.casefold().replace(",", " ").split()) != sorted((target["shooter"] or "Unbekannt").casefold().replace(",", " ").split()):
            return None  # Wait for this shooter's export rather than showing another shooter's archive.
        cleared = self.cleared.get(lane)
        if cleared and target["id"] == cleared[0] and ((last_hit(target) or {}).get("time") or 0) <= cleared[1]:
            return None  # An occupancy change must not resurrect the cleared target.
        eligible = [s for s in target["shots"].values() if profile.practice or s["position"] != 0]
        latest = max(eligible, key=lambda s: (s["time"] or 0, s["position"], s["number"]), default=None)
        position = latest["position"] if latest else 1
        hits = [s for s in eligible if s["position"] == position]
        series_number = latest["series"] if latest else None
        selected = [s for s in hits if profile.hits == "all" or (series_number is not None and s["series"] == series_number)]
        # SDF does not encode concealment. Unknown mode stays concealed until DB confirms it.
        safe_hits = []
        for hit in sorted(selected, key=lambda s: s["number"]):
            item = {k: v for k, v in hit.items() if k not in ("mode", "time")}
            item["timestamp"] = iso(hit["time"])
            item["received_at"] = iso(hit.get("received_at"))
            item["persisted_at"] = iso(hit.get("persisted_at"))
            if hit["mode"] not in (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12):
                item["x_mm"] = item["y_mm"] = None
            if hit["mode"] in (None, 0, 5, 10):
                item["score"] = item["whole_score"] = None
                item["inner_ten"] = False
            safe_hits.append(item)
        scored = [s for s in target["shots"].values() if s["position"] != 0]
        concealed = not scored or any(s["mode"] in (None, 0, 5, 10) for s in scored)
        result_series = [s for (p, _), s in sorted(target["series"].items()) if p == position]
        if concealed or position == 0:
            result_series = [{**s, "score": None, "decimal_score": None} for s in result_series]
        last_public = next((s for s in safe_hits if latest and s["number"] == latest["number"]), None)
        discipline_id = str(target["discipline_id"])
        target_kind = TARGET_RULES.get(discipline_id[:5]) if len(discipline_id) == 8 else STANDARD_TARGET_NAMES.get(target["discipline"])
        return {
            "id": target["id"], "lane": lane, "shooter": target["shooter"],
            "discipline": target["discipline"], "discipline_id": target["discipline_id"],
            "target_kind": target_kind,
            "position": position, "practice": position == 0, "shot_count": len(hits),
            "scored_shot_count": len(scored), "latest": last_public,
            "total": None if concealed else target["total"],
            "decimal_total": None if concealed else target["decimal_total"],
            "series": result_series, "shots": safe_hits,
            "series_pending": bool(hits and profile.hits == "series" and series_number is None),
            "last_shot_at": iso(latest["time"]) if latest else None,
            "source": target["source"],
        }
