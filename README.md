# Meyton Live Display

A German browser display for Meyton ShootMaster SSMDB2, local screens, and OBS. Three Docker containers: a Python ingestion worker, a Python display backend, and a Caddy frontend. Results live in a local SQLite database; profiles remain in one JSON file.

> Disclaimer: This is a heavily vibe-coded app and is inteded to be used only in a trusted local network.

## Start

1. Keep the existing DB and SMB settings in `.env`. For a fresh installation, copy `.env.example` to `.env` and fill in the credentials. The implementation generated a unique `SM_ADMIN_PASSWORD` in the existing `.env`; it is not printed in logs.
2. Set `DISPLAY_ADDRESS` to the addresses viewers will use, for example `localhost, 192.168.10.210`. Use your Docker host's actual LAN IP or resolvable hostname. The default is `localhost`.
3. Run:

   ```sh
   docker compose up -d --build
   ```

4. HTTP works without certificate setup. For HTTPS, trust Caddy's local CA on each viewing/OBS computer. Export the **public root certificate only**:

   ```sh
   docker compose cp frontend:/data/caddy/pki/authorities/local/root.crt ./meyton-root.crt
   ```

   On macOS, import this certificate into Keychain Access and trust it for SSL. On Windows, import it into the Trusted Root Certification Authorities store used by the browser/OBS. Restart browsers and OBS after installing it. Never distribute Caddy's private CA key. Caddy's `/data` and `/config` volumes persist its certificate authority across restarts.

5. Open `http://localhost/` or `https://localhost/`. HTTP also works with your Docker host's LAN IP; configure `DISPLAY_ADDRESS` for HTTPS access by LAN address. The editor is `/admin`; HTTP editor links redirect to HTTPS. Log in as `admin` with `SM_ADMIN_PASSWORD` from `.env`. With a custom `HTTP_PORT` or `HTTPS_PORT`, use that port explicitly in the URL.

The backend is not published to the host. Only the worker receives DB/SMB credentials. The backend and frontend receive no vendor credentials. Ports 80 and 443 are published by default; HTTP and HTTPS both serve the display without redirects. HTTP supports public API, SSE, profile selection, and OBS. Editor access redirects to HTTPS; all `/api/admin/*` requests require HTTPS, keeping admin credentials and profile writes encrypted. DB and SMB accounts should have read access only. The application issues SQL read-only transactions and never creates or modifies remote files.

## Published images and releases

GitHub Actions validates every pull request targeting `main` or `develop`: Python 3.13 backend tests, release-version checks, and both container builds for `linux/amd64` and `linux/arm64`. Pull requests cannot publish images or releases.

Every push (including documentation-only changes and pushes containing several commits) publishes one version:

- `main`: stable releases start at `v0.1.0` and increment the patch number. Images receive the version without `v`, plus `latest`.
- `develop`: prereleases target the next stable patch, for example `v0.1.1-dev.1`, then `v0.1.1-dev.2`. After `v0.1.1`, development starts at `v0.1.2-dev.1`. Images receive the prerelease version without `v`, plus `dev`.

Images are `ghcr.io/meral-it/meyton-live-display-backend` (shared by worker and backend) and `ghcr.io/meral-it/meyton-live-display-frontend`. Git tags and GitHub Releases identify their source commit. Develop releases are marked as prereleases and never become GitHub's latest stable release.

To run published images, configure `.env` as described above. Docker Compose 2.24.4 or newer is required. The registry override disables builds and pulls the selected image versions:

```sh
docker compose -f compose.yaml -f compose.registry.yaml up -d
IMAGE_TAG=dev docker compose -f compose.yaml -f compose.registry.yaml up -d
IMAGE_TAG=0.1.0 docker compose -f compose.yaml -f compose.registry.yaml up -d
```

Use one of these commands to select stable, development, or a pinned version. For demo ingestion, append `-f compose.demo.yaml` before `up -d`. Existing local-build commands remain available without the registry override.

The workflow uses GitHub's automatic `GITHUB_TOKEN`; no registry password secret is needed. GitHub Actions must be enabled, organization policy must permit the pinned actions, and publishing requires `contents: write` and `packages: write`. After the first publication, the repository owner must open each package's settings and change visibility to **Public** to allow anonymous pulls. Until then, authenticate with `docker login ghcr.io` using an account with package access and a token with `read:packages`.

