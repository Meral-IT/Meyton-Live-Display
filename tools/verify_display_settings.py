"""Offline settings, shot timers and unavailable-stand checks using Playwright."""

import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from verify_browser import example, seeds


def main():
    root = Path(__file__).resolve().parents[1] / "frontend" / "public"
    data = example(seeds()[0])
    data["runtime_id"] = "runtime-one"
    data["profile"].update(shot_highlight="background", hide_unavailable=True)
    data["sources"].update({name: {"state": "connected"} for name in ("worker", "local_db", "lana")})
    data["rows"][0][1].update(target=None, occupancy="free")
    for entry in data["rows"][1]:
        entry.update(target=None, occupancy="unknown")
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("window.EventSource = class extends EventTarget { constructor() { super(); window.testStream = this; } close() {} };")
        navigations = []
        page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)

        def respond(route):
            assert urlsplit(route.request.url).netloc == "meyton.test"  # No runtime CDN or other remote requests.
            path = urlsplit(route.request.url).path
            if path == "/api/events":
                route.fulfill(content_type="text/event-stream", body=": test\n\n")
            elif path.endswith("/preview"):
                route.fulfill(json={**data, "profile": route.request.post_data_json})
            elif path == "/api/admin/profiles/alles":
                data["profile"] = route.request.post_data_json
                route.fulfill(json=data["profile"])
            elif path in ("/api/profiles", "/api/admin/profiles"):
                route.fulfill(json=[data["profile"]])
            elif path == "/api/admin/sponsors":
                route.fulfill(json=[])
            elif path == "/api/admin/logo":
                route.fulfill(body="null", content_type="application/json")
            elif path == "/api/snapshot":
                route.fulfill(json=data)
            else:
                file = root / ("index.html" if path.startswith("/display/") else "admin.html" if path == "/admin" else path.lstrip("/"))
                route.fulfill(body=file.read_bytes(), content_type={".html": "text/html", ".css": "text/css", ".js": "text/javascript"}[file.suffix])

        page.route("**/*", respond)
        page.clock.install()
        page.goto("http://meyton.test/display/alles?preview=1")
        page.wait_for_selector(".range-tile")

        def update():
            page.evaluate("snapshot => window.postMessage({type: 'profile-preview', snapshot}, location.origin)", data)
            page.wait_for_timeout(30)

        assert page.locator(".range-tile").count() == 4
        assert page.locator('[data-lane="2"] .phase').inner_text() == "Frei"
        assert page.locator(".range-row").count() == 1  # Empty rows collapse too.
        assert page.locator(".shot-background").count() == 0  # No initial archive flash.
        target = data["rows"][0][0]["target"]
        target["shot_count"] += 1
        update()
        assert page.locator(".shot-background").count() == 1
        card = page.locator('[data-lane="1"]')
        assert card.evaluate("node => getComputedStyle(node).backgroundColor") == "rgb(209, 213, 219)"
        page.clock.run_for(2000)
        update()  # Heartbeats and corrections must not restart the timer.
        page.clock.run_for(900)
        assert card.evaluate("node => node.classList.contains('shot-background')")
        page.clock.run_for(200)
        assert page.locator(".shot-background").count() == 0
        data["profile"]["shot_highlight"] = "border"
        target["shot_count"] += 1
        update()
        assert card.evaluate("node => getComputedStyle(node).borderColor") == "rgb(107, 114, 128)"
        page.clock.run_for(2000)
        target["shot_count"] += 1
        update()
        page.clock.run_for(2000)
        assert page.locator(".shot-border").count() == 1  # Another shot extends to 3 s.
        page.clock.run_for(1100)
        assert page.locator(".shot-border").count() == 0
        data["rows"][0][1].update(target={**target, "id": 2, "target_kind": None, "latest": None}, occupancy="occupied")
        update()
        assert page.locator('[data-lane="2"].shot-border').count() == 1  # First shot, even without a supported target.
        data["profile"]["shot_highlight"] = "none"
        target["shot_count"] += 1
        update()
        assert page.locator(".shot-background, .shot-border").count() == 0
        data["sources"]["lana"]["state"] = "disconnected"
        update()
        assert page.locator(".range-tile").count() == 7

        data["sources"]["lana"]["state"] = "connected"
        data["rows"][1][0]["occupancy"] = "free"
        update()
        assert page.locator(".range-tile").count() == 5  # A returning free stand reappears.
        data["profile"]["hide_unavailable"] = False
        update()
        assert page.locator(".range-tile").count() == 7

        data["profile"]["hide_unavailable"] = True
        for row in data["rows"]:
            for entry in row:
                entry["occupancy"] = "unknown"
        update()
        assert page.locator(".range-row, .range-tile").count() == 0
        assert page.locator("#ranges").evaluate("node => node.style.gridTemplateRows") == "none"

        count = len(navigations)
        page.evaluate("data => window.testStream.dispatchEvent(new MessageEvent('snapshot', {data: JSON.stringify(data)}))", data)
        page.wait_for_timeout(30)
        assert len(navigations) == count  # Same runtime must not reload.
        data["runtime_id"] = "runtime-two"
        with page.expect_navigation():
            page.evaluate("data => window.testStream.dispatchEvent(new MessageEvent('snapshot', {data: JSON.stringify(data)}))", data)
        assert page.url == "http://meyton.test/display/alles?preview=1"  # Also detects changes behind a cached preview.
        page.wait_for_timeout(30)
        assert len(navigations) == count + 1  # The fresh initial snapshot does not loop.

        data["profile"].update(hide_unavailable=False, confetti_enabled=True, confetti_threshold=10.5)
        data["rows"][0][1]["target"] = None
        page.goto("http://meyton.test/display/alles?obs=2")
        page.wait_for_selector(".range-tile")
        assert page.locator("canvas").count() == 0  # Initial qualifying scores remain quiet.
        page.evaluate("() => { window.bursts = []; const original = window.confetti; window.confetti = options => { window.bursts.push(options); original(options); }; }")

        def live():
            page.evaluate("data => window.testStream.dispatchEvent(new MessageEvent('snapshot', {data: JSON.stringify(data)}))", data)
            page.wait_for_timeout(30)

        def shoot(value, **changes):
            hit = {**target["latest"], "number": target["latest"]["number"] + 1,
                   "score": {"value": value, "scale": 10, "unit": "ZehntelRing"}, **changes}
            target["latest"] = hit
            target["shots"].append(hit)

        live()
        assert page.evaluate("bursts.length") == 0  # Initial SSE snapshot also stays quiet.
        shoot(104)
        live()
        assert page.evaluate("bursts.length") == 0, page.evaluate("bursts")
        shoot(105)
        live()
        assert page.evaluate("bursts.length") == 1
        assert page.locator("canvas").count() == 1  # The real, locally served library renders.
        assert page.locator("canvas").evaluate("node => getComputedStyle(node).pointerEvents") == "none"
        bounds = page.locator('[data-lane="1"]').bounding_box()
        assert bounds["x"] <= page.evaluate("bursts[0].position.x") <= bounds["x"] + bounds["width"]
        live()
        assert page.evaluate("bursts.length") == 1
        shoot(109)
        live()
        assert page.evaluate("bursts.length") == 2
        data["profile"]["confetti_enabled"] = False
        shoot(109)
        live()
        data["profile"]["confetti_enabled"] = True
        live()
        assert page.evaluate("bursts.length") == 2  # Enabling does not replay scores.
        shoot(109, invalid=True)
        live()
        shoot(109, invalid=False, score=None)
        live()
        assert page.evaluate("bursts.length") == 2
        data["profile"]["confetti_threshold"] = 0
        shoot(0)
        live()
        assert page.evaluate("bursts.length") == 3
        data["profile"]["confetti_threshold"] = 10.9
        shoot(108)
        live()
        assert page.evaluate("bursts.length") == 3
        data["profile"]["confetti_threshold"] = 10.5
        shoot(105)
        shoot(108)
        shoot(102)
        live()
        assert page.evaluate("bursts.length") == 5  # Coalesced snapshots still celebrate each candidate.
        page.emulate_media(reduced_motion="reduce")
        shoot(109)
        live()
        assert page.evaluate("bursts.length") == 5
        page.emulate_media(reduced_motion="no-preference")
        shoot(109, score=None)
        live()
        assert page.evaluate("bursts.length") == 5
        target["latest"]["score"] = {"value": 109, "scale": 10, "unit": "ZehntelRing"}
        live()
        assert page.evaluate("bursts.length") == 6  # Delayed score confirmation can celebrate a new shot.
        live()
        assert page.evaluate("bursts.length") == 6

        page.goto("http://meyton.test/admin")
        page.wait_for_selector(".profile-choice")
        page.locator("#shot-highlight").select_option("border")
        page.locator("#hide-unavailable").check()
        page.locator("#confetti-enabled").check()
        page.locator("#confetti-threshold").fill("10.9")
        page.locator("#save-profile").click()
        page.locator("#save-status").get_by_text("Gespeichert · Anzeigen aktualisiert", exact=True).wait_for()
        assert data["profile"]["shot_highlight"] == "border"
        assert data["profile"]["hide_unavailable"] is True
        assert data["profile"]["confetti_enabled"] is True
        assert data["profile"]["confetti_threshold"] == 10.9
        page.reload()
        page.wait_for_selector(".profile-choice")
        assert page.locator("#shot-highlight").input_value() == "border"
        assert page.locator("#hide-unavailable").is_checked()
        assert page.locator("#confetti-enabled").is_checked()
        assert page.locator("#confetti-threshold").input_value() == "10.9"
        data["runtime_id"] = "runtime-three"
        with page.expect_navigation():
            page.locator("#save-profile").click()
        page.wait_for_selector(".profile-choice")
        assert page.url == "http://meyton.test/admin"
        for width in (1440, 390, 320):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.locator("#shot-highlight").evaluate("node => { const r=node.getBoundingClientRect(), f=node.closest('form').getBoundingClientRect(); return r.left>=f.left && r.right<=f.right; }")
        assert not errors, errors
        browser.close()
    print("Settings, shot highlights, availability, runtime reloads and local live-shot confetti passed.")


if __name__ == "__main__":
    main()
