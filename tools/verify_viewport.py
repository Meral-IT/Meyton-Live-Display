"""Offline viewport regression check. Run: python tools/verify_viewport.py.

Requires Playwright and Chromium (python -m playwright install chromium).
Removing dvh declarations simulates the CSS parser in older LG webOS browsers;
this is not a substitute for testing on the TV itself.
"""

import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from verify_browser import example, seeds
from app.disciplines import public_catalog


def main():
    public = Path(__file__).resolve().parents[1] / "frontend" / "public"
    profile = seeds()[0]
    snapshot = example(profile)
    # Mix targets and sponsor cards, as in the reported TV display.
    snapshot["rows"][0][0]["target"] = None
    snapshot["sponsors"] = [{
        "id": "test", "name": "Sponsor", "url": "/sponsor.svg",
        "top_text": "mit freundlicher Unterstützung durch", "text": "",
    }]
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for legacy in (True, False):
            page = browser.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                path = urlsplit(route.request.url).path
                if path == "/api/target-rules":
                    route.fulfill(json=public_catalog())
                elif path == "/api/profiles":
                    route.fulfill(json=[profile.model_dump()])
                elif path == "/api/snapshot":
                    route.fulfill(json=snapshot)
                elif path == "/api/events":
                    route.fulfill(content_type="text/event-stream", body=": test\n\n")
                elif path == "/sponsor.svg":
                    route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="240"><rect width="600" height="240" fill="green"/></svg>')
                elif path == "/style.css":
                    css = (public / "style.css").read_text()
                    if legacy:
                        css = re.sub(r"height:\s*100dvh;", "", css)
                    route.fulfill(content_type="text/css", body=css)
                else:
                    file = public / ("index.html" if path.startswith("/display/") else path.lstrip("/"))
                    route.fulfill(path=file)

            page.route("http://display.test/**", serve)
            for mode in ("", "?obs=1", "?obs=2", "?preview=1"):
                page.goto("http://display.test/display/alles" + mode)
                page.wait_for_selector(".range-tile svg", state="attached")
                for width, height in ((1920, 1080), (1280, 720), (1366, 768), (1920, 900), (640, 360)):
                    page.set_viewport_size({"width": width, "height": height})
                    bounds = page.evaluate("""() => ({
                        height: innerHeight, body: document.body.getBoundingClientRect().height,
                        scroll: document.documentElement.scrollHeight,
                        bottom: Math.max(...Array.from(document.querySelectorAll('.range-tile'), n => n.getBoundingClientRect().bottom)),
                        targetsVisible: Array.from(document.querySelectorAll('.target-view'), n => n.getBoundingClientRect().height > 0).every(Boolean)
                    })""")
                    assert bounds["body"] == height and bounds["scroll"] <= height and bounds["bottom"] <= height and (width < 1280 or bounds["targetsVisible"]), (legacy, mode, width, height, bounds)
            page.close()
        assert not errors, errors
        browser.close()
    print("Viewport checks passed: legacy/modern CSS, TV resolutions, resize, OBS and preview.")


if __name__ == "__main__":
    main()
