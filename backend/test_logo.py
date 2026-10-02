"""Logo upload, replacement, authorization, persistence and fallback checks."""

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.sponsors import LogoStore, MAX_REQUEST_BYTES
from test_sponsors import PNG, SVG


class LogoChecks(unittest.TestCase):
    def test_upload_replace_restart_delete_and_validation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SM_ADMIN_PASSWORD": "test"}):
            path = Path(directory) / "profiles.json"
            auth = ("admin", "test")
            app = create_app(path, result_path=Path(directory) / "missing.sqlite3")
            with TestClient(app) as client:
                self.assertIsNone(client.get("/api/snapshot").json()["logo"])
                self.assertEqual(client.get("/api/logo").status_code, 404)
                for method in ("GET", "PUT", "DELETE"):
                    self.assertEqual(client.request(method, "/api/admin/logo").status_code, 401)
                payload = {"name": "logo.png", "data": PNG}
                self.assertEqual(client.put("/api/admin/logo", json=payload, auth=auth,
                                           headers={"Origin": "https://other.invalid"}).status_code, 403)
                self.assertEqual(client.put("/api/admin/logo", content=b"x" * (MAX_REQUEST_BYTES + 1), auth=auth,
                                           headers={"Content-Type": "application/json"}).status_code, 413)
                response = client.put("/api/admin/logo", json=payload, auth=auth)
                self.assertEqual(response.status_code, 200)
                first = response.json()
                self.assertEqual(client.get(first["url"]).content, base64.b64decode(PNG))
                for invalid in ({"data": PNG}, {"name": "", "data": PNG},
                                {"name": "bad.svg", "data": base64.b64encode(b'<svg onload="alert(1)"/>').decode()}):
                    self.assertEqual(client.put("/api/admin/logo", json=invalid, auth=auth).status_code, 422)
                    self.assertEqual(client.get("/api/snapshot").json()["logo"], first)
                response = client.put("/api/admin/logo", json={"name": "logo.svg", "data": base64.b64encode(SVG).decode()}, auth=auth)
                image = response.json()
                self.assertNotEqual(image["url"], first["url"])
                self.assertEqual(client.get("/api/admin/logo", auth=auth).json(), image)
                self.assertEqual(client.get("/api/snapshot").json()["logo"], image)
                public = client.get(image["url"])
                self.assertEqual(public.headers["content-type"], "image/svg+xml")
                self.assertIn("sandbox", public.headers["content-security-policy"])
                self.assertEqual(public.headers["x-content-type-options"], "nosniff")
                self.assertEqual(public.headers["cache-control"], "no-store")
            with TestClient(create_app(path, result_path=Path(directory) / "missing.sqlite3")) as client:
                self.assertEqual(client.get("/api/snapshot").json()["logo"], image)
                self.assertEqual(client.delete("/api/admin/logo", auth=auth, headers={"Content-Type": "application/json"}).status_code, 204)
                self.assertIsNone(client.get("/api/snapshot").json()["logo"])
                self.assertEqual(client.get("/api/logo").status_code, 404)
                self.assertFalse((Path(directory) / "logo.json").exists())

    def test_failed_replacement_preserves_previous_logo(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LogoStore(Path(directory) / "logo.json")
            original = store.set("original.png", PNG)
            with patch("app.sponsors.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    store.set("replacement.svg", base64.b64encode(SVG).decode())
            self.assertEqual(store.metadata, original)
            self.assertEqual(LogoStore(store.path).metadata, original)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
