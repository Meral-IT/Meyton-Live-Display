"""Isolated worker/SQLite/SSE replay, restart and timing checks; no vendor connections."""

import json
import multiprocessing
import os
import queue
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "backend"))


def serve(directory, port):
    import uvicorn
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from app.main import create_app

    app = create_app(Path(directory) / "profiles.json", result_path=Path(directory) / "results.sqlite3")
    public = REPOSITORY / "frontend" / "public"
    app.add_api_route("/range/{lane}", lambda: FileResponse(public / "index.html"), methods=["GET"])
    app.mount("/", StaticFiles(directory=public, html=True))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error", access_log=False)


def wait_for(check, *, timeout=10, page=None):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for local storage/display state")
        if page:
            page.wait_for_timeout(10)
        else:
            time.sleep(0.02)


def main():
    from app.worker import Worker
    from test_app import target

    for name in ("SM_DB_HOST", "SM_SMB_HOST", "SM_LANA_HOST"):
        os.environ[name] = ""  # This replay must never access vendor sources.
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        ready, commits = queue.Queue(), queue.Queue()
        errors = []

        def run_worker():
            try:
                worker = Worker(Path(directory) / "results.sqlite3")
                worker.store.baseline([], now=time.time() - 1)
                worker.ready.set()
                original = worker.store.ingest
                def ingest(records):
                    started = time.perf_counter()
                    changed = original(records)
                    commits.put({"changed": changed, "commit_ms": (time.perf_counter() - started) * 1000})
                    return changed
                worker.store.ingest = ingest
                ready.put(worker)
                worker.run()
            except Exception as error:
                errors.append(repr(error))

        thread = threading.Thread(target=run_worker, daemon=True)
        thread.start()
        worker = ready.get(timeout=10)
        backend = context.Process(target=serve, args=(directory, port), daemon=True)
        backend.start()
        url = f"http://127.0.0.1:{port}"
        records = []
        try:
            with httpx.Client(base_url=url, timeout=1) as client:
                def healthy():
                    try:
                        return client.get("/healthz").status_code == 200
                    except httpx.TransportError:
                        return False
                wait_for(healthy)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    pages, rendered = [], [[], [], []]
                    for viewer in range(3):
                        page = browser.new_page(viewport={"width": 1920, "height": 1080})
                        page.expose_function("recordTiming", lambda data, i=viewer: rendered[i].append(data))
                        page.add_init_script("document.addEventListener('meyton-render', event => window.recordTiming(event.detail))")
                        page.goto(url + "/range/77?obs=1")
                        page.locator(".target-empty").wait_for()
                        pages.append(page)
                    initial_bytes = sum(p.stat().st_size for p in Path(directory).glob("results.sqlite3*"))
                    peak_bytes = initial_bytes
                    practice = {}
                    for sequence in range(1, 21):
                        position = 0 if sequence <= 10 else 1
                        number = sequence if position == 0 else sequence - 10
                        now = time.time()
                        data = target(number, now, position=position)
                        data.update(id=-77, lane=77, received_at=now)
                        if position == 0:
                            practice = data["shots"].copy()
                        else:
                            data["shots"] = practice | data["shots"]
                        offsets = [len(events) for events in rendered]
                        worker.enqueue("targets", [data])
                        commit = commits.get(timeout=3)
                        assert commit["changed"]
                        peak_bytes = max(peak_bytes, sum(p.stat().st_size for p in Path(directory).glob("results.sqlite3*")))
                        def seen():
                            return all(any(event["shots"] and event["shots"][0]["position"] == position and
                                           event["shots"][0]["number"] == number for event in events[offset:])
                                       for events, offset in zip(rendered, offsets))
                        wait_for(seen, page=pages[0], timeout=3)
                        frames = [next(event for event in events[offset:] if event["shots"] and
                                       event["shots"][0]["position"] == position and event["shots"][0]["number"] == number)
                                  for events, offset in zip(rendered, offsets)]
                        records.append({"position": position, "shot": number, "commit_ms": round(commit["commit_ms"], 2),
                                        "worker_to_render_ms": round(max(frame["rendered_at"] for frame in frames) - now * 1000, 2)})
                    worker.enqueue("targets", [data])
                    assert not commits.get(timeout=3)["changed"], "Duplicate update wrote results"
                    backend.terminate()
                    backend.join(timeout=5)
                    backend = context.Process(target=serve, args=(directory, port), daemon=True)
                    backend.start()
                    wait_for(healthy)
                    snapshot = client.get("/api/ranges/77").json()
                    assert snapshot["rows"][0][0]["target"]["scored_shot_count"] == 10
                    # Reconnecting SSE viewers recover persisted state without a vendor query.
                    pages[0].reload()
                    pages[0].locator(".stat strong").first.get_by_text("10", exact=True).wait_for()
                    worker.enqueue("occupancy", {77: {"state": "free", "shooter": "Unbekannt"}})
                    wait_for(lambda: client.get("/api/ranges/77").json()["rows"][0][0]["target"] is None)
                    worker.stop.set()
                    thread.join(timeout=5)
                    wait_for(lambda: client.get("/healthz").json()["sources"]["worker"]["state"] == "degraded",
                             timeout=20, page=pages[0])
                    assert client.get("/api/ranges/77").json()["rows"][0][0]["target"] is None
                    browser.close()
            output = REPOSITORY / "test-results" / "storage-replay.json"
            output.parent.mkdir(exist_ok=True)
            maximum = max(record["worker_to_render_ms"] for record in records)
            summary = {"shots": len(records), "viewers": 3, "maximum_worker_to_render_ms": maximum,
                       "maximum_commit_ms": max(record["commit_ms"] for record in records),
                       "initial_database_bytes": initial_bytes, "peak_database_bytes": peak_bytes, "records": records}
            output.write_text(json.dumps(summary, indent=2) + "\n")
            print(f"Replay passed: {len(records)} shots, 3 viewers, restart, duplicate suppression, durable clear, stale worker.")
            print(f"Maximum worker-to-render: {maximum:.2f} ms; commit: {summary['maximum_commit_ms']:.2f} ms")
            print(f"Report: {output}")
            assert maximum < 100, "Local worker-to-render target exceeded; physical latency remains a separate gate"
            assert not errors, errors
        finally:
            worker.stop.set()
            thread.join(timeout=5)
            backend.terminate()
            backend.join(timeout=5)


if __name__ == "__main__":
    main()
