"""Check sponsor uploads and empty-card images; remove only temporary test images."""

import base64
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_sources import load_env
from verify_browser import example, seeds


def main():
    load_env()
    token = "sponsor-check-" + str(time.time_ns())
    admin_url = "https://localhost:" + os.getenv("HTTPS_PORT", "443")
    public_url = "http://localhost:" + os.getenv("HTTP_PORT", "80")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        admin = browser.new_context(ignore_https_errors=True, viewport={"width": 1600, "height": 1000},
                                    http_credentials={"username": "admin", "password": os.environ["SM_ADMIN_PASSWORD"]})
        editor = admin.new_page()
        public = browser.new_context(viewport={"width": 1920, "height": 1080})
        viewer = public.new_page()
        errors = []
        editor.on("pageerror", lambda error: errors.append(str(error)))
        viewer.on("pageerror", lambda error: errors.append(str(error)))
        original = admin.request.get(admin_url + "/api/admin/sponsors").json()
        viewer.goto(public_url + "/range/99?obs=2")
        viewer.wait_for_selector(".target-empty")
        if not original:
            assert viewer.locator(".sponsor-image").count() == 0
            assert viewer.locator(".empty-symbol").count() == 1
        editor.goto(admin_url + "/admin")
        editor.wait_for_selector("#sponsor-files")
        raster = editor.evaluate("""() => {
            const c=document.createElement('canvas'); c.width=600; c.height=240;
            const x=c.getContext('2d'); x.fillStyle='#087c53'; x.fillRect(0,0,600,240);
            x.fillStyle='white'; x.font='bold 38px sans-serif'; x.fillText('DEMO SPONSOR',100,140);
            return {png:c.toDataURL('image/png').split(',')[1], jpg:c.toDataURL('image/jpeg').split(',')[1]};
        }""")
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 240"><rect width="600" height="240" fill="#275b92"/><text x="100" y="140" fill="white" font-size="38">DEMO SPONSOR</text></svg>'
        try:
            editor.locator("#sponsor-files").set_input_files([
                {"name": token + ".png", "mimeType": "image/png", "buffer": base64.b64decode(raster["png"])},
                {"name": token + ".jpeg", "mimeType": "image/jpeg", "buffer": base64.b64decode(raster["jpg"])},
                {"name": token + ".svg", "mimeType": "image/svg+xml", "buffer": svg}])
            editor.locator("#upload-sponsors").click()
            editor.locator("#sponsor-status").get_by_text("Bilder gespeichert · Anzeigen aktualisiert", exact=True).wait_for()
            images = [i for i in admin.request.get(admin_url + "/api/admin/sponsors").json() if token in i["name"]]
            assert len(images) == 3
            viewer.locator(".sponsor-image").wait_for()
            source = viewer.locator(".sponsor-image").get_attribute("src")
            viewer.wait_for_timeout(1200)
            assert viewer.locator(".sponsor-image").get_attribute("src") == source
            for image in images:
                thumbnail = editor.locator(f'.sponsor-item[data-id="{image["id"]}"] img')
                thumbnail.wait_for()
                assert thumbnail.evaluate("n => n.complete && n.naturalWidth>0"), image
                response = public.request.get(public_url + image["url"])
                assert response.status == 200
                assert "sandbox" in response.headers["content-security-policy"]
                assert "immutable" in response.headers["cache-control"]
            snapshot = public.request.get(public_url + "/api/ranges/99").json()
            snapshot["sponsors"] = images
            snapshot["rows"][0][0].update(target=None, occupancy="free")
            viewer.route("**/api/ranges/99?*", lambda r: r.fulfill(json=snapshot))
            viewer.route("**/api/events?*", lambda r: r.fulfill(content_type="text/event-stream", body=": test\n\n"))
            viewer.reload()
            viewer.locator(".sponsor-image").wait_for()
            viewer.locator(".target-empty p").get_by_text("Stand frei", exact=True).wait_for()
            assert viewer.locator(".sponsor-image").evaluate("n => getComputedStyle(n).objectFit==='contain'")
            Path("test-results").mkdir(exist_ok=True)
            viewer.screenshot(path="test-results/sponsor-empty.png")
            snapshot["rows"][0][0]["target"] = example(seeds()[0])["rows"][0][0]["target"]
            viewer.reload()
            viewer.wait_for_selector(".range-tile svg")
            assert viewer.locator(".sponsor-image").count() == 0
            snapshot["rows"][0][0]["target"] = None
            snapshot["sponsors"] = []
            viewer.reload()
            viewer.wait_for_selector(".empty-symbol")
            assert viewer.locator(".sponsor-image").count() == 0
            editor.locator(f'.sponsor-item[data-id="{images[0]["id"]}"] button').click()
            editor.locator("#sponsor-status").get_by_text("Bild entfernt · Anzeigen aktualisiert", exact=True).wait_for()
            assert admin.request.get(admin_url + images[0]["url"]).status == 404
            assert not errors, errors
            print("Sponsor checks passed: PNG/JPEG/SVG uploads, protected management, thumbnails, HTTP images, SVG policy, live updates, stable random choice, free-range message, active-range exclusion, fallback and removal.")
        finally:
            for image in admin.request.get(admin_url + "/api/admin/sponsors").json():
                if token in image["name"]:
                    assert admin.request.delete(admin_url + "/api/admin/sponsors/" + image["id"],
                                                headers={"Content-Type": "application/json"}).status == 204
            browser.close()


if __name__ == "__main__":
    main()
