"""Offline checks for sponsor captions, balanced assignments and empty-card layouts."""

import base64
import copy
from collections import Counter
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.disciplines import public_catalog
from app.main import create_app
from app.profiles import seeds
from app.sponsors import DEFAULT_SPONSOR_TOP_TEXT
sys.path.insert(0, str(ROOT / "tools"))
from verify_browser import example


def main():
    profile = seeds()[0].model_dump()
    profile.update(rows=[[1, 2, 3], [4, 5]], hide_unavailable=False)
    snapshot = example(seeds()[0])
    active = copy.deepcopy(snapshot["rows"][0][0]["target"])
    snapshot.update(profile=profile, sponsors=[
        {"id": str(i), "name": f"Sponsor {i}", "url": f"/api/sponsors/{i}",
         "top_text": DEFAULT_SPONSOR_TOP_TEXT, "text": f"Sponsor {i}"} for i in range(3)])
    snapshot["rows"] = [[{"lane": lane, "target": None, "occupancy": "free"} for lane in row] for row in profile["rows"]]
    snapshot["sources"] = {name: {"state": "connected", "message": "", "last_check_at": None}
                           for name in ("worker", "local_db", "lana")}
    errors = []
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SM_ADMIN_PASSWORD": "offline-test"}), \
            TestClient(create_app(Path(directory) / "profiles.json", result_path=Path(directory) / "missing.sqlite3"),
                       base_url="http://meyton.test") as client, sync_playwright() as playwright:
        auth = ("admin", "offline-test")
        editor_image = client.post("/api/admin/sponsors", auth=auth, json={"name": "Editor.svg",
                                  "data": base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode()}).json()
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""window.EventSource = class {
            constructor() { this.listeners = {}; window.testStream = this; }
            addEventListener(name, callback) { this.listeners[name] = callback; }
            close() {}
        };""")

        def respond(route):
            path = urlsplit(route.request.url).path
            if path.startswith("/api/admin/"):
                response = client.request(route.request.method, path, auth=auth,
                                          content=route.request.post_data_buffer,
                                          headers={"Content-Type": "application/json"})
                route.fulfill(status=response.status_code, body=response.content, headers=dict(response.headers))
            elif path == "/api/profiles":
                route.fulfill(json=[snapshot["profile"]])
            elif path == "/api/target-rules":
                route.fulfill(json=public_catalog())
            elif path == "/api/snapshot" or path.startswith("/api/ranges/"):
                data = copy.deepcopy(snapshot)
                if path.startswith("/api/ranges/"):
                    lane = int(path.rsplit("/", 1)[1])
                    data["profile"]["rows"] = [[lane]]
                    data["rows"] = [[next(entry for row in data["rows"] for entry in row if entry["lane"] == lane)]]
                route.fulfill(json=data)
            elif path.startswith("/api/sponsors/"):
                route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="200"><rect width="600" height="200" fill="#087c53"/><text x="30" y="120" fill="white" font-size="50">SPONSOR</text></svg>')
            else:
                name = "index.html" if path == "/" or path.startswith(("/display/", "/range/")) else "admin.html" if path == "/admin" else path.lstrip("/")
                file = ROOT / "frontend/public" / name
                if not file.is_file():
                    route.fulfill(status=404, body="Missing")
                    return
                mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
                route.fulfill(body=file.read_bytes(), content_type=mime.get(file.suffix, "text/plain"))

        page.route("**/*", respond)
        page.clock.install()

        def choices():
            return page.locator(".range-tile:has(.sponsor-image)").evaluate_all("nodes => Object.fromEntries(nodes.map(n => [n.dataset.lane, n.querySelector('.sponsor-image').getAttribute('src')]))")

        def balanced():
            selected = choices()
            counts = Counter(selected.values())
            values = [counts[image["url"]] for image in snapshot["sponsors"]]
            assert not values or max(values) - min(values) <= 1, values
            return selected

        def update():
            page.evaluate("data => window.testStream.listeners.snapshot({data: JSON.stringify(data)})", snapshot)
            return balanced()

        page.goto("http://meyton.test/display/" + profile["id"])
        page.wait_for_selector(".sponsor-image")
        before = balanced()
        assert sorted(Counter(before.values()).values()) == [1, 2, 2]
        for _ in range(5):
            assert update() == before
        page.clock.run_for(1000)
        assert choices() == before
        urls = [image["url"] for image in snapshot["sponsors"]]
        for step in range(1, len(urls) + 1):
            page.clock.run_for(15000)
            assert balanced() == {lane: urls[(urls.index(url) + step) % len(urls)]
                                  for lane, url in before.items()}
            for lane, url in choices().items():
                card = page.locator(f'[data-lane="{lane}"]')
                assert card.locator(".sponsor-caption").last.inner_text() == f"Sponsor {urls.index(url)}"
        assert choices() == before
        # Removing the singleton's stand requires exactly one reassignment.
        singleton = next(url for url, count in Counter(before.values()).items() if count == 1)
        lane = int(next(lane for lane, url in before.items() if url == singleton))
        entry = next(e for row in snapshot["rows"] for e in row if e["lane"] == lane)
        entry["target"] = active
        after = update()
        assert len(after) == 4 and str(lane) not in after
        assert sum(before[lane] != url for lane, url in after.items()) == 1
        assert page.locator(f'[data-lane="{lane}"] svg').count() == 1
        page.locator(f'[data-lane="{lane}"]').evaluate("node => window.activeCard = node")
        page.clock.run_for(15000)
        assert page.locator(f'[data-lane="{lane}"]').evaluate("node => node === window.activeCard")
        balanced()
        entry["target"] = None
        update()
        snapshot["profile"]["hide_unavailable"] = True
        entry["occupancy"] = "unknown"
        assert str(lane) not in update()
        snapshot["profile"]["hide_unavailable"] = False
        assert str(lane) in update()
        entry["occupancy"] = "occupied"
        assert str(lane) in update()  # Waiting for export remains eligible.
        entry["occupancy"] = "free"
        sponsors = copy.deepcopy(snapshot["sponsors"])
        snapshot["sponsors"].append({"id": "3", "name": "New", "url": "/api/sponsors/3", "top_text": "", "text": ""})
        update()
        snapshot["sponsors"] = snapshot["sponsors"][1:]
        assert "/api/sponsors/0" not in update().values()
        snapshot["sponsors"] = copy.deepcopy(sponsors[:1])
        assert len(set(update().values())) == 1
        before = choices()
        page.clock.run_for(30000)
        assert choices() == before
        snapshot["sponsors"][0].pop("top_text")
        update()
        assert page.locator(".sponsor-caption").first.inner_text() == DEFAULT_SPONSOR_TOP_TEXT
        snapshot["sponsors"][0].update(top_text="", text="")
        update()
        assert page.locator(".sponsor-caption").count() == 0
        snapshot["sponsors"] = []
        assert update() == {}
        page.clock.run_for(15000)
        assert choices() == {}
        assert page.locator(".empty-symbol").count() == 5
        assert page.locator(".target-empty p").first.evaluate("n => parseFloat(getComputedStyle(n).fontSize)") >= 20
        snapshot["sponsors"] = sponsors
        snapshot["profile"]["hide_unavailable"] = False
        update()

        for width, height, suffix in [(1920, 1080, ""), (1080, 1920, ""), (1920, 1080, "?obs=1"), (1920, 1080, "?obs=2"), (1920, 1080, "?preview=1")]:
            page.set_viewport_size({"width": width, "height": height})
            page.goto("http://meyton.test/display/" + profile["id"] + suffix)
            page.wait_for_selector(".sponsor-image")
            balanced()
            assert page.locator(".target-empty").evaluate_all("""nodes => nodes.every(n => {
                const children = [...n.children];
                const image = n.querySelector('img');
                const bounds = n.getBoundingClientRect();
                return children.map(c => c.tagName).join(',') === 'P,DIV,IMG,DIV'
                    && parseFloat(getComputedStyle(children[0]).fontSize) >= 20
                    && [...n.querySelectorAll('.sponsor-caption')].every(c => parseFloat(getComputedStyle(c).fontSize) >= 16)
                    && getComputedStyle(image).objectFit === 'contain'
                    && image.getBoundingClientRect().height > 0
                    && children.every(c => { const b=c.getBoundingClientRect(); return b.left >= bounds.left-1 && b.right <= bounds.right+1 && b.top >= bounds.top-1 && b.bottom <= bounds.bottom+1; });
            })"""), (width, height, suffix, page.locator('.target-empty').first.evaluate("n => ({bounds:n.getBoundingClientRect().toJSON(), children:[...n.children].map(c => ({tag:c.tagName, text:c.textContent, font:getComputedStyle(c).fontSize, bounds:c.getBoundingClientRect().toJSON()}))})"))
            if suffix == "?preview=1":
                snapshot["sponsors"] = copy.deepcopy(sponsors[:1])
                snapshot["sponsors"][0]["top_text"] = "Vorschau oben"
                page.evaluate("snapshot => window.postMessage({type: 'profile-preview', snapshot}, location.origin)", snapshot)
                page.locator(".sponsor-caption").get_by_text("Vorschau oben", exact=True).first.wait_for()
                snapshot["sponsors"] = sponsors
            if not suffix and width == 1920:
                output = ROOT / "test-results"
                output.mkdir(exist_ok=True)
                page.screenshot(path=str(output / "empty-stands-balanced.png"))
        page.goto("http://meyton.test/range/1?obs=2")
        page.wait_for_selector(".sponsor-image")
        assert page.locator(".sponsor-image").count() == 1
        first = choices()["1"]
        seen = {first}
        for _ in range(len(sponsors)):
            page.clock.run_for(15000)
            seen.add(choices()["1"])
        assert seen == {image["url"] for image in sponsors}
        assert choices()["1"] == first
        assert page.locator(".target-empty p").evaluate("n => parseFloat(getComputedStyle(n).fontSize)") == 48

        snapshot["profile"]["rows"] = [list(range(start, start + 3)) for start in (1, 4, 7, 10)]
        snapshot["rows"] = [[{"lane": lane, "target": None, "occupancy": "free"} for lane in row] for row in snapshot["profile"]["rows"]]
        page.goto("http://meyton.test/display/" + profile["id"])
        page.wait_for_selector(".sponsor-image")
        assert page.locator(".target-empty").evaluate_all("""nodes => nodes.every(n => {
            const image = n.querySelector('img');
            const style = getComputedStyle(image);
            return image.getBoundingClientRect().height - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom) >= 40
                && n.scrollHeight <= n.clientHeight + 1;
        })"""), "Dense profiles must keep logos and captions visible"
        snapshot["sponsors"] = copy.deepcopy(sponsors[:1])
        snapshot["sponsors"][0].update(top_text="Oben " * 60, text="Unten " * 50)
        update()
        assert page.locator(".target-empty").evaluate_all("""nodes => nodes.every(n => {
            const image = n.querySelector('img');
            return getComputedStyle(n).overflowY === 'auto' && n.scrollHeight > n.clientHeight
                && image.getBoundingClientRect().height >= 64;
        })"""), "Long captions must remain accessible without collapsing logos"

        page.goto("http://meyton.test/admin")
        item = page.locator(f'.sponsor-item[data-id="{editor_image["id"]}"]')
        page.locator(".sponsor-settings summary").click()
        top = item.get_by_label("Text oben", exact=True)
        bottom = item.get_by_label("Text unten", exact=True)
        assert top.input_value() == DEFAULT_SPONSOR_TOP_TEXT
        assert bottom.input_value() == ""
        assert top.get_attribute("maxlength") == bottom.get_attribute("maxlength") == "300"
        top.fill("Oben im Editor")
        bottom.fill("Unten im Editor")
        item.get_by_role("button", name="Editor.svg: Text speichern", exact=True).click()
        page.locator("#sponsor-status").get_by_text("Text gespeichert · Anzeigen aktualisiert", exact=True).wait_for()
        saved = client.get("/api/admin/sponsors", auth=auth).json()[0]
        assert (saved["top_text"], saved["text"]) == ("Oben im Editor", "Unten im Editor")
        frame = page.frame_locator("#preview")
        frame.locator(".sponsor-caption").get_by_text("Oben im Editor", exact=True).first.wait_for()
        frame.locator(".sponsor-caption").get_by_text("Unten im Editor", exact=True).first.wait_for()
        top.fill("")
        bottom.fill("")
        item.get_by_role("button", name="Editor.svg: Text speichern", exact=True).click()
        page.wait_for_function("document.querySelector('#preview').contentDocument.querySelectorAll('.sponsor-caption').length === 0")
        page.reload()
        assert item.get_by_label("Text oben", exact=True).input_value() == ""
        assert item.get_by_label("Text unten", exact=True).input_value() == ""
        assert not errors, errors
        browser.close()
    print("Empty-stand checks passed: captions, balance, cyclic rotation, occupancy, visibility, sponsor changes and responsive/OBS/preview layouts.")


if __name__ == "__main__":
    main()
