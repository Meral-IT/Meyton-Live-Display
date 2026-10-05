# Public display on IONOS using SFTP

The local backend can publish selected display profiles to a separate static website. Visitors connect only to IONOS over HTTPS. No incoming connection to the range, PHP receiver, Joomla extension, or remote Meyton database is needed. The local display continues to use SSE; the public display polls a complete JSON bundle every two seconds. The upload interval is configured separately in the web UI.

Publication is disabled by default. Configure publication in the local HTTPS admin page under **Öffentliche Anzeige · SFTP**. Only explicitly selected profiles are published. Leaving **Schützennamen veröffentlichen** unchecked removes target and live-occupancy names before upload and hides the shooter field. Existing vendor score concealment remains enforced. Profile names, discipline descriptions, sponsor text and images remain public: do not put private information in those fields.

## Prepare the public website

1. Create a dedicated subdomain such as `live.example.org`, mapped to a new webspace directory. Enable HTTPS with a publicly trusted certificate. Keep Joomla in its existing directory.
2. Create a dedicated SFTP user restricted to the display website's directory, separate from Joomla. The publisher writes `live.json` and image files directly into the configured directory, alongside the exported display assets. No `published` subfolder is required. Use the directory path as visible to this SFTP account; `.` selects its login directory.
3. Configure and save publication in the local HTTPS admin page, as described below.
4. Click **Webseite bereitstellen** to upload the public frontend, including `.htaccess`. Repeat after application updates. The button compares remote files and uploads only changed files; ordinary snapshot uploads do not deploy or compare frontend assets. A separate SFTP connection handles deployment, so it does not extend the snapshot upload roundtrip.
5. Confirm Apache honors `.htaccess`: `/admin`, `/api/profiles`, `/live.json.tmp`, and directory listings must be denied. Verify JSON responses contain `Cache-Control: no-store`, `Date`, and `Last-Modified`. The browser uses HTTP dates for freshness so the range clock does not determine stale status. If those dates are missing, it falls back to the publication timestamp and requires synchronized clocks.

The host must support the SFTP `posix-rename@openssh.com` extension for atomic replacement. The publisher deliberately fails if this is unsupported; it never deletes the current snapshot first. One publisher may write to a destination. Do not run two local installations against the same destination directory.

## Configure publication in the web UI

Open the local HTTPS admin page and sign in. Under **Öffentliche Anzeige · SFTP**, select public profiles, choose whether to publish approved shooter names, choose the upload interval (default: one minute; options from ten seconds to one hour), and enter the SFTP host, port, user, and destination directory. The configured directory is used as entered, without appending a subfolder. It must map to the public display document root; use `.` when that directory is the SFTP login root.

Enter the SFTP password. The verified `known_hosts` line is optional. When left blank, the first received server key is atomically pinned in `/data/publication_known_hosts` before authentication; future connections reject changed keys, including after restarts. The first connection does not independently verify the server identity, so entering a verified key remains preferable. Obtain the SSH fingerprint from your hosting provider or another trusted channel. `ssh-keyscan` can collect candidate keys but **does not authenticate them**. Compare fingerprints before trusting them. Use `[hostname]:port` entries for a nonstandard port. When a verified key is supplied, unknown or changed keys are rejected. To replace a pinned key, verify the new key independently and enter it in the web UI.

Enable publication and save, then click **Webseite bereitstellen** for the first deployment. The button uses saved settings. Changes apply without restarting the backend. Status shows connection progress, the last successful upload or metadata heartbeat, or a generic transfer error. An upload already in progress finishes before the new configuration takes effect. A destination change reconnects using the new credentials and host key.

Passwords are sent only to the authenticated local HTTPS admin API and are never returned. Leave the password field blank to preserve the existing password; use **Gespeichertes Passwort entfernen** to clear it. The settings, including the password, are stored in `/data/publication.json` with mode `0600` on the existing `profiles` volume. Protect backups of that volume because they now contain SFTP credentials. Do not copy this file to IONOS.

SSH-key authentication remains available: provide a dedicated private key in `.data/publication-secrets`, readable by container UID 10001, then enter `/run/secrets/id_ed25519` in the optional key-file field. This directory is Git-ignored and mounted read-only. When a password and an encrypted key are both configured, the password can unlock the key or serve as password authentication.

Publication is configured only through the web UI and stored in `publication.json`. Fresh installations start with publication disabled. Existing saved settings remain unchanged. Password authentication needs no environment variables or secrets directory.

