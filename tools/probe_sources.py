"""Read-only commissioning probe. No credentials or shooter identities are printed."""

import argparse
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def load_env():
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main():
    load_env()
    from app.domain import iso, last_hit
    from app.sources import SDFReader, read_db

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30)
    args = parser.parse_args()
    targets = read_db(lanes={1, 2, 3, 4, 5, 51, 52, 53, 54, 55})
    print("DB connected; latest targets:", len(targets), flush=True)
    for target in targets:
        hit = last_hit(target)
        print("range", target["lane"], "positions", sorted({s["position"] for s in target["shots"].values()}),
              "hits", len(target["shots"]), "last_shot", iso(hit["time"]) if hit else None, flush=True)
    stop = threading.Event()
    states = {}
    events = []

    def status(source, state, message, heartbeat=False):
        if states.get(source) != state:
            print(source, state, message, flush=True)
        states[source] = state

    def deliver(records):
        for record in records:
            events.append(record)
            print("SDF update; range", record["lane"], "positions", sorted({s["position"] for s in record["shots"].values()}),
                  "hits", len(record["shots"]), "unknown_modes", sum(s["mode"] is None for s in record["shots"].values()), flush=True)

    reader = SDFReader(stop, deliver, status)
    thread = threading.Thread(target=reader.run, daemon=True)
    thread.start()
    try:
        stop.wait(args.seconds)
    finally:
        stop.set()
        thread.join(timeout=8)
    print("SDF updates observed:", len(events), flush=True)
    print("Physical-shot latency and practice export require an on-range browser test.", flush=True)
    return 0 if states.get("sdf") == "connected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
