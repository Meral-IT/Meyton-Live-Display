"""Record new rendered shots and timing; requires synchronized, verified vendor clocks."""

import argparse
import csv
import json
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright


def milliseconds(value):
    return datetime.fromisoformat(value).timestamp() * 1000 if value else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://localhost/display/alles?obs=1")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--output", default="test-results/live-timing.csv")
    args = parser.parse_args()
    seen, records = set(), []
    baseline = True
    started_at = time.time() * 1000

    def event(data):
        nonlocal baseline
        for shot in data["shots"]:
            key = (shot["lane"], shot["target_id"], shot["position"], shot["number"])
            if key in seen:
                continue
            if baseline:
                seen.add(key)
                continue
            if shot.get("x_mm") is None or shot.get("y_mm") is None:
                continue  # Concealed/pending hits have not appeared in the Schussbild.
            seen.add(key)
            detected = milliseconds(shot["timestamp"])
            received = milliseconds(shot.get("received_at"))
            persisted = milliseconds(shot.get("persisted_at"))
            if detected is None or received is None or received < started_at:
                continue
            record = {"lane": shot["lane"], "position": shot["position"], "shot": shot["number"],
                      "source": shot.get("received_source"),
                      "source_visibility_ms": round(received - detected, 1),
                      "receipt_to_transaction_ms": round(persisted - received, 1) if persisted is not None else None,
                      "storage_to_render_ms": round(data["rendered_at"] - persisted, 1) if persisted is not None else None,
                      "application_ms": round(data["rendered_at"] - received, 1),
                      "total_ms": round(data["rendered_at"] - detected, 1)}
            records.append(record)
            print(json.dumps(record), flush=True)
        baseline = False

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(ignore_https_errors=True, viewport={"width": 1920, "height": 1080})
        page.expose_function("recordMeytonTiming", event)
        page.add_init_script("document.addEventListener('meyton-render', event => window.recordMeytonTiming(event.detail))")
        page.goto(args.url)
        print("Listening for new shots. Include practice and scored positions.", flush=True)
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            page.wait_for_timeout(min(1000, max(1, (end - time.monotonic()) * 1000)))
        browser.close()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    if records:
        with path.open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)
        maximum = max(record["total_ms"] for record in records)
        positions = sorted({record["position"] for record in records})
        print(f"Observed shots: {len(records)}; positions: {positions}; maximum: {maximum:.1f} ms", flush=True)
        print(f"Report: {path}", flush=True)
    else:
        print("No new rendered shots observed; latency gate remains unverified.", flush=True)
    print("Clock offsets and SDF export cadence must be verified. Negative timings invalidate the measurement.", flush=True)


if __name__ == "__main__":
    main()
