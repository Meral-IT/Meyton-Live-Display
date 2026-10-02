"""Offline logo fallback and responsive header checks; requires Playwright Chromium."""

from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parents[1] / "frontend" / "public"
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="120"><rect width="640" height="120" fill="green"/></svg>'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for image in (None, svg, "invalid image"):
            page = browser.new_page()

            def respond(route):
                path = urlsplit(route.request.url).path
                if path == "/api/logo":
                    route.fulfill(status=404 if image is None else 200,
                                  content_type="image/svg+xml", body=image or "")
                elif path in ("/app.js", "/editor.js"):
                    route.fulfill(content_type="text/javascript", body="import {updateLogo} from '/branding.js'; window.updateLogo = updateLogo;")
                else:
                    file = root / ({"/": "index.html", "/admin": "admin.html"}.get(path, path.lstrip("/")))
                    mime = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}
                    route.fulfill(body=file.read_bytes(), content_type=mime[file.suffix])

            page.route("**/*", respond)
            for width in (1440, 768, 390, 320):
                page.set_viewport_size({"width": width, "height": 1000})
                for path, selector in (("/", ".display-header"), ("/admin", ".editor-header")):
                    page.goto("http://meyton.test" + path)
                    page.wait_for_function("typeof window.updateLogo === 'function'")
                    page.evaluate("image => window.updateLogo(image)", {"url": "/api/logo?v=test"} if image else None)
                    page.wait_for_function("document.querySelector('.brand-logo').complete")
                    expected = image == svg
                    assert page.locator(".brand").evaluate("node => node.classList.contains('custom-logo')") == expected
                    header = page.locator(selector).bounding_box()
                    assert header["height"] == (55 if width <= 700 else 66)
                    assert header["x"] + header["width"] <= width + 1
                    if path == "/admin":
                        for control in ("#upload-logo", "#delete-logo"):
                            assert page.locator(control).evaluate("""node => {
                                const bounds = node.getBoundingClientRect();
                                const card = node.closest('.profile-settings').getBoundingClientRect();
                                return bounds.left >= card.left && bounds.right <= card.right;
                            }"""), (width, control)
                    if width > 700:
                        assert page.locator(".brand-title").is_visible() != expected
                        assert page.locator(".brand-logo").is_visible() == expected
                        if expected:
                            logo = page.locator(".brand-logo").bounding_box()
                            assert logo["height"] <= 44 and logo["width"] <= min(360, width * 0.25)
                            if width == 1440:
                                assert logo["width"] > 160
                    page.evaluate("window.updateLogo(null)")
                    assert not page.locator(".brand").evaluate("node => node.classList.contains('custom-logo')")
            page.close()
        browser.close()
    print("Logo, missing/invalid fallback, and matching desktop/mobile headers passed.")


if __name__ == "__main__":
    main()
