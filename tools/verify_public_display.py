"""Offline public-display rendering, polling, privacy and outage checks."""

import importlib.util
import json
import sys
import tempfile
import time
from email.utils import formatdate
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.main import Runtime
from app.publication import PublicationSettings, build_publication
from test_app import target


def main():
    spec = importlib.util.spec_from_file_location("export_display", ROOT / "tools/export-public-display.py")
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        exporter.export(root / "web")
        runtime = Runtime(root / "profiles.json")
        runtime.ranges.apply(target(at=time.time()))
        settings = PublicationSettings(("luftgewehr",), False, "host", 22, "user", ".", "", "password", "", interval_seconds=10)
        state = {"age": 0, "offline": False, "fetches": 0}
        errors = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.clock.install()
            page.on("pageerror", lambda error: errors.append(str(error)))
            def respond(route):
                path = urlsplit(route.request.url).path
                assert not path.startswith('/api/'), path
                if path == '/live.json':
                    state["fetches"] += 1
                    if state["offline"]:
                        route.abort()
                        return
                    bundle, _ = build_publication(runtime, settings)
                    route.fulfill(body=bundle, content_type='application/json', headers={
                        'Date': formatdate(time.time(), usegmt=True),
                        'Last-Modified': formatdate(time.time() - state["age"], usegmt=True)})
                else:
                    name = 'index.html' if path == '/' or path.startswith(('/display/', '/range/')) else path.lstrip('/')
                    file = root / "web" / name
                    if not file.is_file():
                        route.fulfill(status=404, body='Missing')
                        return
                    mime = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css'}
                    route.fulfill(body=file.read_bytes(), content_type=mime.get(file.suffix, 'text/plain'))
            page.route('**/*', respond)
            page.goto('http://public.test/display/luftgewehr')
            page.wait_for_selector('[data-lane="1"] .total')
            assert page.locator('a[href="/admin"]').count() == 0
            assert page.locator('.lane').count() == 4
            assert page.locator('a.lane').count() == 0
            page.locator('.lane').first.click()
            assert page.url == 'http://public.test/display/luftgewehr'
            assert 'Test, Ada' not in page.content()
            assert page.locator('#profiles option').count() == 1
            before = state['fetches']
            runtime.ranges.apply(target(2, time.time()))
            page.wait_for_function("document.querySelector('[data-lane=\"1\"] .stat strong')?.textContent === '2'")
            assert state['fetches'] > before
            state['age'] = 20
            page.wait_for_function("document.getElementById('connection').textContent === 'Live-Daten veraltet'")
            assert page.locator('#ranges').is_visible()
            state['age'] = 310
            page.wait_for_function("document.getElementById('ranges').hidden")
            assert page.locator('#connection').inner_text() == 'Live-Feed nicht verfügbar'
            state['age'] = 0
            page.wait_for_function("!document.getElementById('ranges').hidden")
            settings = replace(settings, interval_seconds=3600)
            state['age'] = 310
            page.wait_for_function("document.getElementById('connection').textContent !== 'Live-Daten veraltet'")
            assert page.locator('#ranges').is_visible()
            state['age'] = 5000
            page.wait_for_function("document.getElementById('connection').textContent === 'Live-Daten veraltet'")
            assert page.locator('#ranges').is_visible()
            state['age'] = 11000
            page.wait_for_function("document.getElementById('ranges').hidden")
            settings = replace(settings, interval_seconds=10)
            state['age'] = 0
            page.wait_for_function("!document.getElementById('ranges').hidden")
            page.goto('http://public.test/range/1?profile=luftgewehr')
            page.wait_for_selector('[data-lane="1"] .total')
            assert page.locator('.range-tile').count() == 1
            page.goto('http://public.test/range/51?profile=luftgewehr')
            page.wait_for_function("document.getElementById('connection').textContent.includes('nicht veröffentlicht')")
            assert page.locator('.range-tile').count() == 0
            page.goto('http://public.test/')
            page.wait_for_selector('.range-tile')
            state['offline'] = True
            page.clock.fast_forward(301000)
            assert page.locator('#ranges').is_hidden()
            state['offline'] = False
            runtime.profiles.profiles = []
            page.clock.fast_forward(3000)
            page.wait_for_function("document.querySelectorAll('.range-tile').length === 0")
            assert not errors, errors
            browser.close()
    print('Public polling, name removal, stand allowlist, stale/expired state and recovery passed.')


if __name__ == '__main__':
    main()