Release runs across both branches share a non-canceling queue (GitHub limit: 100 queued runs). Version tags are reserved after validation, before publishing. If publishing fails, rerun the failed workflow: it reuses its tag and skips existing version images. Later pushes allocate new versions, so abandoned reservations can leave gaps. A completed release rerun does not republish; an older failed release rerun cannot move channel tags behind a newer completed release. Do not move release Git tags or overwrite version images manually.

Both version images must exist before channel aliases are updated and the GitHub Release is created. GHCR cannot update the two channel aliases atomically; a failure between updates may temporarily mix versions. Rerun that workflow to finish. Pin `IMAGE_TAG` for deployments requiring a matched pair. This workflow publishes artifacts; it does not deploy to running servers.

Run the release-version checks locally with:

```sh
python3 -m unittest discover -s tools -p test_release_version.py -v
```

## Live demo

Start the demo worker in place of real ingestion:

```sh
docker compose -f compose.yaml -f compose.demo.yaml up -d --build
```

Demo mode writes to the **same SQLite `results` volume** and uses the normal backend, API, SSE, profiles, and display. Compose replaces the worker container; only one ingestion worker runs. No DB, SMB, or LANA connections are made. Source status explicitly says `Demobetrieb`, and fictional shooter names end in `(Demo)`. Existing result records and vendor collection baselines are preserved.

The worker follows the union of all saved profile ranges, including editor changes. Ranges in `kleinkaliber` use KK 5P+10W; other ranges use LG Auflage 30. Each session fires five practice shots followed by ten KK or thirty LG scored shots, with plausible groups and matching integer/decimal scores and series. It shows completed results for eight seconds, clears the range for five seconds, then assigns a new simulated shooter. Shots have independent timing; set `SM_DEMO_SHOT_INTERVAL=3` in `.env` to change their average interval. Retention and cleanup remain active.

Return to real ingestion with:

```sh
docker compose -f compose.yaml up -d --build
```

Demo records remain subject to the configured retention period and keep their demo labels. Live occupancy and new real shots replace the simulated display state. This mode is a software demonstration; it does not validate physical-shot latency.

## Display profiles

- `/display/alles`: ranges 1–5 above 51–55.
- `/display/kleinkaliber`: ranges 51–55.
- `/display/luftgewehr`: ranges 1–5.
- `/range/2`: one live range. Click any Stand label to open its single-range view. Add `?profile=luftgewehr` to use that profile’s colors and display settings; the default is Alles. No profile edit is needed.
- Add `?obs=1` to hide operator controls, or `?obs=2` to show only range cards with no header or footer. Both modes work for profiles and single ranges. Use 1920 × 1080 for the OBS browser source. Streaming is performed by OBS.

The editor supports creating, duplicating, saving, deleting, and previewing profiles. Each row contains comma-separated range numbers. Configure colors, visible fields, practice visibility, current-series/all-hit selection, and automatic/full-target zoom. “Disziplin hinter Standnummer anzeigen” places the visible discipline beside the Stand label; leave it unchecked for a separate line. The Disziplin visibility checkbox controls both placements. Saves update connected viewers without a restart. At least one profile must remain.

Upload and remove sponsor images in the editor’s **Sponsorenbilder** section. Images are shared across all profiles: PNG, JPEG and SVG, up to 5 MiB each and 64 images total. Empty cards show their empty-range message and a random image, fitted without cropping. Each range keeps its selection while empty so live updates do not flicker; an active target always takes precedence. Without images, the existing empty-card display is used. Uploads and removals update connected viewers immediately. SVGs cannot contain scripts, event handlers, foreign HTML, DTDs, entities or external image references; image responses use a restricted content policy.

Confirmed free ranges show an empty slot instead of their archived target. Occupied ranges wait for an export matching the live shooter. Ranges without confirmed occupancy retain their latest stored results until live occupancy is confirmed. An empty range retains its place. Practice and scored positions remain separate; practice hits do not add to scored totals. Totals use vendor values, not locally recalculated scores. Unsupported/custom disciplines retain their results but show no target geometry.

