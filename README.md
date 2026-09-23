# setup_x11_gui.py

Run a full graphical desktop on a headless Linux server and use it from your browser.

The script installs and starts a virtual X display (**Xvfb**), the **XFCE** desktop, a VNC server (**x11vnc**), and **noVNC** (served by **websockify**), so you can open the desktop at `https://<server>:4444/vnc.html` with no VNC client installed. HTTPS is on by default using a self-signed certificate that is generated during installation.

It was written for AWS EC2 instances but works on any server running a supported OS.

```
Browser ──HTTPS/WSS :4444──▶ websockify + noVNC ──▶ x11vnc (localhost:5900) ──▶ Xvfb :1 ◀── XFCE
```

---

## Contents

- [Supported platforms](#supported-platforms)
- [Quick start](#quick-start)
- [Command-line options](#command-line-options)
- [What the script does](#what-the-script-does)
- [HTTPS / TLS](#https--tls)
- [Why websockify runs from a virtualenv on CentOS/RHEL](#why-websockify-runs-from-a-virtualenv-on-centosrhel)
- [Security](#security)
- [Files and paths created](#files-and-paths-created)
- [Stopping](#stopping)
- [Exit codes](#exit-codes)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)

---

## Supported platforms

### Operating systems

| Family | Versions | Package manager | Extra repositories enabled |
|---|---|---|---|
| CentOS Linux | 7 | `yum` | EPEL |
| CentOS Stream | 8, 9 | `dnf` | EPEL + PowerTools (8) / CRB (9) |
| RHEL | 7, 8, 9 | `yum` / `dnf` | EPEL + optional (7) / CodeReady Builder (8, 9) |
| Rocky Linux, AlmaLinux | 8, 9 | `dnf` | EPEL + PowerTools (8) / CRB (9) |
| Debian, Ubuntu | any with the packages below | `apt-get` | none |

The OS is detected from `/etc/os-release`. Anything else (for example Amazon Linux, Fedora or Oracle Linux) is rejected with a list of supported systems.

> CentOS Linux 7 and 8 and CentOS Stream 8 are end-of-life. Their default mirrors are offline, so package installs fail until the repositories point at `vault.centos.org` (and EPEL at `archives.fedoraproject.org`). The script detects this and tells you before trying to install anything.

### Python

Only **Python 3.6** and **Python 3.9** are supported. Any other version stops the script with a message listing the supported versions, any `python3.6` / `python3.9` found on the system, and how to install one for your OS:

| OS | Default `python3` |
|---|---|
| CentOS / RHEL 7 | 3.6 (`yum install -y python3`) |
| EL8 | 3.6 (3.9 available as `dnf install -y python39`) |
| EL9 | 3.9 |
| Debian 11 | 3.9 |
| Ubuntu 18.04 | 3.6 |

The script is written without f-strings or type annotations, so even running it with Python 2.7 (the plain `python` command on CentOS 7) produces the "unsupported Python" message rather than a syntax error.

Only the standard library is used; nothing needs to be installed with pip to run the script itself.

---

## Quick start

Run the script as a **normal user with sudo rights**, not as root (see [Security](#security)). It calls `sudo` itself for the steps that need it.

```bash
# 1. See what would be installed on this machine, without changing anything
python3 setup_x11_gui.py --dry-run

# 2. Recommended: listen on localhost only and connect through an SSH tunnel
python3 setup_x11_gui.py --bind 127.0.0.1 --vnc-password 'choose-a-password'
#    on your own computer:
ssh -L 4444:localhost:4444 user@your-server
#    then open https://localhost:4444/vnc.html

# 3. Or expose it directly (restrict the port to your IP in the firewall/security group)
python3 setup_x11_gui.py --vnc-password 'choose-a-password' --open-firewall
#    then open https://<server-public-ip>:4444/vnc.html
```

The browser shows a certificate warning the first time because the certificate is self-signed; accept it to continue. The script stays in the foreground; press **Ctrl+C** to stop everything.

After the first run, add `--no-install` to skip package installation and start faster.

---

## Command-line options

| Option | Default | Description |
|---|---|---|
| `--dry-run` | off | Detect the OS and Python version, print the install plan with the exact commands, and exit. Changes nothing. |
| `--no-install` | off | Skip step 1 (packages, websockify venv). Use after the first successful run. |
| `--resolution WxHxD` | `1920x1080x24` | Screen size and colour depth of the virtual display. |
| `--vnc-password PASS` | none | Password for the VNC session. Strongly recommended. Without it x11vnc runs with `-nopw`. |
| `--bind ADDR` | `0.0.0.0` | Address noVNC listens on. Use `127.0.0.1` together with an SSH tunnel. |
| `--open-firewall` | off | CentOS/RHEL only: if firewalld is running, open port 4444/tcp (runtime rule only, removed on reload or reboot). |
| `--cert PATH` | none | Use your own TLS certificate (PEM) instead of the generated self-signed one. |
| `--key PATH` | none | Private key for `--cert`. Can be omitted if the certificate file also contains the key. Requires `--cert`. |
| `--no-tls` | off | Serve plain HTTP instead of HTTPS. Not recommended unless you use an SSH tunnel. Cannot be combined with `--cert`/`--key`. |

Fixed settings (edit the constants at the top of the script to change them):

| Setting | Value |
|---|---|
| X display | `:1` |
| VNC port (localhost only) | `5900` |
| noVNC / websockify port | `4444` |
| websockify virtualenv (CentOS/RHEL) | `/opt/websockify` |

---

## What the script does

### Step 0a: Detect the operating system

Reads `/etc/os-release` (`ID`, `ID_LIKE`, `VERSION_ID`) and picks the matching install plan. Unsupported systems exit with code 3. End-of-life CentOS releases get a warning.

### Step 0b: Check the Python version

Accepts only 3.6 and 3.9 (see [Python](#python)); otherwise exits with code 2. The two versions use slightly different code paths:

| | Python 3.6 | Python 3.9 |
|---|---|---|
| Text mode for `subprocess.run` | `universal_newlines=True` (`text=` is 3.7+) | `text=True` |
| Logging commands | `shlex.quote` + join | `shlex.join` (3.8+) |

Before doing any work, the TLS options are also validated: conflicting flags are rejected and `--cert` / `--key` files must exist and be readable, so mistakes are caught before a long install.

### Step 1: Install dependencies and generate TLS keys

Skipped with `--no-install`.

On CentOS/RHEL the script first runs `yum/dnf makecache` to confirm the repositories are reachable. Every command it runs is printed to the log.

**CentOS / RHEL family**

1. Enable EPEL and the repository EPEL depends on:
   - EL7: `yum install -y epel-release` (RHEL 7 also enables `rhel-7-server-optional-rpms`)
   - EL8/9 (CentOS Stream, Rocky, Alma): install `dnf-plugins-core`, enable `powertools` (8) or `crb` (9), install `epel-release`
   - RHEL 8/9: enable `codeready-builder-for-rhel-<ver>-<arch>-rpms` with `subscription-manager`, install EPEL from `dl.fedoraproject.org`
2. Install packages:

   | Purpose | EL7 | EL8 | EL9 |
   |---|---|---|---|
   | Virtual display | `xorg-x11-server-Xvfb` | same | same |
   | `xdpyinfo` (readiness check) | `xorg-x11-utils` | `xorg-x11-utils` | `xdpyinfo` |
   | VNC server | `x11vnc` | same | same |
   | noVNC web files | `novnc` | same | same |
   | D-Bus session | `dbus-x11` | same | same |
   | Terminal | `xterm` | same | same |
   | Certificate generation | `openssl` | same | same |
   | Desktop | group `Xfce` | same | same |

3. Create a virtualenv at `/opt/websockify` with the Python running the script, upgrade pip inside it, and install the current websockify release that supports that Python version ([why](#why-websockify-runs-from-a-virtualenv-on-centosrhel)).

**Debian / Ubuntu**

`apt-get update`, then install `xvfb x11vnc xfce4 xfce4-goodies dbus-x11 x11-utils novnc websockify xterm openssl`. The distribution's websockify is used.

**After installing (all systems)**

- Checks that `Xvfb`, `xdpyinfo`, `x11vnc`, `startxfce4`, `dbus-launch` and websockify all exist, and runs `websockify --help` to confirm it actually starts.
- Generates the self-signed TLS certificate and key next to the script (unless `--no-tls` or `--cert` is used). See [HTTPS / TLS](#https--tls).

### Step 2: Start the virtual display

- Removes a stale `/tmp/.X1-lock` left from an earlier run.
- Starts `Xvfb :1 -screen 0 <resolution> -nolisten tcp` (no X11 TCP port is opened).
- Polls `xdpyinfo` for up to 15 seconds until the display responds.

### Step 3: Start the desktop and VNC server

- Starts XFCE with `dbus-launch --exit-with-session startxfce4` on display `:1` and waits 3 seconds for it to initialise.
- If `--vnc-password` is given, stores it with `x11vnc -storepasswd` in `~/.vnc/x11vnc.passwd`; otherwise uses `-nopw` and logs a warning.
- Starts `x11vnc -display :1 -listen localhost -rfbport 5900 -xkb -forever -shared`. VNC is only reachable from the server itself.
- Waits up to 15 seconds for port 5900 to open.

### Step 4: Start noVNC

- Resolves the websockify binary: `/opt/websockify/bin/websockify` on CentOS/RHEL (never the system RPM), the system `websockify` on Debian/Ubuntu.
- Finds the noVNC web files in `/usr/share/novnc`, `/usr/local/share/novnc` or `/opt/novnc`.
- With TLS (default): loads the certificate and key once to check they match, then starts websockify with `--cert`, `--key` and `--ssl-only`, so plain HTTP connections are refused.
- Starts `websockify --web <novnc dir> <bind>:4444 localhost:5900` and waits up to 15 seconds for the port.
- CentOS/RHEL with firewalld running: opens 4444/tcp with `--open-firewall`, otherwise warns that the port may be blocked. Skipped when bound to localhost.

### Running

- Prints the URL to open. The public IP is read from the EC2 metadata service (IMDSv2 first, then IMDSv1); off EC2 a placeholder is shown.
- Checks every 5 seconds whether a child process has exited and logs it once.
- On Ctrl+C or SIGTERM, stops all started processes in reverse order (terminate, then kill after 5 seconds).

---

## HTTPS / TLS

HTTPS is **on by default**. Three modes:

| Mode | How | Result |
|---|---|---|
| Self-signed (default) | no TLS options | Certificate and key generated during step 1 next to the script |
| Your own certificate | `--cert fullchain.pem --key privkey.pem` | Uses your files (for example Let's Encrypt) |
| Off | `--no-tls` | Plain HTTP; screen contents and keystrokes are unencrypted |

In both TLS modes websockify runs with `--ssl-only`: the page is served over `https://` and the VNC stream over `wss://`, and plain `http://` requests are refused.

### The self-signed certificate

- Files: `novnc-cert.pem` and `novnc-key.pem` in the **same directory as the script**, both mode `0600`.
- RSA 2048, SHA-256, valid 365 days.
- Subject Alternative Names: `localhost`, `127.0.0.1`, and the server's public IP (or hostname) when bound to a public address.
- Generated with an OpenSSL config file rather than `-addext`, so it also works with OpenSSL 1.0.2 on CentOS 7.
- **Reused** on later runs. It is regenerated automatically when:
  - it expires within the next 24 hours, or
  - the server's public IP is no longer in the certificate (for example an EC2 instance without an Elastic IP after a stop/start).
- With `--no-install` on a fresh machine, the keys are generated when noVNC starts instead.
- To force a new certificate, delete both files and run the script again.

Browsers warn about self-signed certificates. Accept the warning once, or use a real certificate with `--cert`.

### Using Let's Encrypt

Keys under `/etc/letsencrypt/live/` are readable only by root. Since the script should run as a normal user, copy them first:

```bash
sudo install -m 600 -o "$USER" /etc/letsencrypt/live/example.com/fullchain.pem ./
sudo install -m 600 -o "$USER" /etc/letsencrypt/live/example.com/privkey.pem ./
python3 setup_x11_gui.py --cert fullchain.pem --key privkey.pem
```

Repeat the copy after each renewal.

---

## Why websockify runs from a virtualenv on CentOS/RHEL

The websockify packages in EPEL cause two problems:

1. **Too old for current noVNC.** websockify releases before 0.9 require the browser to request the `binary` or `base64` WebSocket subprotocol. noVNC 1.x does not, so the connection fails with:
   ```
   code 400, message Client must support 'binary' or 'base64' protocol
   code 404, message File not found
   ```
2. **Broken under Python 3.** Some builds fail at import time:
   ```
   ModuleNotFoundError: No module named 'websocket'
   ```

`pip install websockify` does not help while the RPM is installed, because pip sees the package as "already satisfied", and upgrading over it would overwrite files owned by the RPM. The script therefore installs websockify into its own virtualenv at `/opt/websockify` and always starts it by full path. This also avoids `sudo` hiding `/usr/local/bin` from `PATH` on CentOS.

The `novnc` RPM is still installed, but only for its web files. The websockify RPM it pulls in as a dependency is never used.

---

## Security

Anyone who can reach port 4444 can reach the desktop, which runs with the permissions of the user who started the script and includes a terminal. Recommendations, most important first:

1. **Don't expose port 4444 publicly.** Use `--bind 127.0.0.1` and an SSH tunnel (see [Quick start](#quick-start)). Access then requires your SSH key. If you do expose it, restrict the AWS security group and firewall to your own IP.
2. **Don't run the script as root.** The desktop would run as root. Run it as a normal user; it uses `sudo` only for installation.
3. **Always set `--vnc-password`, but don't rely on it alone.** VNC authentication uses only the first 8 characters and weak DES encryption. Note also that a password passed on the command line appears in your shell history and, briefly, in `ps` output.
4. **Keep TLS on** (the default) whenever the port is reachable from outside the server.
5. Keep SELinux enforcing, stop the desktop when you're not using it, and consider pinning the websockify version.

Already built in:

- x11vnc listens on `localhost` only; Xvfb opens no TCP port (`-nolisten tcp`).
- The firewalld rule from `--open-firewall` is runtime-only.
- TLS keys are created with mode `0600`.
- The script warns when noVNC is exposed without a password or without TLS.

---

## Files and paths created

| Path | Created by | Contents |
|---|---|---|
| `<script dir>/novnc-cert.pem` | step 1 | Self-signed TLS certificate |
| `<script dir>/novnc-key.pem` | step 1 | TLS private key (`0600`) |
| `/opt/websockify/` | step 1, CentOS/RHEL | Python virtualenv with websockify |
| `~/.vnc/x11vnc.passwd` | step 3, with `--vnc-password` | Obfuscated VNC password |
| `/tmp/.X1-lock` | Xvfb | X display lock (removed on the next start if stale) |

**Never commit the private key.** Add this to `.gitignore`:

```gitignore
novnc-*.pem
fullchain.pem
privkey.pem
```

---

## Stopping

Press **Ctrl+C** in the terminal running the script, or send it `SIGTERM`. All processes it started (Xvfb, XFCE, x11vnc, websockify) are stopped.

To run it in the background, use `tmux`/`screen`, or a systemd service that runs `python3 setup_x11_gui.py --no-install ...`.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Stopped normally (Ctrl+C / SIGTERM) or `--dry-run` finished |
| 1 | An error: failed install, missing binary, invalid TLS options or files, a service not starting in time |
| 2 | Unsupported Python version |
| 3 | Unsupported or undetected operating system |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `TypeError: 'type' object is not subscriptable` | Running an older version of this script (with `list[...]` annotations) on Python 3.6 | Use the current script |
| `Unsupported Python version` | Not 3.6 or 3.9 | Run with the interpreter the message suggests, e.g. `python3.9 setup_x11_gui.py` |
| `makecache failed` on CentOS 7/8 | End-of-life mirrors are offline | Point the repos at `vault.centos.org`, EPEL at `archives.fedoraproject.org` |
| `Client must support 'binary' or 'base64' protocol` | Old websockify from EPEL | Handled by the virtualenv; make sure step 1 ran (not only `--no-install`) |
| `No module named 'websocket'` | Broken websockify RPM | Same as above |
| Page does not load from outside | Security group, firewalld, or `--bind 127.0.0.1` | Open 4444/tcp for your IP, use `--open-firewall`, or use an SSH tunnel |
| `non-SSL connection received but disallowed` in the log | Browser opened `http://` while TLS is on | Use `https://` |
| Browser certificate warning | Self-signed certificate | Accept once, or use `--cert`/`--key` |
| `Certificate/key could not be loaded` | Certificate and key do not match, or unreadable | Check the pair; for Let's Encrypt see [above](#using-lets-encrypt) |
| `Cannot write TLS keys to ...` | Script directory is not writable by your user | Move the script to a directory you own |
| Grey or black screen in noVNC | XFCE did not start | Check the log for `startxfce4` errors; confirm the `Xfce` group installed |

Use `--dry-run` first on a new machine to see exactly which commands will run.

---

## Known limitations

- Package names come from the distributions' repositories and may change between point releases; `--dry-run` shows them before anything is installed.
- Tested logic under Python 3.9; the Python 3.6 path uses only features available in 3.6 but should be verified on your host.
- The process monitor only reports exited processes; it does not restart them.
- One desktop on display `:1` per machine; ports and display are fixed constants.
