# Technical reference

[Back to the getting-started and deployment guide](../README.md).

Commands below run from the repository root. This document describes the current source and Compose configuration; local commissioning observations are not guarantees for other Meyton installations.

## Architecture

Three Compose services run two images:

- `worker` receives Meyton credentials, collects results and occupancy, and is the only result-database writer.
- `backend` serves the API and Server-Sent Events (SSE) from a read-only SQLite connection. It manages profiles and sponsor files but receives no Meyton credentials.
- `frontend` serves static browser assets and proxies the API through Caddy. It alone publishes HTTP/HTTPS host ports.

Results use `/results/results.sqlite3` on the shared `results` volume. Configuration uses the `profiles` volume. Caddy persists its internal CA in `caddy_data` and runtime configuration in `caddy_config`. Run exactly one worker and one Uvicorn backend process. Multiple viewers share the backend database watcher and SSE cache. There is no separate database server, ORM, broker, historical browsing UI, or write access to Meyton.

The backend image runs as UID 10001. Services enable `no-new-privileges`. DB and SMB accounts must have read access only; the application uses SQL read-only transactions and does not create or modify vendor files. Public HTTP/HTTPS displays are unauthenticated. Administrative requests use Basic authentication through HTTPS; cross-origin browser mutations are rejected.

## Configuration

Copy [`.env.example`](../.env.example) for a new installation. Compose injects settings into the appropriate containers; changing `.env` requires container recreation with `docker compose up`, not only `restart`.

| Setting | Default / requirement | Meaning |
| --- | --- | --- |
| `SM_DB_HOST`, `SM_DB_USER`, `SM_DB_PASS` | Required, nonempty | SSMDB2 connection credentials. |
| `SM_DB_NAME` | `SSMDB2` | Vendor database name. |
| `SM_DB_PORT` | `3306` | Vendor SQL port. |
| `SM_SMB_HOST`, `SM_SMB_USER`, `SM_SMB_PASS` | Required for SMB ingestion | SMB connection credentials; optional with a local SDF directory. |
| `SM_SDF_DIRECTORY` | Empty | Local SDF directory; takes precedence over SMB. Set the absolute host path in `.env` and uncomment the worker's local SDF environment setting and bind mount in `compose.yaml`; the worker reads its read-only mount at `/sdf`. Without Docker, set the directory the worker can access directly. |
| `SM_SMB_PORT` | `445` | SMB port. |
| `SM_SMB_SHARE` | `xml_result` | Result export share. |
| `SM_TIMEZONE` | `Europe/Berlin` | Time zone for vendor DB wall times. |
| `SM_SDF_TIMEZONE` | `Europe/Berlin`; may be empty | Override for XML timestamps; empty preserves genuine UTC timestamps. |
| `SM_LANA_HOST` | Falls back to `SM_DB_HOST` | Live-occupancy host. |
| `SM_LANA_PORT` | `53088` | LANA WebSocket port. |
| `SM_RETENTION_DAYS` | `7`; positive integer | Retention by target's last-shot activity. |
| `SM_DEMO_SHOT_INTERVAL` | `3`; positive finite number | Average seconds between demo shots; demo mode only. |
| `SM_ADMIN_PASSWORD` | Required, nonempty | Password for username `admin`; supplied by the operator. |
| `DISPLAY_ADDRESS` | `localhost` | Comma-separated viewing hostnames/IP addresses for Caddy HTTPS. |
| `HTTP_PORT`, `HTTPS_PORT` | `80`, `443` | Frontend host ports. |
| `IMAGE_TAG` | `latest` | Shared GHCR tag for both images; `dev` or an explicit release version also works. |

`RESULT_DB_PATH` and `PROFILE_PATH` are internal container paths set in Compose. The worker uses `/results/results.sqlite3`; the backend uses that database and `/data/profiles.json`. The demo override sets the worker's profile path to `/profiles/profiles.json` and mounts the same configuration volume read-only.

`SM_LANA_STARTLIST_ID` is obsolete and ignored. Start-list IDs are discovered automatically.

