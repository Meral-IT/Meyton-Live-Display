# Install Meyton Live Display on openSUSE Leap

This guide runs the published container images on an openSUSE Leap host on your trusted range LAN. You only need `compose.yaml` and `.env`; Git, Python, Node.js, and a source build are not required. Use an `x86_64` (amd64) or `aarch64` (arm64) host with sudo access and internet access to download packages and images.

Public displays show shooter names and results without authentication. Keep the installation on a trusted LAN and do not forward its ports from the internet.

## 1. Install Docker and Compose

Install Docker from your Leap repositories and enable it at boot:

```sh
sudo zypper refresh
sudo zypper install docker curl openssl
sudo systemctl enable --now docker
sudo docker version
```

openSUSE documents installing its Docker package in [Hands on with Docker](https://news.opensuse.org/2018/05/04/hands-on-with-docker-opensuse-leap-15/).

Install the Compose plugin:

```sh
sudo zypper install docker-compose
sudo docker compose version
```

Package availability depends on your Leap release and enabled repositories. The final command must work and report Compose v2 or newer. The old Python-based `docker-compose` v1 is unsuitable. If the package is unavailable or does not provide `docker compose`, use Docker's [manual plugin installation instructions](https://docs.docker.com/compose/install/linux/#install-the-plugin-manually), selecting your host architecture and the **all-users** location `/usr/local/lib/docker/cli-plugins` so that `sudo docker compose` finds it. Manually installed plugins require manual updates.

All Docker commands below use `sudo`; adding your login to the Docker group is unnecessary.

## 2. Copy the deployment files

Create an installation directory owned by your current login:

```sh
sudo install -d -m 700 -o "$(id -un)" -g "$(id -gn)" /opt/meyton-live-display
cd /opt/meyton-live-display
```

Download the Compose file and configuration example from the repository's stable branch:

```sh
curl --fail --location https://raw.githubusercontent.com/Meral-IT/Meyton-Live-Display/main/compose.yaml --output compose.yaml
curl --fail --location https://raw.githubusercontent.com/Meral-IT/Meyton-Live-Display/main/.env.example --output .env.example
cp .env.example .env
chmod 600 .env
```

Alternatively, copy `compose.yaml` and `.env.example` from an existing repository checkout into this directory, then create `.env` as above. Use files from the same revision. For a pinned deployment, download both files from the corresponding release tag instead of `main`, and set `IMAGE_TAG` to that release's version without its leading `v`.

Run the `cp` command only for a new installation: it overwrites an existing configuration. Always run the following Compose commands from `/opt/meyton-live-display`.

## 3. Prepare the Meyton connection

Enable SDF result export in the Meyton Kontrollzentrum before shooting. Obtain read-only credentials for the SSMDB2 database and, for remote SDF access, the SMB result share. On the central ShootMaster workstation, you can use its local export directory instead; see the [local SDF setup](../README.md#3-start-the-published-stable-images). The application connects to the existing Meyton services; you do not install a database server or mount an SMB share on this host.

Allow connections **from the containers on the Docker host to the Meyton server**:

| Service | Default destination port | Purpose |
| --- | --- | --- |
| SSMDB2 | TCP 3306 | Result metadata and reconciliation. |
| SMB | TCP 445 | SDF XML results in the `xml_result` share. |
| LANA | TCP 53088 | Live stand occupancy over WebSocket. |

Adjust ports to match your installation. These are outgoing connections, not ports to publish on the display host. For vendor setup details, see [sources and commissioning](../docs/technical-reference.md#sources-and-commissioning).

## 4. Configure `.env`

Edit the file with your preferred text editor, for example:

```sh
vi .env
```

Set these values using your actual addresses and credentials:

```dotenv
SM_DB_HOST=192.168.10.200
SM_DB_USER=meyton
SM_DB_PASS='replace-with-database-password'
SM_DB_NAME=SSMDB2
SM_DB_PORT=3306

SM_SMB_HOST=192.168.10.200
SM_SMB_USER=meyton
SM_SMB_PASS='replace-with-share-password'
SM_SMB_PORT=445
SM_SMB_SHARE=xml_result

SM_ADMIN_PASSWORD=replace-with-a-unique-generated-password
DISPLAY_ADDRESS=localhost, 192.168.10.50
HTTP_PORT=80
HTTPS_PORT=443
IMAGE_TAG=latest
```

Here `192.168.10.200` is the example Meyton server and `192.168.10.50` is the example **openSUSE Docker host**. Give the Docker host a fixed LAN address or DHCP reservation. Include every hostname/IP viewers will use in `DISPLAY_ADDRESS`, separated by commas; hostnames must resolve to this host on viewing computers.

Generate an admin password and paste the output into `SM_ADMIN_PASSWORD`:

```sh
openssl rand -hex 24
```

Database and admin passwords must be nonempty; SMB credentials are required only for SMB access. Single-quote passwords containing `$` or `#` to prevent Compose interpolation or comment parsing; see Docker's [`.env` syntax](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/#env-file-syntax). Keep `.env` private and out of source control.

Keep `SM_TIMEZONE=Europe/Berlin` and `SM_SDF_TIMEZONE=Europe/Berlin` for Meyton timestamps representing German local time. Set `SM_SDF_TIMEZONE=` only if your XML timestamps represent genuine UTC. `SM_LANA_HOST` can stay empty when LANA runs on `SM_DB_HOST`. `SM_RETENTION_DAYS=7` keeps completed sessions for seven days after their last shot. See the [configuration reference](../docs/technical-reference.md#configuration) for all settings.

If ports 80/443 are occupied, choose free ports, such as `HTTP_PORT=8080` and `HTTPS_PORT=8443`. Use those ports in browser URLs and firewall rules below.

## 5. Check LAN access and the firewall

Viewers need TCP access to the Docker host's HTTP and HTTPS ports. If firewalld is active, inspect the LAN interface's zone:

```sh
sudo firewall-cmd --state
sudo firewall-cmd --get-active-zones
```

If the LAN interface uses `public`, the commands for the default ports are:

```sh
sudo firewall-cmd --permanent --zone=public --add-service=http
sudo firewall-cmd --permanent --zone=public --add-service=https
sudo firewall-cmd --reload
```

Replace `public` with your actual LAN zone. For custom ports, use `--add-port=8080/tcp` and `--add-port=8443/tcp` instead of the service options. Skip these commands if firewalld is not running. See openSUSE's [firewalld documentation](https://doc.opensuse.org/documentation/leap/security/single-html/book-security/#sec-security-firewall-firewalld).

The supplied Compose file publishes ports on all host interfaces. Docker also manages forwarding rules, so ordinary host firewall service settings alone are not an access restriction for published containers. Keep the host on the trusted LAN and verify access from another LAN computer. See [Docker's firewalld integration](https://docs.docker.com/engine/network/packet-filtering-firewalls/#integration-with-firewalld).

## 6. Start and verify

Validate the configuration without printing credentials, then start the published images:

```sh
sudo docker compose config --quiet
sudo docker compose up -d --pull always --no-build
sudo docker compose ps
sudo docker compose logs --tail=100 worker backend frontend
```

`--no-build` is required for this deployment: the copied Compose file also contains source-build definitions, but no source checkout is installed. The worker and backend use the backend image; the frontend serves the browser interface and HTTPS. Their `unless-stopped` restart policy and the enabled Docker service bring them back after a reboot unless you explicitly stopped them.

Open `http://192.168.10.50/display/alles` from a LAN computer, substituting your Docker host address. Check source status and fire a new shot with SDF export enabled. A healthy container alone does not prove the Meyton connection works. Existing archived targets are not imported on first setup; new results appear as shots arrive.

## 7. Trust HTTPS and open the profile editor

Caddy creates a local certificate authority. Export its public root certificate after the frontend has started:

```sh
sudo docker compose cp frontend:/data/caddy/pki/authorities/local/root.crt ./meyton-root.crt
```

Copy `meyton-root.crt` securely to each viewing or OBS computer and trust it as a root CA. On Windows use the Trusted Root Certification Authorities store; on macOS use Keychain Access and enable SSL trust. For an openSUSE viewer, install it into the system trust store:

```sh
sudo install -m 644 meyton-root.crt /etc/pki/trust/anchors/meyton-root.crt
sudo update-ca-certificates
```

Restart the browser or OBS. If a browser uses its own certificate store, import the same root there as well. Distribute only the public `root.crt`, never Caddy's private CA key.

Open `https://192.168.10.50/admin` and log in with username `admin` and your `SM_ADMIN_PASSWORD`. The editor requires HTTPS. If using a custom HTTPS port, use a URL such as `https://192.168.10.50:8443/admin`. Configure your actual stand numbers and display profiles there.

## 8. Operate and update

```sh
cd /opt/meyton-live-display
sudo docker compose ps
sudo docker compose logs --tail=100 worker backend frontend
sudo docker compose stop
sudo docker compose start
```

Results, profiles/sponsor files, and Caddy's certificates persist in the `results`, `profiles`, `caddy_data`, and `caddy_config` named volumes. Back up `.env` and all four volumes before upgrades; follow [backup and restore](../docs/technical-reference.md#backup-and-restore). Keep the Compose project name `meyton-live-display` unchanged to continue using those volumes.

Read release notes, update `compose.yaml` when required using files matching your selected release/channel, and keep your existing `.env`. Compare any new `.env.example` with your configuration rather than overwriting it. Update the images, or apply `.env` changes, with:

```sh
sudo docker compose config --quiet
sudo docker compose up -d --pull always --no-build
```

`restart` alone does not apply changed environment variables. `latest` follows stable published images; set `IMAGE_TAG` to an explicit version from [GitHub Releases](https://github.com/Meral-IT/Meyton-Live-Display/releases) to pin it.

`docker compose down` removes containers but retains volumes. **Do not run `docker compose down -v`: it deletes results, profiles, sponsors, and the certificate authority.**

If startup fails, check `sudo journalctl -u docker --no-pager -n 100` and Compose logs. For empty displays, check source status, credentials, network reachability, SDF export, and a new shot. For certificate errors, check `DISPLAY_ADDRESS` and root trust. For image pull errors, check the image tag, internet access, and GHCR package access; keep `--no-build` enabled.
