"""Verify local Compose TLS and profile persistence across a backend restart."""

import argparse
import os
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from probe_sources import load_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://localhost")
    args = parser.parse_args()
    load_env()
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "root.crt"
        subprocess.run(["docker", "compose", "cp", "frontend:/data/caddy/pki/authorities/local/root.crt", str(root)],
                       cwd=repository, check=True, capture_output=True)
        with httpx.Client(base_url=args.url, verify=ssl.create_default_context(cafile=root), timeout=5) as client:
            response = client.get("/api/profiles")
            response.raise_for_status()
            profile = {**response.json()[0], "id": f"restart-check-{time.time_ns()}", "name": "Restart check"}
            auth = ("admin", os.environ["SM_ADMIN_PASSWORD"])
            response = client.post("/api/admin/profiles", json=profile, auth=auth)
            assert response.status_code == 201
            try:
                subprocess.run(["docker", "compose", "restart", "backend"], cwd=repository, check=True, capture_output=True)
                deadline = time.monotonic() + 30
                while True:
                    try:
                        response = client.get("/api/profiles")
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Backend did not recover")
                    time.sleep(0.25)
                assert profile in response.json()
                response = client.get(f"/api/snapshot?profile={profile['id']}")
                assert response.status_code == 200
                assert response.json()["profile"] == profile
                for method in ("post", "put", "delete"):
                    path = "/api/admin/profiles" + ("" if method == "post" else f"/{profile['id']}")
                    response = client.request(method.upper(), path, json=profile)
                    assert response.status_code == 401
            finally:
                response = client.delete(f"/api/admin/profiles/{profile['id']}", auth=auth,
                                         headers={"Content-Type": "application/json"})
                assert response.status_code == 204
    print("Runtime checks passed: verified TLS, restart persistence, unauthenticated mutations rejected.")


if __name__ == "__main__":
    main()