For deployments use `docker compose up -d --pull always --no-build`. For local source use `docker compose up -d --build --pull never`. The same Compose file has both `image` and `build`; without explicit flags, Compose can use cached images or fall back to source builds. The `dev` channel does not receive the special automatic-refresh treatment of `latest`; use `--pull always` to refresh it. See the [Compose build specification](https://docs.docker.com/reference/compose-file/build/#using-build-and-image) and [pull policies](https://docs.docker.com/reference/compose-file/services/#pull_policy).

## Demo behavior

Use `docker compose -f compose.yaml -f compose.demo.yaml up -d --pull always --no-build`. Demo mode replaces the ingestion worker; it does not add a second writer or connect to DB, SMB, or LANA. Compose still validates the required database variables, so a new demo-only `.env` needs nonempty dummy database values.

The worker follows the union of saved profile ranges, including editor changes. Ranges in `kleinkaliber` use KK 5P+10W; other ranges use LG Auflage 30. Each session fires five practice shots followed by ten KK or thirty LG scored shots, with plausible groups and matching integer/decimal scores and series. Completed targets remain for eight seconds, the stand clears for five seconds, then another simulated shooter is assigned. Shots have independent timing.

Demo mode uses the same SQLite `results` volume, backend, API, SSE, profiles, and display as live mode. Existing result records and collection baselines are preserved. Fictional names end in `(Demo)` and source status says `Demobetrieb`. Retention remains active. Returning to live collection replaces simulated display state as confirmed live occupancy and new shots arrive; it does not immediately delete all demo records. Demo playback cannot validate physical-shot latency.

## Display behavior and target mappings

These are defaults for new profile storage; existing saved profiles are not replaced.

- `/display/alles`: ranges 1–4 above 51–53.
- `/display/kleinkaliber`: ranges 51–53.
- `/display/luftgewehr`: ranges 1–4.
- `/range/2`: one live range. Click any Stand label to open its single-range view. Add `?profile=luftgewehr` to use that profile’s colors and display settings; the default is Alles. No profile edit is needed.
- Add `?obs=1` to hide operator controls, or `?obs=2` to show only range cards with no header or footer. Both modes work for profiles and single ranges. Use 1920 × 1080 for the OBS browser source. Streaming is performed by OBS.

The editor supports creating, duplicating, saving, deleting, and previewing profiles. Each row contains comma-separated range numbers. Configure colors, visible fields, practice visibility, current-series/all-hit selection, and automatic/full-target zoom. “Disziplin hinter Standnummer anzeigen” places the visible discipline beside the Stand label; leave it unchecked for a separate line. The Disziplin visibility checkbox controls both placements. Saves update connected viewers without a restart. At least one profile must remain.

Upload and remove sponsor images and edit their optional text (up to 300 characters) in the editor’s **Sponsorenbilder** section. Images are shared across all profiles: PNG, JPEG and SVG, up to 5 MiB each and 64 images total. Empty cards show their empty-range message and a random image, fitted without cropping. Each range keeps its selection while empty so live updates do not flicker; an active target always takes precedence. Without images, the existing empty-card display is used. Uploads, removals, and text changes update connected viewers immediately. SVGs cannot contain scripts, event handlers, foreign HTML, DTDs, entities or external image references; image responses use a restricted content policy.

Confirmed free ranges show an empty slot instead of their archived target. Occupied ranges wait for an export matching the live shooter. Ranges without confirmed occupancy retain their latest stored results until live occupancy is confirmed. An empty range retains its place. Practice and scored positions remain separate; practice hits do not add to scored totals. Totals use vendor values, not locally recalculated scores. Unsupported/custom disciplines retain their results but show no target geometry.

Supported standard target mappings use documented Meyton discipline IDs for LG/LP 10 m and KK 50 m events, including training events such as KK 5P+10W (41211010) and LG 5P+10W (11011010). The [Meyton discipline catalog](https://software.meyton.info/wp-content/uploads/Upload/Manuals/DE/ShootMaster/ShootMasterII_-_Disziplin_erstellen.pdf), section 19.3, supplies the standard IDs. Standard IDs take precedence. For short local IDs, the exact standard names LG Auflage 30 and LP 40 select their standard 10 m targets. LG Schach 10x10, LG Schach 10x10 5W, and the inspected local LG Schach 5W are supported using Meyton target definition 9010, obtained read-only through the installed LANA GetResults interface: a 90 mm square with 100 white 9 mm cells, black outlines, and the vendor’s 1/3/5/9 cell values. The canonical 18052 discipline family and these exact local names select this geometry. Other custom names, including Dart, remain unsupported; range numbers do not select target geometry. Geometry uses the [ISSF 2026 rule book](https://backoffice.issf-sports.org/getfile.aspx?file=ISSF-Rule-Book-2026-Edition-2025-Second-Print-07-2026-Effective-1-July-2026.pdf&inst=455&mod=docf&pane=1), rules 6.3.4.2, 6.3.4.3, and 6.3.4.6, with 4.5 mm and 5.6 mm projectile diameters. Scores always come from Meyton.

## Sources and commissioning

The worker collects all discovered ranges independently of profiles or viewers, reconciles current results once per second, and discovers new targets through incremental metadata queries. Retained historical targets are reconciled in batches of up to 50 every ten seconds. One shared SMB reader subscribes to recursive change notifications on `xml_result`. Existing files are indexed without replaying archives. A background metadata rescan every 30 seconds repairs missed notifications; reconnects also rescan. Partial XML writes are retried without discarding valid results. With `SM_SDF_DIRECTORY`, the same reader instead scans the local directory recursively every 100 ms, using modification time and size to detect new or changed XML files. Existing files are indexed without replaying archives, and partial writes use the same retries. Scan time adds to detection latency for large directories; validate it with the actual export archive. Missing or unreadable directories report a disconnected source and are retried. No SMB connection is made in local mode.

The parser requires SDF 0.2.2 (`ResultList Version="0.2.2"`); the originally inspected server exposes its schemas through `xml_schemata`. [Meyton documents SDF](https://software.meyton.info/wp-content/uploads/Upload/Manuals/DE/Schnittstellen/Schnittstelle_-_Shooting_Data_Feed.pdf) as a live result export enabled in the Kontrollzentrum. Enable it before shooting. Confirm export notifications during practice, scoring, target replacement, and position changes.

Important observed format details:

- SDF hexadecimal `TargetID` is the signed 32-bit DB `ScheibenID`.
- Coordinates use the XML `Resolution`; DB coordinates use 0.01 mm.
- Shot Ring, total Ring, and ZehntelRing values are integers scaled by 10. DB `Serien.Ring` uses whole points (scale 1); SDF series Ring uses scale 10. For example DB series 82, DB total 820, and SDF series 820 all represent 82 points.
- SDF `ShotNoTotal` is not the actual exported hit count. Count actual shots.
- SDF lacks per-shot series membership and concealment mode. Confirm these against DB; do not guess ten-shot series. Unknown concealment stays hidden. DB corrections/deletions are authoritative.
- The installed schema defines positions 1–8, while DB contains practice at `Stellung=0`. The parser accepts an explicit exported zero without renumbering positions. If SDF omits practice, practice appears through the one-second DB reconciliation.
- Observed ShootMaster 6.0 XML uses a `Z` suffix for times matching the DB's local wall time. `SM_TIMEZONE` and the default `SM_SDF_TIMEZONE` are `Europe/Berlin`. Validate this against physical shots and synchronized clocks. Set `SM_SDF_TIMEZONE=` when your XML contains genuine UTC timestamps.

Connection status and last successful source checks are independent of last shot time. Source failures retain results. Process health does not claim that sources are connected.

Live occupancy requires no start-list configuration. The worker discovers positive `StarterlistenID` values from SSMDB2 `Scheiben` every five seconds, queries the discovered contexts over one shared persistent LANA connection, and combines confirmed lane states. New event IDs are picked up automatically once they appear in SSMDB2 results. Invalid or deleted contexts do not block valid ones; contradictory responses leave the affected lane unknown. If there are no IDs yet, occupancy stays unknown and discovery retries. Cached IDs remain usable during temporary DB failures. The former `SM_LANA_STARTLIST_ID` setting is removed and ignored if left in an older `.env`.

`SM_LANA_HOST` defaults to `SM_DB_HOST`, and `SM_LANA_PORT` defaults to 53088. Only the read-only `GetLaneInfo` command is sent. Shooter identity prevents another shooter's archived result from appearing during export delays. A confirmed clear blocks the previous target until a replacement or a new shot arrives. Errors preserve results and confirmed clears; missing lanes and “no free stands” errors never prove occupancy or offline status. LANA availability and start-list restrictions vary by installation.

SSMDB2 is a result archive, not a live occupancy table. Its `Status` column contains competition flags; neither that column nor result age proves that a stand was cleared. Initial occupied state with the same shooter as an old archived target cannot establish a new session from the LANA name alone. That case still needs a confirmed clear or a new result export. Clear tracking persists in SQLite across worker and backend restarts. Live occupancy becomes unknown until the worker confirms a fresh connection.

**End-to-end physical-shot latency below 500 ms is a commissioning target, not an independently verified guarantee.** SMB registration and synthetic replay cannot prove it. If SDF omits practice, is delayed, or DB confirmation is delayed, the selected sources may fail that gate. No multicast decoder is included.

## Local result database

The worker is the only result writer. The backend opens read-only SQLite connections (`mode=ro` and `query_only`) and never accesses Meyton. Both services use the local `results` Docker volume at `/results/results.sqlite3`. The volume permits SQLite WAL/shared-memory coordination files; do not place it on an SMB or NFS share. WAL uses full synchronous commits and standard automatic checkpoints. No separate database server is needed.

There is **no historical backfill**. The first successful SSMDB2 connection records target IDs and their last-shot watermarks without storing archived results. Existing sessions enter storage only after a new shot; the worker then captures the entire session so earlier shots, positions, totals, and series remain complete. These watermarks survive restarts. Shots missed during downtime are recovered from SSMDB2. If the vendor is unavailable on first startup, the display stays empty and shows degraded sources while the worker retries. Existing stored results remain available on later offline restarts.

Set `SM_RETENTION_DAYS=7` in `.env` to retain whole targets for seven days after their last shot. Any positive integer is accepted; recreate the worker after changing it with `docker compose up -d --no-build worker`. Targets without shots use the vendor target timestamp. Polling and corrections do not refresh last-shot retention. Cleanup runs every minute using an expiry index; minimal identity/shot watermarks remain to prevent expired archives from returning. Shortening retention removes expired results; increasing it does not backfill deleted sessions. Retention deletes rows and reuses database pages; it does not shrink the file on every cleanup.

Target changes, range selection, occupancy, and cleared-target markers commit atomically. Duplicate updates cause no result writes. Replacing or clearing a range does not delete its retained session. Sessions are stored as complete normalized snapshots, not an update journal. Scores and hit positions are filtered by the existing vendor concealment rules before public responses.

One backend thread checks the committed revision every 50 ms and refreshes only current range state when results or metadata change. Every viewer uses the resulting cache. Worker heartbeats commit every five seconds; fifteen seconds without progress marks the worker degraded. HTTP health reports process health and source status separately. Compose starts the backend after the worker has initialized the local database, even if vendor sources are unavailable.

## API and profile storage

Public: `GET /api/profiles`, `GET /api/snapshot?profile=alles`, `GET /api/ranges/2?profile=alles`, `GET /api/events?profile=alles`, `GET /api/sponsors/{image_id}`, and internal `/healthz` (not exposed by Caddy).

The single-range endpoint returns the normal snapshot shape with exactly one row and one range, retaining occupancy, source status, scores, concealment, and profile settings. `GET /api/events?profile=alles&lane=2` streams the corresponding live snapshot. Range numbers must be integers from 1 to 32767. Single views also work for ranges absent from saved profiles. All ranges are collected by the shared worker; opening a view creates no vendor request, connection, or range lease. Saved profiles remain unchanged.

The SSE endpoint sends `snapshot` events containing complete ordered range states and source status. Reconnects receive a fresh snapshot. A deleted profile emits `deleted`. Slow viewers coalesce updates instead of creating an unbounded replay queue.

Protected: `GET/POST /api/admin/sponsors`, `PUT/DELETE /api/admin/sponsors/{image_id}`, `GET /api/admin/auth`, `GET/POST /api/admin/profiles`, `POST /api/admin/profiles/preview`, and `PUT/DELETE /api/admin/profiles/{id}`. Draft previews are validated and use live range state without saving. Writes require Basic authentication and JSON; cross-origin browser mutations are rejected. Profile IDs remain stable on edits.

Backend storage: `/data/profiles.json` on the `profiles` Docker volume. Saves use a temporary file, fsync, and atomic replacement. Invalid existing storage fails startup instead of silently overwriting profiles.

Sponsor uploads accept JSON with exactly `name` and `data`; `data` is base64-encoded image content. Sponsor text updates accept `{"text": "Optional display text"}` with a maximum of 300 characters. Removing an image also removes its text. Public snapshots include sponsor metadata; image responses use immutable caching and a restricted content policy.

Profile IDs use lowercase letters, digits, and hyphens, with a maximum of 48 characters. Names are nonblank and limited to 64 characters. Profiles contain 1–8 rows, each with 1–12 unique stand numbers between 1 and 32767. At most 64 profiles may be stored, and at least one must remain. Themes use `#RRGGBB` colors. `hits` is `series` or `all`; `zoom` is `auto` or `full`. Additional fields are rejected.

HTTP admin calls are rejected by Caddy; use HTTPS. Swagger, ReDoc, and the OpenAPI endpoint are disabled.

## Backup and restore

Keep `.env` in a secure backup location. Keep backups of all four volumes, including Caddy's certificate authority, before an upgrade. The examples below put application backups under Git-ignored `.data/backups/`; copy them to a separate backup destination afterward.

### Result database

Create a consistent backup while ingestion continues using SQLite's backup API:

```sh
mkdir -p .data/backups
docker compose exec worker python -m app.worker --backup /tmp/results-backup.sqlite3
docker compose cp worker:/tmp/results-backup.sqlite3 .data/backups/results-backup.sqlite3
docker compose exec worker rm /tmp/results-backup.sqlite3
```

Do not copy only the live `.sqlite3` file while WAL is active. The backup includes collection watermarks and clear tracking.

To restore, stop `worker` and `backend` first. Preserve the existing results volume, including WAL/SHM files, as a fallback. Replace the database with the backup, set ownership to UID 10001, and remove the old database's `-wal` and `-shm` sidecars before starting the services. Start the worker before the backend; normal Compose dependencies enforce this ordering.

### Profiles and sponsor files

Stop the backend briefly to avoid edits during the configuration backup:

```sh
mkdir -p .data/backups
docker compose stop backend
docker compose cp backend:/data/profiles.json .data/backups/profiles-backup.json
docker compose cp backend:/data/sponsors .data/backups/sponsors-backup
docker compose start backend
```

Copy the whole sponsor directory, including text sidecars. Restore profiles and sponsor files with the backend stopped and ownership set to UID 10001. Restart the backend afterward. Profile saves use a temporary file, fsync, and atomic replacement; invalid existing profiles fail startup rather than being overwritten with defaults.

### Caddy data

Use your Docker host's volume-backup procedure for `caddy_data` and `caddy_config`; stop the frontend while taking or restoring a consistent volume backup. The public root certificate alone cannot restore the CA. Protect backups containing Caddy's private CA key and do not distribute them to viewers. Retaining the original CA preserves trust on viewing computers.

`docker compose down` retains volumes. **`docker compose down -v` deletes results, collection watermarks, profiles, sponsor images/text, and Caddy's CA.**

## Verification

### Automated tests

Use Python 3.13 or newer. CI runs Python 3.13. Run from the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt -r requirements-dev.txt
PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend -v
.venv/bin/python -m unittest discover -s tools -p test_release_version.py -v
```

Backend tests use temporary storage and synthetic source data. Release tests cover version allocation, prerelease numbering/reset, reruns, and preventing older releases from replacing channel aliases. GitHub Actions also builds both images for `linux/amd64` and `linux/arm64`; it does not run Playwright or connect to a live Meyton host.

### Browser, storage, and deployment checks

```sh
.venv/bin/python -m playwright install chromium
.venv/bin/python tools/verify_storage_runtime.py
.venv/bin/python tools/verify_browser.py --url http://localhost
.venv/bin/python tools/verify_sponsors.py
.venv/bin/python tools/verify_runtime.py --url https://localhost
```

`verify_storage_runtime.py` starts an isolated local backend with temporary files and synthetic shots; it never connects to vendor sources. It checks worker/SQLite/SSE behavior and records a replay report in `test-results/storage-replay.json`.

The other checks require a running Compose deployment and a configured `.env`. `verify_browser.py` checks layouts, OBS bounds, zoom, orientation, score formatting, authentication, keyboard access, and updates across multiple viewers. It uses synthetic browser responses and temporarily creates/deletes profiles. Supply the HTTP base URL with its port if changed; the tool reads `HTTPS_PORT` for admin access. `verify_sponsors.py` uploads/removes temporary sponsor images and reads `HTTP_PORT`/`HTTPS_PORT`. `verify_runtime.py` trusts the copied Caddy CA, temporarily creates/deletes a profile, and **restarts the backend** to test persistence; pass the correct HTTPS URL if the hostname or port differs. Run these deployment checks when a brief interruption and temporary configuration changes are acceptable.

Screenshots and reports are saved under ignored `test-results/`; they are local artifacts, not shipped repository evidence.

### Live source commissioning and latency

With real credentials in `.env`, run the read-only source probe:

```sh
.venv/bin/python tools/probe_sources.py --seconds 30
```

The probe connects to DB and SMB and prints non-identifying stand/shot metadata. It checks the originally configured stand set (1–5 and 51–55); a successful probe does not validate every installation's range layout or physical-shot latency.

Shoot practice and scored shots while recording rendered arrivals:

```sh
.venv/bin/python tools/validate_live.py --url 'https://localhost/display/alles?obs=1' --seconds 120
```

Use a profile containing the stands under test and the correct host/port. Records go to `test-results/live-timing.csv` when new rendered shots are observed. No shots observed means no acceptance result.

- `source_visibility_ms`: vendor timestamp to worker receipt.
- `receipt_to_transaction_ms`: receipt to the timestamp captured at the start of the storage transaction.
- `storage_to_render_ms`: transaction timestamp to browser render, including commit completion, the shared watcher, SSE, and render frames.
- `application_ms`: total worker-receipt-to-render delay.
- `total_ms`: vendor timestamp to browser render.

`persisted_at` marks transaction start, not an exact commit acknowledgement. The isolated replay separately measures complete transactions through commit return. These measurements require verified, synchronized vendor/backend/browser clocks; negative delays invalidate them. Use a high-frame-rate camera showing Meyton's shot indication and the browser for an independent physical detection-to-render measurement.

Earlier commissioning notes from 2026-10-01 reported one vendor-clock observation of 142.0 ms total (121.1 ms to backend receipt, 20.9 ms to browser render), and isolated three-viewer replays below 85 ms from worker receipt to render. Those installation-specific measurements do not prove a physical latency guarantee. Earlier host availability, stand occupancy, and test counts are not current deployment status; rerun the checks above for your installation.

## Release automation

GitHub Actions validates every pull request targeting `main` or `develop`: Python 3.13 backend tests, release-version checks, and both container builds for `linux/amd64` and `linux/arm64`. Pull requests cannot publish images or releases.

Every push (including documentation-only changes and pushes containing several commits) publishes one version:

- `main`: stable releases start at `v0.1.0` and increment the patch number. Images receive the version without `v`, plus `latest`.
- `develop`: prereleases target the next stable patch, for example `v0.1.1-dev.1`, then `v0.1.1-dev.2`. After `v0.1.1`, development starts at `v0.1.2-dev.1`. Images receive the prerelease version without `v`, plus `dev`.

Images are `ghcr.io/meral-it/meyton-live-display-backend` (shared by worker and backend) and `ghcr.io/meral-it/meyton-live-display-frontend`. Git tags and GitHub Releases identify their source commit. Develop releases are marked as prereleases and never become GitHub's latest stable release.

The workflow uses GitHub's automatic `GITHUB_TOKEN`; no registry password secret is needed. GitHub Actions must be enabled, organization policy must permit the pinned actions, and publishing requires `contents: write` and `packages: write`. After the first publication, the repository owner must open each package's settings and change visibility to **Public** to allow anonymous pulls. Until then, authenticate with `docker login ghcr.io` using an account with package access and a token with `read:packages`.

Release runs across both branches share a non-canceling queue (GitHub limit: 100 queued runs). Version tags are reserved after validation, before publishing. If publishing fails, rerun the failed workflow: it reuses its tag and skips existing version images. Later pushes allocate new versions, so abandoned reservations can leave gaps. A completed release rerun does not republish; an older failed release rerun cannot move channel tags behind a newer completed release. Do not move release Git tags or overwrite version images manually.

Both version images must exist before channel aliases are updated and the GitHub Release is created. GHCR cannot update the two channel aliases atomically; a failure between updates may temporarily mix versions. Rerun that workflow to finish. Pin `IMAGE_TAG` for deployments requiring a matched pair. This workflow publishes artifacts; it does not deploy to running servers.

Release selection lives in [`tools/release_version.py`](../tools/release_version.py); the workflow is [`release.yaml`](../.github/workflows/release.yaml). Local version checks are included in the [verification commands](#automated-tests).
