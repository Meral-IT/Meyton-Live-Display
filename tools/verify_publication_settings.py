"""Offline admin publication form check against the real authenticated API."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import paramiko
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.main import create_app
from app.publication import SFTPPublisher


def main():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'SM_ADMIN_PASSWORD': 'test-password'}), \
            patch.object(SFTPPublisher, 'upload'):
        app = create_app(Path(directory) / 'profiles.json')
        with TestClient(app, base_url='https://meyton.test') as client, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def respond(route):
                url = urlsplit(route.request.url)
                if url.path == '/api/events':
                    route.fulfill(body='', content_type='text/event-stream')
                elif url.path.startswith('/api/'):
                    response = client.request(route.request.method, url.path + ('?' + url.query if url.query else ''),
                        auth=('admin', 'test-password'), content=route.request.post_data,
                        headers={'Content-Type': 'application/json', 'Origin': 'https://meyton.test'})
                    route.fulfill(status=response.status_code, body=response.content,
                                  content_type=response.headers.get('content-type', 'application/json'))
                else:
                    name = 'admin.html' if url.path == '/admin' else 'index.html' if url.path.startswith('/display/') else url.path.lstrip('/')
                    file = ROOT / 'frontend/public' / name
                    if not file.is_file():
                        route.fulfill(status=404, body='Missing')
                        return
                    mime = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css'}
                    route.fulfill(body=file.read_bytes(), content_type=mime.get(file.suffix, 'text/plain'))
            page.route('**/*', respond)
            page.goto('https://meyton.test/admin')
            page.wait_for_function("document.getElementById('publication-status').textContent === 'Deaktiviert'")
            cards = page.locator('details.settings-card')
            assert cards.count() == 7
            assert page.locator('.profile-settings').get_attribute('open') is not None
            for card in page.locator('.profile-preview details.settings-card').all():
                assert card.get_attribute('open') is None
                summary = card.locator(':scope > summary')
                summary.click()
                assert card.get_attribute('open') is not None
                summary.focus()
                page.keyboard.press('Enter')
                assert card.get_attribute('open') is None
            page.locator('.publication-settings > summary').click()
            page.locator('#publication-host').fill('retained.test')
            page.locator('.publication-settings > summary').click()
            assert not page.locator('#publication-host').is_visible()
            assert page.locator('#name').is_visible()
            page.locator('.publication-settings > summary').click()
            assert page.locator('#publication-host').input_value() == 'retained.test'
            page.locator('#publication-profiles').select_option(['luftgewehr'])
            assert page.locator('#publication-interval').input_value() == '60'
            page.locator('#publication-interval').select_option('300')
            page.locator('#publication-host').fill('example.test')
            page.locator('#publication-iframe-origins').fill('https://www.example.org')
            page.locator('#publication-user').fill('publisher')
            page.locator('#publication-directory').fill('.')
            page.locator('#publication-password').fill('private-password')
            key = paramiko.RSAKey.generate(1024)
            page.locator('#publication-known-hosts').fill(f'example.test {key.get_name()} {key.get_base64()}')
            page.locator('#publication-enabled').check()
            page.locator('#save-publication').click()
            page.wait_for_function("document.getElementById('publication-status').textContent.includes('Gespeichert')")
            assert app.state.runtime.publication.password == 'private-password'
            assert app.state.runtime.publication.iframe_origins == 'https://www.example.org'
            with patch.object(SFTPPublisher, 'connect'), patch.object(SFTPPublisher, 'deploy_frontend', return_value=2) as deploy:
                page.locator('#deploy-publication').click()
                page.wait_for_function("document.getElementById('publication-deploy-status').textContent.includes('2 Dateien')")
                deploy.assert_called_once()
            assert page.locator('#publication-password').input_value() == ''
            page.reload()
            page.wait_for_function("document.getElementById('publication-enabled').checked")
            page.locator('.publication-settings > summary').click()
            assert page.locator('#publication-password').input_value() == ''
            assert page.locator('#publication-password').get_attribute('placeholder') == 'Passwort gespeichert'
            assert page.locator('#publication-host').input_value() == 'example.test'
            assert page.locator('#publication-iframe-origins').input_value() == 'https://www.example.org'
            assert page.locator('#publication-interval').input_value() == '300'
            page.locator('#publication-enabled').uncheck()
            page.locator('#publication-clear-password').check()
            page.locator('#save-publication').click()
            page.wait_for_function("document.getElementById('publication-status').textContent.includes('Gespeichert')")
            assert app.state.runtime.publication is None
            assert not app.state.runtime.publication_store.settings.password
            assert not errors, errors
            browser.close()
    print('Admin publication form, persistence, password privacy and live disable passed.')


if __name__ == '__main__':
    main()
