"""Runnable browser checks against the Compose frontend; temporary profiles are removed."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from probe_sources import load_env
from app.profiles import seeds


def example(profile):
    now = "2026-10-01T17:00:00+00:00"
    rows = []
    for row in profile.rows:
        entries = []
        for lane in row:
            kind = "lp" if lane == 1 else "schach10" if lane == 2 else "lg" if lane < 50 else "kk"
            shots = [{"position": 1, "number": n, "series": 1, "x_mm": (n - 5) * (0.55 if lane < 50 else 2.1),
                      "y_mm": ((n * 3) % 7 - 3) * (0.4 if lane < 50 else 2.5),
                      "score": {"value": 100 + n % 9, "scale": 10, "unit": "ZehntelRing"},
                      "whole_score": {"value": 100, "scale": 10, "unit": "Ring"},
                      "inner_ten": False, "invalid": False, "timestamp": now} for n in range(1, 11)]
            entries.append({"lane": lane, "occupancy": "occupied", "target": {
                "id": lane, "lane": lane, "shooter": "Unbekannt" if lane == 2 else f"Mustermann, {'Anna' if lane % 2 else 'Max'}",
                "discipline": {"lp": "LP 40", "lg": "LG Auflage 30", "kk": "KK Auflage 30", "schach10": "LG Schach 10x10 5W"}[kind], "target_kind": kind,
                "position": 0 if lane == 4 else 1, "practice": lane == 4, "shot_count": 10, "latest": shots[-1], "shots": shots,
                "total": {"value": 990, "scale": 10, "unit": "Ring"},
                "series": [{"number": 1, "score": {"value": 990, "scale": 10, "unit": "Ring"}}],
                "last_shot_at": now, "source": "sdf", "series_pending": False,
            }})
        rows.append(entries)
    return {"profile": profile.model_dump(), "revision": 1, "rows": rows,
            "sources": {source: {"state": "connected", "message": "Verbunden", "last_check_at": now} for source in ("db", "sdf")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://localhost")
    args = parser.parse_args()
    load_env()
    url = urlsplit(args.url)
    admin_url = urlunsplit(("https", f"{url.hostname}:{os.getenv('HTTPS_PORT', '443')}", "", "", "")) if url.scheme == "http" else args.url
    output = Path("test-results")
    output.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        response = context.request.get(args.url + "/", max_redirects=0)
        assert response.status == 200  # HTTP must serve the page without redirecting to HTTPS.
        assert context.request.get(admin_url + "/api/admin/auth").status == 401
        assert context.request.get(admin_url + "/admin").status == 401
        if url.scheme == "http":
            response = context.request.get(args.url + "/admin", max_redirects=0)
            assert response.status == 308
            assert response.headers["location"].startswith(admin_url)
            for method in ("GET", "POST", "PUT", "DELETE"):
                assert context.request.fetch(args.url + "/api/admin/profiles", method=method).status == 426
        fixtures = {profile.id: example(profile) for profile in seeds()}

        def snapshot(route):
            from urllib.parse import parse_qs, urlparse
            url = urlparse(route.request.url)
            profile = parse_qs(url.query).get("profile", ["alles"])[0]
            data = json.loads(json.dumps(fixtures[profile]))
            if url.path.startswith("/api/ranges/"):
                lane = int(url.path.split("/")[-1])
                data["profile"]["rows"] = [[lane]]
                entry = next((entry for row in data["rows"] for entry in row if entry["lane"] == lane), {"lane": lane, "target": None, "occupancy": "unknown"})
                data["rows"] = [[entry]]
            route.fulfill(json=data)

        page.route("**/api/snapshot?*", snapshot)
        page.route("**/api/ranges/*", snapshot)
        page.route("**/api/events?*", lambda route: route.fulfill(status=200, content_type="text/event-stream", body=": test\n\n"))
        for profile in seeds():
            profile_id, lanes = profile.id, [lane for row in profile.rows for lane in row]
            page.goto(f"{args.url}/display/{profile_id}?obs=1")
            page.wait_for_selector(".range-tile svg")
            assert page.locator(".range-tile").count() == len(lanes)
            assert [int(value) for value in page.locator(".range-tile").evaluate_all("nodes => nodes.map(node => node.dataset.lane)")] == lanes
            assert not page.locator(".display-actions").is_visible()
            assert page.locator(".display-header").is_visible()
            assert page.locator(".display-footer").is_visible()
            assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
            assert page.locator(".range-tile").evaluate_all("nodes => nodes.every(node => {const r=node.getBoundingClientRect(); return r.bottom <= innerHeight && r.right <= innerWidth})")
            assert "10,1" in page.locator(".last-score strong").first.inner_text()
            assert page.locator(".shot-time").count() == 0
            assert page.locator(".range-heading .discipline").count() == 0
            assert page.locator(".range-tile > .discipline").count() == len(lanes)
            assert page.locator("table.series").count() == 0
            assert page.locator(".total strong").first.inner_text() == "99"
            assert page.locator(".tile-stats").evaluate_all("nodes => nodes.every(n => { const items=[...n.children].map(c=>c.getBoundingClientRect()); return items.length<2 || Math.abs(items[0].top-items[1].top)<1; })")
            name = page.locator(".shooter").first
            original_name = name.inner_text()
            name.evaluate("node => node.textContent='Schwarzenberger-Müller, Maximilian Alexander'")
            assert name.evaluate("node => getComputedStyle(node).whiteSpace === 'nowrap' && getComputedStyle(node).textOverflow === 'ellipsis' && node.scrollWidth > node.clientWidth && node.clientHeight < parseFloat(getComputedStyle(node).fontSize)*1.5")
            name.evaluate("(node, text) => node.textContent=text", original_name)
            shot = page.locator('[data-shot="10"]').first
            y = float(shot.get_attribute("data-y-mm"))
            assert float(shot.locator("circle").get_attribute("cy")) == -y
            assert page.locator('[data-latest="true"]').count() == len(lanes)
            assert page.locator("svg").evaluate_all("nodes => nodes.every(svg => svg.querySelector(':scope > rect').getAttribute('fill') === '#ffffff' && getComputedStyle(svg.parentElement).backgroundColor === 'rgb(255, 255, 255)')")
            for kind in ("lg", "lp", "kk"):
                circles = page.locator(f'svg[data-target-kind="{kind}"] > circle:first-of-type')
                assert circles.evaluate_all("nodes => nodes.every(circle => circle.getAttribute('fill') === '#179c80')")
            assert page.locator("svg").evaluate_all("nodes => nodes.every(svg => { const box=svg.viewBox.baseVal; return [...svg.querySelectorAll('g[data-shot] circle')].every(c => { const x=+c.getAttribute('cx'), y=+c.getAttribute('cy'), r=+c.getAttribute('r'); return x-r>=box.x && y-r>=box.y && x+r<=box.x+box.width && y+r<=box.y+box.height; }); })")
            if profile_id == "alles":
                assert page.locator('[data-lane="1"] .phase.scored').evaluate("node => getComputedStyle(node).backgroundColor") == "rgb(201, 47, 50)"
                assert page.locator('[data-lane="4"] .phase.practice').inner_text() == "Probe"
                marker = page.locator('[data-lane="4"] .target-view .practice-marker')
                assert marker.count() == 1
                assert marker.get_attribute("aria-label") == "Probestellung"
                assert marker.evaluate("node => { const r=node.getBoundingClientRect(), t=node.parentElement.getBoundingClientRect(); return getComputedStyle(node).backgroundColor==='rgb(0, 0, 0)' && getComputedStyle(node).clipPath==='polygon(0px 0px, 100% 0px, 100% 100%)' && Math.abs(r.top-t.top)<1 && Math.abs(r.right-t.right)<1; }")
                assert page.locator('[data-lane="1"] .practice-marker').count() == 0
                assert page.locator('[data-lane="2"] .shooter').inner_text() == "Unbekannt"
                schach = page.locator('[data-lane="2"] svg[data-target-kind="schach10"]')
                assert schach.locator("g[data-cell]").count() == 100
                for cell, x, y, value in [("0,0", -45, -45, "1"), ("1,0", -45, -36, "5"), ("9,9", 36, 36, "9")]:
                    node = schach.locator(f'[data-cell="{cell}"]')
                    assert node.locator("text").text_content() == value
                    assert node.locator("rect").get_attribute("x") == str(x)
                    assert node.locator("rect").get_attribute("y") == str(y)
                    assert node.locator("rect").get_attribute("width") == "9"
                assert page.evaluate("async () => (await import('/target.js')).targetExtent('schach10', [], 'full')") == 49.5
                pistol = page.locator('[data-lane="1"] svg[data-target-kind="lp"]')
                assert pistol.locator(":scope > circle").evaluate_all("nodes => nodes.map(node => +node.getAttribute('r'))") == [77.75, 29.75, 77.75, 69.75, 61.75, 53.75, 45.75, 37.75, 29.75, 21.75, 13.75, 5.75]
                assert float(pistol.locator('g[data-shot="10"] circle').get_attribute("r")) == 2.25
                page.screenshot(path=str(output / "alles.png"))
        practice_target = fixtures["alles"]["rows"][0][3]["target"]
        practice_target.update(practice=False, position=1)
        page.goto(f"{args.url}/display/alles?obs=1")
        page.locator('[data-lane="4"] .phase.scored').get_by_text("Wertung", exact=True).wait_for()
        assert page.locator('[data-lane="4"] .practice-marker').count() == 0
        practice_target.update(practice=True, position=0)
        fixtures["alles"]["profile"]["discipline_inline"] = True
        page.goto(f"{args.url}/display/alles?obs=1")
        page.locator('.range-heading .discipline').get_by_text("LP 40", exact=True).wait_for()
        assert page.locator(".range-tile > .discipline").count() == 0
        visible_fields = fixtures["alles"]["profile"]["fields"][:]
        fixtures["alles"]["profile"]["fields"].remove("discipline")
        page.reload()
        page.wait_for_selector(".range-tile svg")
        assert page.locator(".range-tile .discipline").count() == 0
        fixtures["alles"]["profile"]["fields"] = visible_fields
        fixtures["alles"]["profile"]["discipline_inline"] = False
        table_fixture = fixtures["alles"]["rows"][0][0]["target"]
        original_series = table_fixture["series"]
        original_total = table_fixture["total"]
        table_fixture["series"] = [{"number": n, "score": {"value": value, "scale": 10, "unit": "Ring"}}
                                   for n, value in [(1, 500), (2, 600)]]
        table_fixture["total"] = {"value": 1100, "scale": 10, "unit": "Ring"}
        page.goto(f"{args.url}/display/alles?obs=1")
        table = page.locator("table.series").first
        table.locator("th").get_by_text("S2", exact=True).wait_for()
        assert table.locator("th").all_text_contents() == ["S1", "S2", "Gesamt"]
        assert table.locator("td").all_text_contents() == ["50", "60", "110"]
        assert table.evaluate("node => Math.abs(node.getBoundingClientRect().width - node.parentElement.querySelector('.target-view').getBoundingClientRect().width) < 1")
        assert page.locator('[data-lane="1"] .total').count() == 0
        fields = fixtures["alles"]["profile"]["fields"][:]
        fixtures["alles"]["profile"]["fields"].remove("total")
        page.reload()
        page.locator("table.series").first.locator("th").get_by_text("S2", exact=True).wait_for()
        assert page.locator("table.series").first.locator("th").all_text_contents() == ["S1", "S2"]
        fixtures["alles"]["profile"]["fields"] = [f for f in fields if f != "series"]
        page.reload()
        page.locator('[data-lane="1"] .total strong').get_by_text("110", exact=True).wait_for()
        assert page.locator("table.series").count() == 0
        fixtures["alles"]["profile"]["fields"] = fields
        table_fixture["series"] = []
        page.reload()
        page.locator('[data-lane="1"] .total strong').get_by_text("110", exact=True).wait_for()
        assert page.locator("table.series").count() == 0
        table_fixture["series"] = original_series
        table_fixture["total"] = original_total
        cleared_fixture = fixtures["alles"]["rows"][0][2]
        original = cleared_fixture.copy()
        cleared_fixture.update(target=None, occupancy="free")
        page.goto(f"{args.url}/display/alles?obs=1")
        page.locator('[data-lane="3"] .phase').get_by_text("Frei", exact=True).wait_for()
        assert page.locator('[data-lane="3"] svg').count() == 0
        cleared_fixture.update(occupancy="occupied", live_shooter="Neu, Anna")
        page.reload()
        page.locator('[data-lane="3"] .phase').get_by_text("Belegt", exact=True).wait_for()
        assert page.locator('[data-lane="3"] .shooter').inner_text() == "Neu, Anna"
        assert "Warten auf Ergebnisexport" in page.locator('[data-lane="3"]').inner_text()
        cleared_fixture.clear()
        cleared_fixture.update(original)
        print("Display checks passed: layouts, OBS, number formatting, SVG orientation, highlight, zoom.")

        page.unroute("**/api/events?*")
        def single_events(route):
            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(route.request.url).query)
            if "lane" not in query:
                route.fulfill(status=200, content_type="text/event-stream", body=": test\n\n")
                return
            lane = int(query["lane"][0])
            profile = query.get("profile", ["alles"])[0]
            data = json.loads(json.dumps(fixtures[profile]))
            entry = next((entry for row in data["rows"] for entry in row if entry["lane"] == lane), {"lane": lane, "target": None, "occupancy": "unknown"})
            data["profile"]["rows"], data["rows"] = [[lane]], [[entry]]
            if entry["target"]:
                entry["target"]["latest"]["score"]["value"] = 125
            route.fulfill(status=200, content_type="text/event-stream", body="event: snapshot\ndata: " + json.dumps(data) + "\n\n")
        page.route("**/api/events?*", single_events)
        page.goto(f"{args.url}/display/alles")
        page.locator('[data-lane="2"] a.lane').click()
        page.wait_for_url("**/range/2?profile=alles")
        page.locator(".last-score strong").get_by_text("12,5", exact=True).wait_for()
        assert page.locator(".range-tile").count() == 1
        assert page.locator("#profile-name").inner_text() == "Stand 2 · Alles"
        page.locator("#profiles").select_option("luftgewehr")
        page.wait_for_url("**/range/2?profile=luftgewehr")
        page.locator("#profile-name").get_by_text("Stand 2 · Luftgewehr", exact=True).wait_for()
        page.goto(f"{args.url}/range/2?obs=1")
        page.locator(".last-score strong").get_by_text("12,5", exact=True).wait_for()
        assert not page.locator(".display-actions").is_visible()
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
        page.screenshot(path=str(output / "single-range.png"))
        page.goto(f"{args.url}/display/alles?obs=2")
        page.wait_for_selector(".range-tile svg")
        assert not page.locator(".display-header").is_visible()
        assert not page.locator(".display-footer").is_visible()
        assert page.locator("#ranges").evaluate("node => {const r=node.getBoundingClientRect(); return r.top===0 && r.bottom===innerHeight;}")
        page.screenshot(path=str(output / "obs-cards.png"))
        page.locator('[data-lane="2"] a.lane').click()
        page.wait_for_url("**/range/2?profile=alles&obs=2")
        page.wait_for_selector(".range-tile svg")
        assert page.locator(".range-tile").count() == 1
        assert not page.locator(".display-header").is_visible()
        assert not page.locator(".display-footer").is_visible()
        page.set_viewport_size({"width": 640, "height": 360})
        assert page.locator("#ranges").evaluate("node => {const r=node.getBoundingClientRect(); return r.top===0 && r.bottom===innerHeight;}")
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
        page.set_viewport_size({"width": 1920, "height": 1080})
        page.goto(f"{args.url}/range/99?obs=1")
        page.locator(".target-empty").wait_for()
        assert page.locator(".range-tile").count() == 1
        assert page.locator(".range-tile").get_attribute("data-lane") == "99"
        print("Single-range checks passed: links, API display, SSE updates, profile selection, empty ranges, OBS.")

        admin = browser.new_context(ignore_https_errors=True, viewport={"width": 1600, "height": 1000},
                                    http_credentials={"username": "admin", "password": os.environ["SM_ADMIN_PASSWORD"]})
        editor = admin.new_page()
        editor.on("pageerror", lambda error: errors.append(str(error)))
        editor.goto(admin_url + "/admin")
        editor.wait_for_selector(".profile-choice")
        temporary_id = "browser-check-" + str(int(time.time()))
        editor.locator("#new-profile").click()
        editor.locator("#name").fill("Browserprüfung")
        editor.locator("#profile-id").fill(temporary_id)
        editor.locator("#rows").fill("1, 2, 3, 4, 5\n51, 52, 53, 54, 55")
        preview = editor.frame_locator("#preview")
        preview.locator("#profile-name").get_by_text("Browserprüfung", exact=True).wait_for()
        assert preview.locator(".range-tile").count() == 10
        assert preview.locator(".target-view svg").evaluate_all("nodes => nodes.every(node => node.getBoundingClientRect().height > 150)")
        editor.locator("#name").focus()
        editor.keyboard.press("Tab")
        assert editor.locator("#profile-id").evaluate("node => node === document.activeElement")
        editor.locator('#field-options input[value="discipline"]').check()
        editor.locator("#discipline-inline").check()
        preview.locator(".range-heading .discipline").first.wait_for()
        editor.locator("#save-profile").click()
        try:
            editor.locator("#save-status").get_by_text("Gespeichert · Anzeigen aktualisiert", exact=True).wait_for()
            observer = context.new_page()
            observer.goto(f"{args.url}/display/{temporary_id}?obs=1")
            observer.locator("#profile-name").get_by_text("Browserprüfung", exact=True).wait_for()
            observer.locator(".range-heading .discipline").first.wait_for()
            assert observer.locator(".range-tile > .discipline").count() == 0
            assert editor.locator("#discipline-inline").is_checked()
            editor.locator("#discipline-inline").uncheck()
            editor.locator("#save-profile").click()
            observer.locator(".range-tile > .discipline").first.wait_for()
            assert observer.locator(".range-heading .discipline").count() == 0
            editor.locator("#name").fill("Sofort aktualisiert")
            editor.locator("#save-profile").click()
            observer.locator("#profile-name").get_by_text("Sofort aktualisiert", exact=True).wait_for()
            # Second viewer uses the same backend ingestion and receives the same snapshot.
            observer2 = context.new_page()
            observer2.goto(f"{args.url}/display/{temporary_id}?obs=1")
            observer2.locator("#profile-name").get_by_text("Sofort aktualisiert", exact=True).wait_for()
            editor.screenshot(path=str(output / "editor.png"), full_page=True)
            observer.close()
            observer2.close()
            print("Editor checks passed: authentication, create, keyboard access, immediate updates, multiple viewers.")
        finally:
            response = admin.request.delete(f"{admin_url}/api/admin/profiles/{temporary_id}", headers={"Content-Type": "application/json"})
            assert response.status == 204
        assert not errors, errors
        browser.close()
    print("Screenshots: test-results/alles.png, test-results/editor.png")


if __name__ == "__main__":
    main()
