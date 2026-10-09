"""Sponsor upload validation, authorization, persistence and display snapshots."""

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.sponsors import MAX_REQUEST_BYTES, SponsorStore, image_data

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII="
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 100"><rect width="300" height="100" fill="#087c53"/><text x="20" y="60" fill="white">Sponsor</text></svg>'


class SponsorChecks(unittest.TestCase):
    def test_captions_legacy_persistence_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SponsorStore(directory)
            image = store.add("Logo.png", PNG)
            self.assertEqual(image["top_text"], "mit freundlicher Unterstützung durch")
            self.assertEqual(image["text"], "")
            legacy = Path(directory) / (image["id"] + ".txt")
            legacy.write_text("Alter Sponsorentext", encoding="utf-8")
            store = SponsorStore(directory)
            self.assertEqual(store.images[0]["text"], "Alter Sponsorentext")
            saved = store.set_text(image["id"], " Unten ", " Oben ")
            self.assertEqual((saved["top_text"], saved["text"]), ("Oben", "Unten"))
            self.assertEqual(SponsorStore(directory).images, [saved])
            saved = store.set_text(image["id"], "Neu")
            self.assertEqual(saved["top_text"], "Oben")
            with patch("app.sponsors.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    store.set_text(image["id"], "Lost bottom", "Lost top")
            self.assertEqual(store.images, [saved])
            self.assertEqual(SponsorStore(directory).images, [saved])
            for text, top in [("x" * 301, ""), ("", "x" * 301), (42, ""), ("", 42)]:
                with self.assertRaises(ValueError):
                    store.set_text(image["id"], text, top)
            cleared = store.set_text(image["id"], "", "")
            self.assertEqual((cleared["top_text"], cleared["text"]), ("", ""))
            self.assertEqual(SponsorStore(directory).images, [cleared])
            store.delete(image["id"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_caption_api_validation_and_compatibility(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SM_ADMIN_PASSWORD": "test-password"}):
            path = Path(directory) / "profiles.json"
            auth = ("admin", "test-password")
            with TestClient(create_app(path, result_path=Path(directory) / "missing.sqlite3")) as client:
                image = client.post("/api/admin/sponsors", json={"name": "Logo.png", "data": PNG}, auth=auth).json()
                url = "/api/admin/sponsors/" + image["id"]
                self.assertEqual(client.put(url, json={"text": "", "top_text": ""}).status_code, 401)
                self.assertEqual(client.put(url, auth=auth, json={"text": "", "top_text": ""},
                                            headers={"Origin": "https://other.invalid"}).status_code, 403)
                for field in ("text", "top_text"):
                    for invalid in ("x" * 301, 123, None):
                        payload = {"text": "Unten", "top_text": "Oben", field: invalid}
                        self.assertEqual(client.put(url, auth=auth, json=payload).status_code, 422)
                saved = client.put(url, auth=auth, json={"text": "Unten", "top_text": "Oben"}).json()
                self.assertEqual((saved["top_text"], saved["text"]), ("Oben", "Unten"))
                saved = client.put(url, auth=auth, json={"text": "Neu"}).json()
                self.assertEqual((saved["top_text"], saved["text"]), ("Oben", "Neu"))
                self.assertEqual(client.get("/api/snapshot").json()["sponsors"], [saved])
                with patch("app.sponsors.os.replace", side_effect=OSError("disk unavailable")):
                    self.assertEqual(client.put(url, auth=auth, json={"text": "Lost", "top_text": "Lost"}).status_code, 503)
                self.assertEqual(client.get("/api/admin/sponsors", auth=auth).json(), [saved])
            with TestClient(create_app(path, result_path=Path(directory) / "missing.sqlite3")) as client:
                self.assertEqual(client.get("/api/snapshot").json()["sponsors"], [saved])
                cleared = client.put(url, auth=auth, json={"text": "", "top_text": ""}).json()
                self.assertEqual((cleared["top_text"], cleared["text"]), ("", ""))

    def test_validation_and_atomic_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SponsorStore(directory)
            for name, data, kind in [("Logo.png", base64.b64decode(PNG), "png"),
                                     ("Logo.svg", SVG, "svg"), ("Logo.jpeg", b"\xff\xd8\xff\xe0test\xff\xd9", "jpg")]:
                saved = store.add(name, base64.b64encode(data).decode())
                self.assertTrue(saved["id"].endswith("." + kind))
                self.assertTrue(store.path(saved["id"]).is_file())
            self.assertEqual(SponsorStore(directory).images, store.images)
            before = list(store.images)
            with patch("app.sponsors.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    store.add("Another.png", PNG)
            self.assertEqual(store.images, before)
            self.assertEqual(len(list(Path(directory).iterdir())), 3)
            for invalid in [b"not an image", b'<svg><script>alert(1)</script></svg>',
                            b'<svg onload="alert(1)"/>', b'<svg><foreignObject/></svg>',
                            b'<svg><image href="https://example.com/image.png"/></svg>',
                            b'<!DOCTYPE svg [<!ENTITY x "bad">]><svg>&x;</svg>']:
                with self.assertRaises(ValueError):
                    image_data(base64.b64encode(invalid).decode())
            with self.assertRaises(ValueError):
                image_data("not base64")
            with self.assertRaises(FileNotFoundError):
                store.path("../profiles.json")
            for image in before:
                store.delete(image["id"])
            self.assertEqual(SponsorStore(directory).images, [])

    def test_api_auth_limits_snapshot_and_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SM_ADMIN_PASSWORD": "test-password"}):
            profile_path = Path(directory) / "profiles.json"
            app = create_app(profile_path, result_path=Path(directory) / "missing.sqlite3")
            auth = ("admin", "test-password")
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/snapshot").json()["sponsors"], [])
                self.assertEqual(client.get("/api/admin/sponsors").status_code, 401)
                self.assertEqual(client.post("/api/admin/sponsors", json={"name": "Logo.png", "data": PNG}).status_code, 401)
                self.assertEqual(client.post("/api/admin/sponsors", auth=auth, content=b"x" * (MAX_REQUEST_BYTES + 1),
                                             headers={"Content-Type": "application/json"}).status_code, 413)
                self.assertEqual(client.post("/api/admin/sponsors", json={"name": "Logo.png", "data": PNG}, auth=auth,
                                             headers={"Origin": "https://other.invalid"}).status_code, 403)
                self.assertEqual(client.post("/api/admin/sponsors", json={"name": "Bad.svg", "data": "bad"}, auth=auth).status_code, 422)
                response = client.post("/api/admin/sponsors", json={"name": "../Sponsor.svg", "data": base64.b64encode(SVG).decode()}, auth=auth)
                self.assertEqual(response.status_code, 201)
                image = response.json()
                self.assertEqual(client.get("/api/snapshot").json()["sponsors"], [image])
                self.assertEqual(client.get("/api/admin/sponsors", auth=auth).json(), [image])
                response = client.get(image["url"])
                self.assertEqual(response.status_code, 200)
                self.assertIn("image/svg+xml", response.headers["content-type"])
                self.assertIn("sandbox", response.headers["content-security-policy"])
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(client.delete("/api/admin/sponsors/" + image["id"], headers={"Content-Type": "application/json"}).status_code, 401)
            with TestClient(create_app(profile_path, result_path=Path(directory) / "missing.sqlite3")) as client:
                self.assertEqual(client.get("/api/snapshot").json()["sponsors"], [image])
                self.assertEqual(client.delete("/api/admin/sponsors/" + image["id"], auth=auth,
                                               headers={"Content-Type": "application/json"}).status_code, 204)
                self.assertEqual(client.get(image["url"]).status_code, 404)
                self.assertEqual(client.get("/api/snapshot").json()["sponsors"], [])


if __name__ == "__main__":
    unittest.main()