Supported standard target mappings use documented Meyton discipline IDs for LG/LP 10 m and KK 50 m events, including training events such as KK 5P+10W (41211010) and LG 5P+10W (11011010). The [Meyton discipline catalog](https://software.meyton.info/wp-content/uploads/Upload/Manuals/DE/ShootMaster/ShootMasterII_-_Disziplin_erstellen.pdf), section 19.3, supplies the standard IDs. Standard IDs take precedence. For short local IDs, the exact standard names LG Auflage 30 and LP 40 select their standard 10 m targets. LG Schach 10x10, LG Schach 10x10 5W, and the inspected local LG Schach 5W are supported using Meyton target definition 9010, obtained read-only through the installed LANA GetResults interface: a 90 mm square with 100 white 9 mm cells, black outlines, and the vendor’s 1/3/5/9 cell values. The canonical 18052 discipline family and these exact local names select this geometry. Other custom names, including Dart, remain unsupported; range numbers do not select target geometry. Geometry uses the [ISSF 2026 rule book](https://backoffice.issf-sports.org/getfile.aspx?file=ISSF-Rule-Book-2026-Edition-2025-Second-Print-07-2026-Effective-1-July-2026.pdf&inst=455&mod=docf&pane=1), rules 6.3.4.2, 6.3.4.3, and 6.3.4.6, with 4.5 mm and 5.6 mm projectile diameters. Scores always come from Meyton.

## Sources and commissioning

The worker collects all discovered ranges independently of profiles or viewers, reconciles current results once per second, and discovers new targets through incremental metadata queries. Retained historical targets are reconciled in batches of up to 50 every ten seconds. One shared SMB reader subscribes to recursive change notifications on `xml_result`. Existing files are indexed without replaying archives. A background metadata rescan every 30 seconds repairs missed notifications; reconnects also rescan. Partial XML writes are retried without discarding valid results.

The inspected server exposes SDF 0.2.2 schemas through `xml_schemata`. [Meyton documents SDF](https://software.meyton.info/wp-content/uploads/Upload/Manuals/DE/Schnittstellen/Schnittstelle_-_Shooting_Data_Feed.pdf) as a live result export enabled in the Kontrollzentrum. Enable it before shooting. Confirm export notifications during practice, scoring, target replacement, and position changes.

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

`SM_LANA_HOST` defaults to `SM_DB_HOST`, and `SM_LANA_PORT` defaults to 53088. Only the read-only `GetLaneInfo` command is sent. The original installation was validated with start lists 2 and 8; ranges 2 and 3 reported free when the operator confirmed they were empty, and assigning a shooter changed range 2 to occupied while range 3 stayed free. Shooter identity prevents another shooter's archived result from appearing during export delays. A confirmed clear blocks the previous target until a replacement or a new shot arrives. Errors preserve results and confirmed clears; missing lanes and “no free stands” errors never prove occupancy or offline status. LANA availability and start-list restrictions vary by installation.

SSMDB2 is a result archive, not a live occupancy table. Its `Status` column contains competition flags; neither that column nor result age proves that a stand was cleared. Initial occupied state with the same shooter as an old archived target cannot establish a new session from the LANA name alone. That case still needs a confirmed clear or a new result export. Clear tracking persists in SQLite across worker and backend restarts. Live occupancy becomes unknown until the worker confirms a fresh connection.

**The under-500 ms physical-shot requirement is an unverified acceptance gate.** SMB registration and synthetic replay cannot prove it. If SDF omits practice, is delayed, or DB confirmation is delayed, the selected sources may fail that gate. No multicast decoder is included.

Software verification on 2026-10-01: fourteen backend checks passed; browser checks passed for all three layouts, draft previews, editing, OBS, and multiple viewers; verified HTTPS and profile persistence passed across a backend restart. Both source connections succeeded. The five-second source probe observed zero changed exports, so physical-shot latency and live practice export remain unmeasured. After extending the standard training mappings, KK 5P+10W targets on ranges 51–54 are supported. LP 40 is supported on range 1. Range 3 uses a short local ID (4) for LG Auflage 30, resolved through its exact standard name. Range 5 contains standard LG practice results, and range 55 has no stored target.

The display fixes additionally pass checks for unknown shooter normalization, red scoring badges, Schach cell values/dimensions/orientation, live empty/assigned slots, and shared read-only LANA polling. Operator-assisted occupancy checks confirmed empty and assigned states on range 2 while range 3 stayed empty. Subsequent live snapshots showed range 2 return to free and then display a newly exported practice hit; range 3 retained its empty slot. The display no longer substitutes the cleared archived target for these confirmed empty stands. A 30-second live observation captured one new scored shot on range 2: 121.1 ms from vendor timestamp to backend receipt, 20.9 ms from receipt to browser render, 142.0 ms maximum observed total (`test-results/occupancy-live-timing.csv`). The operator reported good perceived latency. This is a vendor-clock measurement; synchronized clocks and physical detection-to-render timing have not been independently verified.

## Verify

Use a local Python 3.13+ virtual environment; browser tooling is only needed for verification:

```sh
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt -r requirements-dev.txt
PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend -v
.venv/bin/python -m playwright install chromium
.venv/bin/python tools/probe_sources.py --seconds 30
.venv/bin/python tools/verify_browser.py --url http://localhost
.venv/bin/python tools/verify_runtime.py
.venv/bin/python tools/verify_storage_runtime.py
```

The probe loads `.env`, connects read-only, and prints non-identifying range/shot metadata. Browser checks use synthetic display data and temporary local profiles, then remove those profiles. They test all layouts, OBS bounds, zoom, orientation, number formatting, authentication, keyboard access, and updates across multiple viewers. Screenshots are saved under ignored `test-results/`. Runtime checks verify HTTPS using Caddy's public CA and restart the local backend to confirm volume persistence; their temporary profile is removed afterward.

For live shooting:

```sh
.venv/bin/python tools/validate_live.py --seconds 120
```

Shoot practice and scored shots while this runs. The tool records newly rendered hit identities and timing to `test-results/live-timing.csv`, with maximum observed delay. `source_visibility_ms` measures vendor timestamp to worker receipt; `receipt_to_transaction_ms` measures receipt to the local transaction timestamp; `storage_to_render_ms` includes commit completion, the shared watcher, SSE, and render frames. `application_ms` remains the total worker-receipt-to-render delay. `persisted_at` marks the start of the storage transaction, not an exact commit acknowledgement. The isolated replay check separately measures complete transactions through commit return. These measurements require verified, synchronized vendor/backend/browser clocks. Negative delays invalidate the measurement. Use a high-frame-rate camera showing Meyton's shot indication and this browser as an independent physical check. No shots observed means no acceptance result.

## Local result database

The worker is the only result writer. The backend opens read-only SQLite connections (`mode=ro` and `query_only`) and never accesses Meyton. Both services use the local `results` Docker volume at `/results/results.sqlite3`. The volume permits SQLite WAL/shared-memory coordination files; do not place it on an SMB or NFS share. WAL uses full synchronous commits and standard automatic checkpoints. No separate database server is needed.

There is **no historical backfill**. The first successful SSMDB2 connection records target IDs and their last-shot watermarks without storing archived results. Existing sessions enter storage only after a new shot; the worker then captures the entire session so earlier shots, positions, totals, and series remain complete. These watermarks survive restarts. Shots missed during downtime are recovered from SSMDB2. If the vendor is unavailable on first startup, the display stays empty and shows degraded sources while the worker retries. Existing stored results remain available on later offline restarts.

Set `SM_RETENTION_DAYS=7` in `.env` to retain whole targets for seven days after their last shot. Any positive integer is accepted; recreate the worker after changing it with `docker compose up -d worker`. Targets without shots use the vendor target timestamp. Polling and corrections do not refresh last-shot retention. Cleanup runs every minute using an expiry index; minimal identity/shot watermarks remain to prevent expired archives from returning. Shortening retention removes expired results; increasing it does not backfill deleted sessions. Retention deletes rows and reuses database pages; it does not shrink the file on every cleanup.

Target changes, range selection, occupancy, and cleared-target markers commit atomically. Duplicate updates cause no result writes. Replacing or clearing a range does not delete its retained session. Sessions are stored as complete normalized snapshots, not an update journal. Scores and hit positions are filtered by the existing vendor concealment rules before public responses.

One backend thread checks the committed revision every 50 ms and refreshes only current range state when results or metadata change. Every viewer uses the resulting cache. Worker heartbeats commit every five seconds; fifteen seconds without progress marks the worker degraded. HTTP health reports process health and source status separately. Compose starts the backend after the worker has initialized the local database, even if vendor sources are unavailable.

Create a consistent backup while ingestion continues using SQLite's backup API:

```sh
docker compose exec worker python -m app.worker --backup /tmp/results-backup.sqlite3
docker compose cp worker:/tmp/results-backup.sqlite3 ./results-backup.sqlite3
docker compose exec worker rm /tmp/results-backup.sqlite3
```

Do not copy only the live `.sqlite3` file while WAL is active. To restore, stop `worker` and `backend`, preserve the old database and its WAL/SHM files, replace the database with the backup as UID 10001, remove the old WAL/SHM sidecars, then start both services. The backup includes collection watermarks and clear tracking. Profiles and Caddy certificates use their existing separate backups.

Worker/database verification on 2026-10-01: 24 backend checks passed, including no-backfill eligibility, corrections, duplicate suppression, expiry, restart recovery, concealed results, and read-only readers. Existing browser and Docker restart/TLS checks passed. Three isolated 20-shot replays with three viewers each observed a maximum worker-receipt-to-render delay of 84.97 ms and a maximum full transaction duration of 1.32 ms. The latest replay recorded 60.84 ms and 1.17 ms; its database plus WAL/SHM files grew from 106,936 to 465,376 bytes. Reports are in `test-results/storage-replay.json`. A Docker snapshot used approximately 114 MiB across the three services; the worker averaged 1.44% of one CPU over twenty idle seconds including health checks, with vendor sources unavailable. SQLite backup integrity and deployed backend vendor isolation were verified. Live vendor query/resource behavior and physical-shot latency remain unverified because the Meyton host times out.

## API and storage

Public: `GET /api/profiles`, `GET /api/snapshot?profile=alles`, `GET /api/ranges/2?profile=alles`, `GET /api/events?profile=alles`, and internal `/healthz`.

The single-range endpoint returns the normal snapshot shape with exactly one row and one range, retaining occupancy, source status, scores, concealment, and profile settings. `GET /api/events?profile=alles&lane=2` streams the corresponding live snapshot. Range numbers must be integers from 1 to 32767. Single views also work for ranges absent from saved profiles. All ranges are collected by the shared worker; opening a view creates no vendor request, connection, or range lease. Saved profiles remain unchanged.

The SSE endpoint sends `snapshot` events containing complete ordered range states and source status. Reconnects receive a fresh snapshot. A deleted profile emits `deleted`. Slow viewers coalesce updates instead of creating an unbounded replay queue.

Protected: `GET /api/admin/auth`, `GET/POST /api/admin/profiles`, `POST /api/admin/profiles/preview`, and `PUT/DELETE /api/admin/profiles/{id}`. Draft previews are validated and use live range state without saving. Writes require Basic authentication and JSON; cross-origin browser mutations are rejected. Profile IDs remain stable on edits.

Backend storage: `/data/profiles.json` on the `profiles` Docker volume. Saves use a temporary file, fsync, and atomic replacement. Invalid existing storage fails startup instead of silently overwriting profiles. Back up:

```sh
docker compose cp backend:/data/profiles.json ./profiles-backup.json
docker compose cp backend:/data/sponsors ./sponsors-backup
```

Sponsor files live at `/data/sponsors` on the same `profiles` volume and survive restarts. Restore both backups with the backend stopped, preserving UID 10001 ownership. Restart afterward. Keep the Caddy data volume to preserve CA trust. `docker compose down` retains volumes; `docker compose down -v` deletes results, collection watermarks, profiles, sponsor images, and CA data.

Run exactly one ingestion worker and one Uvicorn backend process. Multiple viewers share one database watcher and SSE cache. There is no ORM, broker, historical browsing UI, or write access to Meyton.

Zero-configuration discovery and single-range views pass automated event-change, invalid-context, empty-database recovery, range validation, shared polling, SSE, profile selection, and OBS checks. Live event-change verification remains pending while the Meyton workstation is unreachable.
