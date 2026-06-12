# setup_x11_gui.py — Documentation

## Overview

`setup_x11_gui.py` automates the setup of a headless X11 graphical desktop environment on a remote Linux server (originally targeting AWS EC2). It installs the required packages, starts a virtual display, exposes it over VNC, and wraps it in a browser-accessible noVNC interface — all in a single command.

---

## Requirements

- Python 3.10+ (uses `list[...]` and `X | Y` union type hints)
- Ubuntu / Debian-based OS with `sudo` and `apt-get`
- Outbound internet access for package installation

---

## Quick Start

```bash
# Full setup with a VNC password (recommended)
python3 setup_x11_gui.py --vnc-password mysecretpass

# Skip package installation if dependencies are already installed
python3 setup_x11_gui.py --no-install --vnc-password mysecretpass

# Custom screen resolution
python3 setup_x11_gui.py --resolution 1280x720x24 --vnc-password mysecretpass
```

Once running, open the GUI in any browser:

```
http://<your-ec2-ip>:4444/vnc.html
```

---

## Command-Line Arguments

| Argument | Type | Default | Description |
|---|---|---|---|
| `--no-install` | flag | `False` | Skip apt package installation (Step 1) |
| `--resolution` | string | `1920x1080x24` | Xvfb virtual display resolution (`WxHxD`) |
| `--vnc-password` | string | `None` | Password to protect the VNC server. Strongly recommended for non-local use. |

---

## What It Does — Step by Step

### Step 1 — Install Dependencies
Runs `apt-get update` and installs the following packages:

| Package | Purpose |
|---|---|
| `xvfb` | Virtual framebuffer X server (headless display) |
| `x11vnc` | VNC server that mirrors an X display |
| `xfce4` + `xfce4-goodies` | Lightweight desktop environment |
| `dbus-x11` | D-Bus session support for the desktop |
| `xdpyinfo` | Display info utility (used for readiness polling) |
| `novnc` | Browser-based VNC client (HTML5) |
| `websockify` | WebSocket-to-TCP proxy connecting noVNC to VNC |
| `xterm` | Fallback terminal emulator |

Skippable with `--no-install`.

---

### Step 2 — Start Xvfb
Launches `Xvfb` on display `:1` with the specified resolution. Xvfb creates an in-memory X display with no physical screen. The script:
- Removes any stale X lock file at `/tmp/.X1-lock` before starting
- Polls `xdpyinfo` every 0.5 s (up to 15 s) to confirm the display is ready
- Exits with an error if the display does not become available in time

---

### Step 3 — Start XFCE4 Desktop and x11vnc
1. Launches `startxfce4` via `dbus-launch` against display `:1`, giving the desktop 3 seconds to initialise.
2. Starts `x11vnc` listening on `localhost:5900` with the following options:

| Option | Effect |
|---|---|
| `-listen localhost` | Accepts connections only from localhost (noVNC proxies) |
| `-xkb` | Enables XKB keyboard extension |
| `-forever` | Keeps running after the first client disconnects |
| `-shared` | Allows multiple simultaneous VNC clients |
| `-rfbport 5900` | Explicit VNC port |
| `-rfbauth ~/.vnc/x11vnc.passwd` | Password file (when `--vnc-password` is set) |
| `-nopw` | No password (only when no `--vnc-password` given) |

If a password is provided, it is stored with `x11vnc -storepasswd` at `~/.vnc/x11vnc.passwd`.

---

### Step 4 — Start noVNC / websockify
Launches `websockify` to proxy WebSocket connections (from the browser) to the VNC TCP socket:

```
Browser (WebSocket) → websockify :4444 → x11vnc localhost:5900
```

The noVNC HTML/JS client is served from the system web directory (typically `/usr/share/novnc`). The script falls back to several alternative paths if the default is not found.

---

## Ports

| Port | Service | Notes |
|---|---|---|
| `5900` | VNC (x11vnc) | Bound to `localhost` only — not directly exposed |
| `4444` | noVNC / websockify | Browser-accessible; open this in your firewall |

---

## Process Lifecycle

All background processes (`Xvfb`, `startxfce4`, `x11vnc`, `websockify`) are tracked in an internal `_procs` list. The main loop runs every 5 seconds and logs warnings if any child process exits unexpectedly (it does not auto-restart them).

On `SIGINT` (Ctrl+C) or `SIGTERM`, all tracked processes are terminated gracefully (`SIGTERM` → 5 s wait → `SIGKILL`).

---

## Key Functions

### `run(cmd, check, capture)`
Runs a command synchronously via `subprocess.run`. Used for one-shot commands like `apt-get` and `x11vnc -storepasswd`.

### `spawn(cmd, env)`
Starts a background process with `subprocess.Popen`, merges any additional environment variables with the current environment, and registers the process for cleanup.

### `wait_for_display(display, timeout)`
Polls `xdpyinfo` every 0.5 s until the given X display becomes available or the timeout expires. Returns `True` on success, `False` on timeout.

### `wait_for_port(port, timeout)`
Attempts a TCP connection to `127.0.0.1:<port>` every 0.5 s until it succeeds or times out. Returns `True` on success, `False` on timeout.

### `cleanup(*_)`
Iterates `_procs` in reverse order, sending `SIGTERM` to each process and waiting up to 5 seconds before escalating to `SIGKILL`.

---

## Security Notes

> **Warning:** Running `x11vnc` without a password (`-nopw`) is only safe on loopback / fully private networks. Always use `--vnc-password` in cloud environments.

- The VNC port (`5900`) is bound to `localhost` only and is not directly exposed.
- Only port `4444` (noVNC) needs to be open in the security group / firewall.
- For production use, consider placing noVNC behind an HTTPS reverse proxy (e.g., nginx with a TLS certificate).

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `Xvfb did not become ready` | Stale lock file or port conflict | Delete `/tmp/.X1-lock` manually; check for other X servers |
| `x11vnc did not start` | Port 5900 already in use | Kill existing `x11vnc` process |
| `websockify did not start` | Port 4444 already in use | Change `NOVNC_PORT` or kill the conflicting process |
| Browser shows blank page | noVNC web directory not found | Check that `novnc` package is installed and the web dir exists |
| `xdpyinfo not found` | Package not installed | Run without `--no-install`, or `apt-get install x11-utils` manually |
