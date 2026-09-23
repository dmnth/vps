# IB Gateway in Docker with a browser desktop

A Docker image that runs **Interactive Brokers IB Gateway** on a virtual desktop you open in your browser, and exposes the IB API on port **7496**.

- **Remote desktop:** Xvfb + XFCE + x11vnc, served by noVNC over **HTTPS** on port **4444**. No VNC client needed.
- **IB Gateway:** installed at first start from an installer (`ibgateway-*.sh`) in a **directory on the host** that is shared with the container.
- **API:** port **7496** is forwarded to the gateway, so API clients outside the container are accepted as local connections.
- **Java:** the newest Eclipse Temurin release is installed at build time.

Everything is in a single `Dockerfile`; the startup script is embedded in it.

```
 Browser ──HTTPS :4444──▶ websockify/noVNC ──▶ x11vnc (localhost:5900) ──▶ Xvfb :1 ◀── XFCE + IB Gateway
 API client ──TCP :7496──▶ socat ─────────────────────────────────────────▶ IB Gateway API (127.0.0.1:4001)
```

---

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [API access](#api-access)
- [Java](#java)
- [Updating](#updating)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)

---

## Requirements

| Requirement | Notes |
|---|---|
| Docker **23 or newer** (BuildKit) | The Dockerfile uses heredocs. The stock `docker` package on CentOS 7 (1.13) is too old; install Docker CE. |
| IB Gateway **standalone Linux installer** | `ibgateway-stable-standalone-linux-x64.sh` or `ibgateway-latest-standalone-linux-x64.sh`, downloaded from Interactive Brokers into a directory on the host. |
| x86_64 or aarch64 host | Architecture of the Java download is detected automatically. |
| Internet access during the build | Packages (AlmaLinux, EPEL, PyPI) and Java (Adoptium). |

The image is based on **AlmaLinux 9**.

---

## Quick start

### 1. Put the installer in a host directory

```bash
mkdir -p ~/ibgw/installer
mv ~/Downloads/ibgateway-stable-standalone-linux-x64.sh ~/ibgw/installer/
```

### 2. Build the image

```bash
docker build -t ibgateway-novnc .
```

### 3. Create an environment file

Keeping the password in a file keeps it out of your shell history. (It is still visible to anyone who can run `docker inspect` on the host, like any container environment variable.)

```bash
cat > ibgw.env <<'EOF'
VNC_PASSWORD=choose-a-strong-password
PUBLIC_HOST=
IB_API_PORT=4001
EOF
chmod 600 ibgw.env
```

### 4. Run the container

```bash
docker run -d --name ibgateway --init --restart unless-stopped \
  --env-file ibgw.env \
  -p 127.0.0.1:4444:4444 \
  -p 127.0.0.1:7496:7496 \
  -v ~/ibgw/installer:/installer:ro,z \
  -v ibgw-install:/opt/ibgateway \
  -v ibgw-jts:/home/ibgw/Jts \
  -v ibgw-tls:/home/ibgw/tls \
  ibgateway-novnc
```

### 5. Connect

Ports are published on `127.0.0.1` only (see [Security](#security)), so connect through an SSH tunnel from your own computer:

```bash
ssh -L 4444:localhost:4444 -L 7496:localhost:7496 user@your-server
```

- Desktop: open **https://localhost:4444/vnc.html**, accept the certificate warning (self-signed), enter `VNC_PASSWORD`.
- Log in to IB Gateway in the desktop (including 2FA).
- API: connect your client to **localhost:7496**.

Follow the startup with `docker logs -f ibgateway`. The first start takes longer because IB Gateway is installed.

### Docker Compose

```yaml
services:
  ibgateway:
    build: .
    image: ibgateway-novnc:latest
    container_name: ibgateway
    init: true
    restart: unless-stopped
    stop_grace_period: 30s
    env_file: ibgw.env
    ports:
      - "127.0.0.1:4444:4444"
      - "127.0.0.1:7496:7496"
    volumes:
      - ./installer:/installer:ro,z
      - ibgw-install:/opt/ibgateway
      - ibgw-jts:/home/ibgw/Jts
      - ibgw-tls:/home/ibgw/tls

volumes:
  ibgw-install:
  ibgw-jts:
  ibgw-tls:
```

---

## Configuration

### Environment variables (runtime)

| Variable | Default | Description |
|---|---|---|
| `VNC_PASSWORD` | *(required)* | Password for the remote desktop. The container refuses to start without it. |
| `PUBLIC_HOST` | empty | Public IP or hostname of the server. Added to the TLS certificate so it matches when you connect directly instead of through a tunnel. |
| `IB_API_PORT` | `4001` | The gateway's own API port that 7496 is forwarded to: `4001` for live, `4002` for paper trading. Must not be `7496`. |
| `RESOLUTION` | `1920x1080x24` | Size and colour depth of the virtual screen. |
| `IB_USE_SYSTEM_JAVA` | `0` | `1` runs the installer and IB Gateway on the Temurin Java installed in the image instead of the Java bundled with IB Gateway. See [Java](#java). |

### Build arguments

| Argument | Default | Description |
|---|---|---|
| `JAVA_VERSION` | `latest` | `latest` (newest Temurin release), `lts` (newest long-term-support release) or a feature number such as `25`. |
| `JAVA_IMAGE_TYPE` | `jre` | `jre` or `jdk`. |
| `UID` | `1000` | User ID of the `ibgw` user inside the container. Match your host user if you bind-mount writable directories. |

Example: `docker build --build-arg JAVA_VERSION=lts -t ibgateway-novnc .`

### Ports

| Port | Purpose |
|---|---|
| `4444` | Remote desktop: noVNC over HTTPS (plain HTTP is refused) |
| `7496` | IB API, forwarded to the gateway on `IB_API_PORT` |

VNC itself (5900) listens only inside the container and is not exposed.

### Volumes and mounts

| Container path | Type | Contents |
|---|---|---|
| `/installer` | bind mount, read-only | Host directory with `ibgateway*.sh` |
| `/opt/ibgateway` | named volume | Installed IB Gateway (installed once, reused) |
| `/home/ibgw/Jts` | named volume | IB Gateway settings: API configuration, login settings |
| `/home/ibgw/tls` | named volume | Self-signed certificate `cert.pem` and key `key.pem` |

---

## How it works

### Build time

1. Enables the CRB and EPEL repositories and installs Xvfb, `xdpyinfo`, x11vnc, the noVNC web files, `dbus-x11`, xterm, OpenSSL, socat, the X libraries and fonts IB Gateway's GUI needs, and the XFCE desktop.
2. Installs **websockify** from PyPI into a virtualenv at `/opt/websockify`. The EPEL builds are too old for current noVNC and fail with `Client must support 'binary' or 'base64' protocol`.
3. Downloads **Eclipse Temurin** from the Adoptium API to `/opt/java`, verifies its SHA-256 checksum and sets `JAVA_HOME`.
4. Creates the unprivileged user `ibgw`. All services run as this user, never as root.

### Container start (`/usr/local/bin/start.sh`)

1. **Checks settings:** `VNC_PASSWORD` must be set and `IB_API_PORT` must not be 7496.
2. **Installs IB Gateway** from the newest `ibgateway*.sh` in `/installer`, unattended, into `/opt/ibgateway`. The installer's SHA-256 is stored; the install is skipped on later starts unless the installer file changes. With no installer mounted, an existing installation is used.
3. **Starts Xvfb** on display `:1` (`-nolisten tcp`), after removing stale lock files, and waits until the display responds.
4. **Starts XFCE** and **x11vnc** (localhost only, password from `VNC_PASSWORD`) and waits for port 5900.
5. **Starts noVNC** on 4444 with `--ssl-only`. A self-signed certificate is generated on the first start (RSA 2048, 365 days, key mode `600`) and reused until it is within a day of expiring.
6. **Forwards 7496** to `127.0.0.1:IB_API_PORT` with socat.
7. **Starts IB Gateway** on the desktop and checks every 10 seconds whether it is still running; if it has exited, it is started again. An instance started by the gateway's own daily auto-restart is detected, so no second copy is launched.
8. **Watches the services.** If Xvfb, the desktop, x11vnc, noVNC or the API forwarder exits, the container stops so the restart policy can start it cleanly. On `docker stop` all services are shut down.

---

## API access

IB Gateway accepts API connections from localhost only by default. Connections arriving through Docker's port mapping come from the Docker network, not localhost, and would be rejected. The container therefore forwards port 7496 internally with socat, so the gateway sees every API client as `127.0.0.1`.

In IB Gateway's settings (**Configure → Settings → API → Settings**):

- Leave **Socket port** at the default: **4001** (live) or **4002** (paper). For paper trading set `IB_API_PORT=4002`.
- Keep **Allow connections from localhost only** enabled.
- **Read-Only API** is enabled by default; disable it only if your client needs to place orders.

These settings are stored in the `/home/ibgw/Jts` volume and survive restarts.

---

## Java

The image installs the newest Eclipse Temurin release at build time. Adoptium sometimes lists a new version before its binaries are published; in that case the build falls back to the next newest version that has a build and logs which one it used.

The IB Gateway **standalone** installer ships with its own Java, and IB Gateway uses that bundled Java by default. Interactive Brokers supports specific Java versions only, so running the gateway on the newest release may not work. To try it anyway, set `IB_USE_SYSTEM_JAVA=1`.

Check the installed version with:

```bash
docker exec ibgateway java -version
```

---

## Updating

| What | How |
|---|---|
| IB Gateway | Put the new installer in the host directory (remove or keep the old one; the newest file is used) and `docker restart ibgateway`. It is reinstalled because the file changed. |
| Java, packages, websockify | Rebuild without cache, otherwise Docker reuses the old layers: `docker build --pull --no-cache -t ibgateway-novnc .`, then recreate the container. |
| TLS certificate | Regenerated automatically before it expires. After changing `PUBLIC_HOST`, remove the old one: `docker volume rm ibgw-tls` (with the container removed) or delete `cert.pem`/`key.pem` in it. |

---

## Security

Whoever reaches these ports gets either a desktop logged in to your brokerage account or its API. Treat both as sensitive.

1. **Publish on `127.0.0.1` and use an SSH tunnel** (as in the quick start). Docker publishes ports by writing its own firewall rules, which **bypass firewalld and ufw**: `-p 7496:7496` without an address is reachable from the internet even if your host firewall blocks it.
2. **The IB API has no authentication.** Anyone who can connect to 7496 can use the logged-in session. Never expose it publicly; keep **Read-Only API** on unless you need trading.
3. **Use a strong `VNC_PASSWORD`.** VNC authentication uses only the first 8 characters and is weak on its own; the tunnel is the real protection. Pass it with `--env-file`, not `-e` on the command line.
4. **TLS** is always on for the desktop. The certificate is self-signed; if you expose 4444 directly, set `PUBLIC_HOST` so the certificate matches the address.
5. Services run as the unprivileged `ibgw` user; VNC is only reachable inside the container; Xvfb opens no TCP port.

Do not commit secrets or installers. Suggested `.gitignore`:

```gitignore
*.env
installer/
*.pem
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Build fails at `RUN <<'EOF'` or `COPY <<'EOF'` | Docker too old / BuildKit off | Use Docker 23+, or `DOCKER_BUILDKIT=1` on 18.09+ |
| `No matching Temurin build found` | Adoptium unreachable or requested version unavailable | Check network; use `--build-arg JAVA_VERSION=lts` |
| `VNC_PASSWORD is not set` | Missing environment variable | Add it to the env file and pass `--env-file` |
| `No IB Gateway installed and no ibgateway*.sh found` | Installer directory not mounted or empty | Mount it: `-v /host/dir:/installer:ro,z`; file name must start with `ibgateway` and end with `.sh` |
| `Permission denied` reading `/installer` on CentOS/RHEL/Fedora | SELinux label | Add `,z` to the mount options |
| `IB Gateway installer failed` | Wrong or corrupt installer | Download the Linux **standalone** installer again |
| Page won't load | Using `http://`, or no tunnel / port not published | Use `https://localhost:4444/vnc.html` through the SSH tunnel |
| Browser certificate warning | Self-signed certificate | Expected; accept it, or set `PUBLIC_HOST` for direct access |
| API client: connection refused or closed | Gateway not logged in, or API port mismatch | Log in via the desktop; make sure the gateway's socket port equals `IB_API_PORT` (4001 live / 4002 paper) |
| API client connects to paper instead of live (or vice versa) | `IB_API_PORT` doesn't match the trading mode | 4001 live, 4002 paper |
| Container keeps restarting | A core service exits | `docker logs ibgateway` shows which service stopped; logging out of XFCE also stops the container |
| Permission errors on volumes | Volume created with another UID | Rebuild with `--build-arg UID=<id>` or recreate the volumes |

---

## Known limitations

- **IB Gateway login is manual.** You log in (and confirm 2FA) through the desktop after each gateway restart. Automated login tools such as IBC are not included.
- The unattended installer options (`-q -overwrite -dir`) and the Java override used by `IB_USE_SYSTEM_JAVA` come from the install4j installer framework IB Gateway uses; verify them against the installer version you download.
- Only one gateway (one trading mode) per container. Run a second container for live and paper at the same time, with a different host port mapped to 7496.
- The certificate is not reissued automatically when `PUBLIC_HOST` changes; remove it as described in [Updating](#updating).
- The API port inside the container is fixed at 7496; map it to another host port with `-p 127.0.0.1:<host-port>:7496` if needed.