Build this source version and restart:

```sh
docker compose up -d --build --pull never
```

Use matching source exports and backend versions. Once a release containing this feature is published, its tagged images can replace the local build.

## Behavior and operation

- One complete `live.json` contains approved profile snapshots and the target-rule catalog. Each upload first writes `live.json.tmp`, then atomically replaces `live.json`.
- Referenced logo and sponsor images use content-hash filenames. Images upload before the JSON references them; unchanged images are skipped during a connected publisher session. Superseded images known to the process are removed after a successful snapshot replacement. Images left by earlier processes may require occasional manual cleanup; do not delete currently referenced files.
- Publication checks for meaningful changes at the selected interval. Unchanged results skip the JSON and image uploads; only `live.json` timestamps are refreshed via SFTP metadata to confirm the publisher is still online. Revision counters and generated server timestamps do not count as changes. Results, occupancy, source-state changes, profiles, branding, interval changes and frontend deployment trigger uploads. Existing settings without an interval default to one minute. Choices are 10, 15, 30 seconds and 1, 2, 5, 10, 15, 30, 60 minutes. Browser polling adds up to another two seconds; upload duration and vendor export latency can increase delay. JSON bundles are limited to 16 MiB.
- The local publisher counts as a viewer while enabled, avoiding the resource-saver mode's no-viewer slowdown. Existing inactivity and source-outage policies still apply.
- Upload failures preserve the previous snapshot and retry with backoff up to one hour, never faster than the selected interval. Logs report failure types without credentials or result payloads. Public viewers show stale status after 1.25 upload intervals (at least 15 seconds) and hide results after three intervals (at least five minutes). Successful publication restores the display.
- Deleted profiles and cleared stands disappear in the next successful publication. Single-stand URLs work only for stands in an approved profile. An empty publication clears existing cards.
- Disabling publication in the web UI stops updates. It does not erase files on IONOS. Remove `live.json` manually if public data must be withdrawn immediately. Hiding stale results is a display behavior, not server-side deletion or access control.
- Frontend assets deploy only when **Webseite bereitstellen** is clicked. They come from the installed backend image; rebuild/update that image before deploying an update. Changed files replace atomically, with `index.html` last. A successful deployment of changed files makes connected public displays reload on the next snapshot. Deployment owns the exported files, including `.htaccess`; unrelated files are left alone.

Link Joomla's menu to `https://live.example.org/`. Direct URLs such as `/display/luftgewehr` and `/range/1?profile=luftgewehr` work on the dedicated subdomain. For an iframe, add the exact HTTPS origin of Joomla to `frame-ancestors 'self'` in the source `hosting/.htaccess`, then rebuild the backend image and deploy. Do not use a wildcard.

## Customize public assets

The backend reads the public page from `/app/frontend/public` and Apache rules from `/app/hosting/.htaccess`. To customize `.htaccess` without rebuilding the image, copy `hosting/.htaccess` to `custom-public/.htaccess`, edit it (for example, add your website's exact HTTPS origin to `frame-ancestors` for iframe embedding), and create `compose.override.yaml`:

```yaml
services:
  backend:
    volumes:
      - ./custom-public/.htaccess:/app/hosting/.htaccess:ro
      # Optional: replace an existing public stylesheet.
      # - ./custom-public/style.css:/app/frontend/public/style.css:ro
```

Create the files before starting Compose and make them readable by container UID 10001. Run `docker compose up -d backend`, then click **Webseite bereitstellen** to upload the customized assets. Mount other existing public files the same way; only `index.html`, the bundled JavaScript/CSS, and files under `vendor/` are exported. Keep the default access restrictions, cache headers, and routing rules when editing `.htaccess`. Each deployment reapplies these mounted files, so edits made directly on the hosting server can be overwritten. Alternatively, edit the source files, rebuild the backend image, and deploy.

## Verify before sharing

```sh
PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend -v
.venv/bin/python tools/verify_public_display.py
.venv/bin/python tools/verify_publication_settings.py
```

The offline browser check covers polling, privacy, profile selection, single-stand restrictions, stale/expired states, deletion, and recovery. Before sharing the real URL, verify host-key rejection, successful atomic replacement of an existing snapshot, image rendering, denied temporary/admin paths, name settings, and outage recovery on the actual IONOS package. Credentials and live hosting are not exercised by the offline checks.
