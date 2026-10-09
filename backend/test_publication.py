"""Publication privacy, configuration and atomic upload checks."""

import base64
import asyncio
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from app.main import Runtime, create_app
from app.publication import PublicationSettings, PublicationStore, SFTPPublisher, build_publication, publication_failure, publication_signature, publish
from fastapi.testclient import TestClient
from test_app import target


class PublicationChecks(unittest.TestCase):
    def test_sponsor_captions_and_asset_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory) / "profiles.json")
            image = runtime.sponsors.add("Sponsor.svg", base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode())
            runtime.sponsors.set_text(image["id"], "Unten", "Oben")
            data, assets = build_publication(runtime, self.settings())
            published = json.loads(data)["snapshots"]["luftgewehr"]["sponsors"][0]
            self.assertEqual((published["top_text"], published["text"]), ("Oben", "Unten"))
            self.assertEqual(published["url"], "/" + next(iter(assets)))
            self.assertEqual(runtime.sponsors.images[0]["url"], image["url"])

    def settings(self, **changes):
        settings = PublicationSettings(("luftgewehr",), False, "example.test", 22, "publisher", "/meyton-live-display",
                                       "", "secret", "")
        return replace(settings, **changes)

    def test_allowlist_privacy_and_replacements(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory) / "profiles.json")
            runtime.ranges.apply(target(at=time.time()))
            runtime.ranges.live_shooters[2] = "Private, Name"
            runtime.status["db"]["message"] = "Sensitive connection error"
            runtime.logo.set("Logo", base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode())
            data, assets = build_publication(runtime, self.settings())
            text = data.decode()
            bundle = json.loads(text)
            self.assertEqual(list(bundle["snapshots"]), ["luftgewehr"])
            self.assertEqual(bundle["interval_seconds"], 60)
            self.assertNotIn("Private, Name", text)
            self.assertNotIn("Test, Ada", text)
            self.assertNotIn("Sensitive connection error", text)
            snapshot = bundle["snapshots"]["luftgewehr"]
            self.assertNotIn("shooter", snapshot["profile"]["fields"])
            self.assertEqual(snapshot["logo"]["url"], "/" + next(iter(assets)))
            self.assertEqual(len(assets), 1)
            named, _ = build_publication(runtime, self.settings(names=True))
            self.assertIn("Private, Name", named.decode())
            self.assertIn("Test, Ada", named.decode())
            self.assertEqual(runtime.snapshot("luftgewehr")["rows"][0][0]["target"]["shooter"], "Test, Ada")
            runtime.profiles.profiles = []
            cleared, assets = build_publication(runtime, self.settings())
            self.assertEqual(json.loads(cleared)["snapshots"], {})
            self.assertEqual(assets, {})

    def test_score_concealment_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory) / "profiles.json")
            runtime.ranges.apply(target(at=time.time(), mode=0))
            data, _ = build_publication(runtime, self.settings(names=True))
            target_data = json.loads(data)["snapshots"]["luftgewehr"]["rows"][0][0]["target"]
            self.assertIsNone(target_data["total"])
            self.assertIsNone(target_data["shots"][0]["score"])
            self.assertIsNone(target_data["shots"][0]["x_mm"])

    def test_settings_default_to_disabled_and_preserve_saved_config(self):
        from app.publication import PublicationConfig
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publication.json"
            store = PublicationStore(path)
            self.assertIsNone(store.active())
            self.assertEqual(store.settings.directory, ".")
            self.assertFalse(store.settings.names)
            self.assertEqual(store.settings.interval_seconds, 60)
            store.save(PublicationConfig(profiles=["luftgewehr"], host="example.test", interval_seconds=3600))
            restored = PublicationStore(path)
            self.assertEqual(restored.settings.profiles, ["luftgewehr"])
            self.assertEqual(restored.settings.host, "example.test")
            self.assertEqual(restored.settings.interval_seconds, 3600)
            for interval in (9, 11, 3601):
                with self.assertRaises(ValueError):
                    PublicationConfig(interval_seconds=interval)
            self.assertIsNone(restored.active())

    def test_unchanged_results_only_touch_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory) / "profiles.json")
            runtime.ranges.apply(target(at=time.time()))
            first, assets = build_publication(runtime, self.settings())
            runtime.revision += 10
            second, _ = build_publication(runtime, self.settings())
            self.assertEqual(publication_signature(first), publication_signature(second))
            publisher = SFTPPublisher(self.settings())
            publisher.sftp = Mock()
            publisher.sftp.stat.return_value.st_mtime = time.time() + 500
            self.assertTrue(publisher.upload(first, assets))
            count = publisher.sftp.putfo.call_count
            self.assertFalse(publisher.upload(second, assets))
            self.assertEqual(publisher.sftp.putfo.call_count, count)
            publisher.sftp.utime.assert_called_once()
            self.assertGreater(publisher.sftp.utime.call_args.args[1][1], time.time() + 490)
            runtime.ranges.apply(target(2, time.time()))
            changed, assets = build_publication(runtime, self.settings())
            self.assertNotEqual(publication_signature(first), publication_signature(changed))
            self.assertTrue(publisher.upload(changed, assets))
            self.assertEqual(publisher.sftp.putfo.call_count, count + 1)
            # Failed uploads must not record the changed signature.
            runtime.profiles.profiles[0].name = "Changed profile"
            runtime.publication_frontend_revision += 1
            failed, assets = build_publication(runtime, self.settings())
            successful = publisher.last_signature
            publisher.sftp.putfo.side_effect = OSError("failed")
            with self.assertRaises(OSError):
                publisher.upload(failed, assets)
            self.assertEqual(publisher.last_signature, successful)

    def test_publisher_waits_for_selected_interval(self):
        for interval, fail, expected in ((10, False, 10), (3600, False, 3600), (300, True, 600)):
            with tempfile.TemporaryDirectory() as directory:
                runtime = Runtime(Path(directory) / "profiles.json")
                runtime.publication = self.settings(interval_seconds=interval)
                waits = []
                async def stop_wait(awaitable, timeout):
                    awaitable.close()
                    waits.append(timeout)
                    raise asyncio.CancelledError
                with patch.object(SFTPPublisher, "upload", side_effect=OSError() if fail else None), \
                        patch("app.publication.asyncio.wait_for", side_effect=stop_wait):
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(publish(runtime))
                self.assertEqual(waits, [expected])

    def test_iframe_origins_validation_and_deployment(self):
        from app.publication import PublicationConfig
        from app.public_assets import public_assets
        self.assertIn(b"frame-ancestors 'self';", public_assets()[".htaccess"])
        origins = "https://example.org https://www.example.org:8443"
        with tempfile.TemporaryDirectory() as directory:
            store = PublicationStore(Path(directory) / "publication.json")
            store.save(PublicationConfig(enabled=True, profiles=["luftgewehr"], host="example.test",
                                         user="publisher", password="secret", iframe_origins=origins))
            self.assertEqual(PublicationStore(store.path).active().iframe_origins, origins)
            publisher = SFTPPublisher(store.active())
            publisher.sftp = Mock()
            publisher.sftp.open.side_effect = FileNotFoundError
            publisher.deploy_frontend()
            uploads = {call.args[1]: call.args[0].getvalue() for call in publisher.sftp.putfo.call_args_list}
            htaccess = uploads[".htaccess.tmp"]
            self.assertIn(("frame-ancestors 'self' " + origins + ";").encode(), htaccess)
            self.assertIn(b"RewriteRule ^(?:admin|api)", htaccess)
        for invalid in ("*", "http://example.org", "https://*.example.org", "https://example.org/path",
                        "https://user@example.org", 'https://example.org"', "https://example.org;", "https://example.org:0", "https://example.org:65536"):
            with self.subTest(origin=invalid), self.assertRaises(ValueError):
                PublicationConfig(iframe_origins=invalid)

    def test_manual_frontend_deployment_uploads_only_changed_files(self):
        from app.public_assets import public_assets
        files = public_assets()
        self.assertNotIn("admin.html", files)
        self.assertNotIn("editor.js", files)
        remote = {"app.js": files["app.js"], "style.css": b"old"}
        directories = set()
        sftp = Mock()
        def read(name, mode):
            if name not in remote:
                raise FileNotFoundError(name)
            return io.BytesIO(remote[name])
        def stat(name):
            if name == "live.json":
                return Mock(st_mtime=time.time())
            if name not in directories:
                raise FileNotFoundError(name)
        sftp.open.side_effect = read
        sftp.stat.side_effect = stat
        sftp.mkdir.side_effect = directories.add
        sftp.putfo.side_effect = lambda file, path, **kwargs: remote.update({path: file.read()})
        sftp.posix_rename.side_effect = lambda source, destination: remote.update({destination: remote.pop(source)})
        sftp.stat.return_value.st_mtime = time.time()
        publisher = SFTPPublisher(self.settings())
        publisher.sftp = sftp
        self.assertEqual(publisher.deploy_frontend(), len(files) - 1)
        self.assertIn("vendor", directories)
        self.assertEqual(remote["index.html"], files["index.html"])
        self.assertEqual(sftp.posix_rename.call_args.args, ("index.html.tmp", "index.html"))
        count = sftp.putfo.call_count
        self.assertEqual(publisher.deploy_frontend(), 0)
        self.assertEqual(sftp.putfo.call_count, count)
        with patch("app.publication.public_assets", side_effect=AssertionError("Automatic deployment is forbidden")):
            publisher.upload(b'{"snapshots":{}}', {})

    def test_atomic_upload_and_failure_preserves_last_snapshot(self):
        publisher = SFTPPublisher(self.settings())
        remote = {"live.json": b"old"}
        sftp = Mock()
        sftp.putfo.side_effect = lambda file, path, **kwargs: remote.update({path: file.read()})
        def rename(source, destination):
            remote[destination] = remote.pop(source)
        sftp.posix_rename.side_effect = rename
        publisher.sftp = sftp
        sftp.stat.return_value.st_mtime = time.time()
        new = b'{"snapshots":{},"value":"new"}'
        newer = b'{"snapshots":{},"value":"newer"}'
        publisher.upload(new, {"hash.svg": b"image"})
        self.assertEqual(remote["live.json"], new)
        self.assertEqual(sftp.posix_rename.call_args_list[0].args, ("hash.svg.tmp", "hash.svg"))
        publisher.upload(newer, {"hash.svg": b"image"})
        self.assertEqual(sftp.putfo.call_count, 3)  # Unchanged image is not uploaded twice.
        sftp.posix_rename.side_effect = OSError("Atomic rename unsupported")
        with self.assertRaises(OSError):
            publisher.upload(b'{"snapshots":{},"value":"broken"}', {})
        self.assertEqual(remote["live.json"], newer)
        sftp.remove.assert_not_called()
        self.assertIsNone(publisher.sftp)

    def test_host_key_verification_and_explicit_credentials(self):
        import paramiko
        with tempfile.TemporaryDirectory() as directory:
            key = paramiko.RSAKey.generate(1024)
            known = f"example.test {key.get_name()} {key.get_base64()}"
            publisher = SFTPPublisher(self.settings(known_hosts=known))
            with patch("paramiko.SSHClient") as client:
                publisher.connect()
                instance = client.return_value
                instance.load_host_keys.assert_not_called()
                self.assertEqual(instance.get_host_keys.return_value.add.call_args.args[:2], ("example.test", key.get_name()))
                policy = instance.set_missing_host_key_policy.call_args.args[0]
                self.assertIsInstance(policy, paramiko.RejectPolicy)
                with self.assertRaises(paramiko.SSHException):
                    policy.missing_host_key(paramiko.SSHClient(), "unknown.test", paramiko.RSAKey.generate(1024))
                kwargs = instance.connect.call_args.kwargs
                self.assertEqual(kwargs["password"], "secret")
                self.assertFalse(kwargs["allow_agent"])
                self.assertFalse(kwargs["look_for_keys"])
                instance.open_sftp.return_value.chdir.assert_called_once_with("/meyton-live-display")
                publisher.close()
                instance.close.assert_called_once()

    def test_missing_destination_is_identified_without_exposing_exception(self):
        import paramiko
        key = paramiko.RSAKey.generate(1024)
        settings = self.settings(known_hosts=f"example.test {key.get_name()} {key.get_base64()}")
        publisher = SFTPPublisher(settings)
        with patch("paramiko.SSHClient") as client:
            client.return_value.open_sftp.return_value.chdir.side_effect = FileNotFoundError("sensitive server detail")
            with self.assertRaises(FileNotFoundError):
                publisher.upload(b'{"snapshots":{}}', {})
            self.assertEqual(publisher.operation, "destination directory")
            message = publication_failure(FileNotFoundError("sensitive server detail"), publisher.operation)
            self.assertIn("Zielverzeichnis nicht gefunden", message)
            self.assertNotIn("sensitive server detail", message)
            client.return_value.open_sftp.return_value.putfo.assert_not_called()
            client.return_value.close.assert_called_once()

    def test_optional_key_pins_first_key_and_survives_restart(self):
        import paramiko
        from app.publication import PublicationConfig
        with tempfile.TemporaryDirectory() as directory:
            store = PublicationStore(Path(directory) / "publication.json")
            store.save(PublicationConfig(enabled=True, profiles=["luftgewehr"], host="example.test",
                                         user="publisher", password="secret", known_hosts=""))
            key = paramiko.RSAKey.generate(1024)
            changed = paramiko.RSAKey.generate(1024)
            client = paramiko.SSHClient()
            with patch("paramiko.SSHClient", return_value=client), patch.object(client, "connect"), \
                    patch.object(client, "open_sftp"):
                publisher = SFTPPublisher(store.active())
                publisher.connect()
                client._policy.missing_host_key(client, "example.test", key)
                publisher.close()
            path = Path(store.active().trust_file)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            restored = paramiko.SSHClient()
            restored.load_host_keys(str(path))
            self.assertTrue(restored.get_host_keys().check("example.test", key))
            self.assertFalse(restored.get_host_keys().check("example.test", changed))
            store = PublicationStore(store.path)
            self.assertEqual(store.active().trust_file, str(path))

    def test_publisher_counts_as_viewer(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory) / "profiles.json")
            runtime.publication = self.settings()
            runtime.write_viewer_presence()
            self.assertEqual(json.loads(runtime.viewer_presence_path.read_text())["viewer_count"], 1)
            runtime.write_viewer_presence(0)
            self.assertEqual(json.loads(runtime.viewer_presence_path.read_text())["viewer_count"], 0)

    def test_web_settings_auth_privacy_persistence_and_live_apply(self):
        import paramiko
        key = paramiko.RSAKey.generate(1024)
        settings = {"enabled": True, "profiles": ["luftgewehr"], "names": False, "host": "example.test",
                    "port": 22, "user": "publisher", "directory": ".", "password": "private-password",
                    "known_hosts": f"example.test {key.get_name()} {key.get_base64()}", "key_file": ""}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SM_ADMIN_PASSWORD": "test-password"}), \
                patch.object(SFTPPublisher, "upload") as upload:
            path = Path(directory) / "profiles.json"
            app = create_app(path)
            auth = ("admin", "test-password")
            url = "/api/admin/settings/publication"
            with TestClient(app, base_url="https://local.test") as client:
                self.assertEqual(client.get(url).status_code, 401)
                self.assertEqual(client.put(url, json=settings).status_code, 401)
                self.assertEqual(client.put(url, auth=auth, headers={"Origin": "https://other.test"}, json=settings).status_code, 403)
                invalid = client.put(url, auth=auth, json=settings | {"host": "wrong.test"})
                self.assertEqual(invalid.status_code, 400)
                self.assertNotIn("private-password", invalid.text)
                invalid = client.put(url, auth=auth, json=settings | {"known_hosts": "example.test ssh-rsa broken"})
                self.assertEqual(invalid.status_code, 400)
                response = client.put(url, auth=auth, json=settings)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("private-password", response.text)
                self.assertTrue(response.json()["settings"]["password_set"])
                self.assertNotIn("password", client.get(url, auth=auth).json()["settings"])
                self.assertEqual((Path(directory) / "publication.json").stat().st_mode & 0o777, 0o600)
                until = time.monotonic() + 3
                while not upload.called and time.monotonic() < until:
                    time.sleep(0.05)
                self.assertTrue(upload.called)
                self.assertEqual(app.state.runtime.publication.password, "private-password")
                public = response.json()["settings"]
                public.pop("password_set")
                public["names"] = True
                saved = client.put(url, auth=auth, json=public)
                self.assertEqual(saved.status_code, 200)
                self.assertEqual(app.state.runtime.publication.password, "private-password")
                self.assertTrue(app.state.runtime.publication.names)
                public["enabled"] = False
                public["password"] = ""
                self.assertEqual(client.put(url, auth=auth, json=public).status_code, 200)
                self.assertIsNone(app.state.runtime.publication)
                self.assertFalse(client.get(url, auth=auth).json()["settings"]["password_set"])
            restored = PublicationStore(Path(directory) / "publication.json")
            self.assertFalse(restored.settings.enabled)
            self.assertTrue(restored.settings.names)
            self.assertEqual(restored.settings.password, "")

    def test_deploy_endpoint_requires_auth_and_saved_enabled_settings(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SM_ADMIN_PASSWORD": "test-password"}):
            app = create_app(Path(directory) / "profiles.json")
            url = "/api/admin/settings/publication/deploy"
            with TestClient(app, base_url="https://local.test") as client:
                self.assertEqual(client.post(url, json={}).status_code, 401)
                auth = ("admin", "test-password")
                self.assertEqual(client.post(url, json={}, auth=auth).status_code, 409)
                app.state.runtime.publication = self.settings()
                with patch.object(SFTPPublisher, "connect"), patch.object(SFTPPublisher, "upload"), \
                        patch.object(SFTPPublisher, "deploy_frontend", return_value=3) as deploy:
                    response = client.post(url, json={}, auth=auth)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"changed_files": 3})
                    deploy.assert_called_once()
                    self.assertEqual(app.state.runtime.publication_frontend_revision, 1)
                with patch.object(SFTPPublisher, "connect", side_effect=OSError("secret")):
                    response = client.post(url, json={}, auth=auth)
                    self.assertEqual(response.status_code, 502)
                    self.assertNotIn("secret", response.text)
                self.assertFalse(app.state.runtime.publication_deploy_lock.locked())

    def test_public_export_has_no_editor(self):
        path = Path(__file__).resolve().parents[1] / "tools/export-public-display.py"
        spec = importlib.util.spec_from_file_location("export_display", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            module.export(directory)
            root = Path(directory)
            self.assertIn('name="meyton-public"', (root / "index.html").read_text())
            self.assertNotIn('/admin', (root / "index.html").read_text())
            self.assertFalse((root / "admin.html").exists())
            self.assertFalse((root / "editor.js").exists())
            self.assertFalse((root / "published").exists())
            self.assertTrue((root / "publication.js").is_file())


if __name__ == "__main__":
    unittest.main()
