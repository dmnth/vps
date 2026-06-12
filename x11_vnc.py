#!/usr/bin/env python3
"""
setup_x11_gui.py
Automates steps 1-4 of headless X11 GUI setup on AWS:
  1. Install dependencies (xvfb, x11vnc, xfce4, novnc, websockify)
  2. Start Xvfb on display :1
  3. Start x11vnc pointing at :1
  4. Start noVNC / websockify on port 4444
"""

import subprocess
import time
import sys
import os
import shutil
import signal
import logging
import argparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DISPLAY = ":1"
VNC_PORT = 5900
NOVNC_PORT = 4444
NOVNC_WEB_DIR = "/usr/share/novnc"

# Processes we start — kept so we can clean up on exit
_procs: list[subprocess.Popen] = []


def run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )


def spawn(cmd: list[str], env: dict | None = None) -> subprocess.Popen:
    """Start a background process and register it for cleanup."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    log.info("& %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, env=merged_env)
    _procs.append(proc)
    return proc


def require(binary: str) -> bool:
    return shutil.which(binary) is not None


def wait_for_display(display: str = DISPLAY, timeout: float = 15.0) -> bool:
    """Poll xdpyinfo until the display is ready or timeout expires."""
    log.info("Waiting for display %s to be ready ...", display)
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["xdpyinfo", "-display", display],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            log.info("Display %s is ready.", display)
            return True
        time.sleep(0.5)
    return False


def wait_for_port(port: int, timeout: float = 15.0) -> bool:
    """Poll until a local TCP port is listening."""
    import socket
    log.info("Waiting for localhost:%d ...", port)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                log.info("Port %d is open.", port)
                return True
        except OSError:
            time.sleep(0.5)
    return False


# ── Step 1: Install dependencies ─────────────────────────────────────────────

def step1_install(skip: bool = False) -> None:
    if skip:
        log.info("Step 1 skipped (--no-install).")
        return

    log.info("=== Step 1: Installing dependencies ===")
    packages = [
        "xvfb",
        "x11vnc",
        "xfce4",
        "xfce4-goodies",
        "dbus-x11",
        "xdpyinfo",
        "novnc",
        "websockify",
        "xterm",
    ]
    run(["sudo", "apt-get", "update", "-y"])
    run(["sudo", "apt-get", "install", "-y"] + packages)
    log.info("Step 1 complete.")


# ── Step 2: Start Xvfb ───────────────────────────────────────────────────────

def step2_xvfb(resolution: str = "1920x1080x24") -> subprocess.Popen:
    log.info("=== Step 2: Starting Xvfb on display %s ===", DISPLAY)

    if not require("Xvfb"):
        log.error("Xvfb not found. Run without --no-install or install manually.")
        sys.exit(1)

    # Kill any stale lock file left from a previous run
    lock = f"/tmp/.X{DISPLAY.lstrip(':')}-lock"
    if os.path.exists(lock):
        log.warning("Removing stale X lock file: %s", lock)
        os.remove(lock)

    proc = spawn(["Xvfb", DISPLAY, "-screen", "0", resolution])

    if not wait_for_display(DISPLAY, timeout=15):
        log.error("Xvfb did not become ready in time.")
        cleanup()
        sys.exit(1)

    log.info("Step 2 complete. Xvfb PID=%d", proc.pid)
    return proc


# ── Step 3: Start desktop + x11vnc ───────────────────────────────────────────

def step3_x11vnc(password: str | None = None) -> subprocess.Popen:
    log.info("=== Step 3: Starting XFCE4 and x11vnc ===")

    if not require("x11vnc"):
        log.error("x11vnc not found.")
        cleanup()
        sys.exit(1)

    display_env = {"DISPLAY": DISPLAY}

    # Start the desktop environment
    spawn(
        ["dbus-launch", "--exit-with-session", "startxfce4"],
        env=display_env,
    )
    # Give the DE a moment to initialise before VNC attaches
    time.sleep(3)

    # Build x11vnc command
    vnc_cmd = [
        "x11vnc",
        "-display", DISPLAY,
        "-listen", "localhost",
        "-xkb",
        "-forever",
        "-shared",
        "-rfbport", str(VNC_PORT),
    ]

    if password:
        passwd_file = os.path.expanduser("~/.vnc/x11vnc.passwd")
        os.makedirs(os.path.dirname(passwd_file), exist_ok=True)
        run(["x11vnc", "-storepasswd", password, passwd_file])
        vnc_cmd += ["-rfbauth", passwd_file]
        log.info("VNC password stored at %s", passwd_file)
    else:
        vnc_cmd.append("-nopw")
        log.warning("No VNC password set (-nopw). Suitable for local/trusted networks only.")

    proc = spawn(vnc_cmd)

    if not wait_for_port(VNC_PORT, timeout=15):
        log.error("x11vnc did not start listening on port %d in time.", VNC_PORT)
        cleanup()
        sys.exit(1)

    log.info("Step 3 complete. x11vnc PID=%d, VNC on localhost:%d", proc.pid, VNC_PORT)
    return proc


# ── Step 4: Start noVNC / websockify on port 4444 ────────────────────────────

def step4_novnc() -> subprocess.Popen:
    log.info("=== Step 4: Starting noVNC / websockify on port %d ===", NOVNC_PORT)

    if not require("websockify"):
        log.error("websockify not found.")
        cleanup()
        sys.exit(1)

    # Resolve web directory — distros vary
    web_dir = NOVNC_WEB_DIR
    if not os.path.isdir(web_dir):
        for candidate in [
            "/usr/share/novnc",
            "/usr/local/share/novnc",
            "/opt/novnc",
        ]:
            if os.path.isdir(candidate):
                web_dir = candidate
                break
        else:
            log.warning("noVNC web directory not found; browser UI may not load.")

    proc = spawn([
        "websockify",
        "--web", web_dir,
        str(NOVNC_PORT),
        f"localhost:{VNC_PORT}",
    ])

    if not wait_for_port(NOVNC_PORT, timeout=15):
        log.error("websockify did not start on port %d in time.", NOVNC_PORT)
        cleanup()
        sys.exit(1)

    log.info("Step 4 complete. noVNC PID=%d", proc.pid)
    return proc


# ── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup(*_) -> None:
    log.info("Shutting down background processes ...")
    for proc in reversed(_procs):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    log.info("All processes stopped.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Headless X11 GUI setup for AWS")
    parser.add_argument("--no-install", action="store_true", help="Skip apt package installation")
    parser.add_argument("--resolution", default="1920x1080x24", help="Xvfb screen resolution (default: 1920x1080x24)")
    parser.add_argument("--vnc-password", metavar="PASS", help="Set a VNC password (recommended)")
    args = parser.parse_args()

    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, lambda *a: (cleanup(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *a: (cleanup(), sys.exit(0)))

    step1_install(skip=args.no_install)
    step2_xvfb(resolution=args.resolution)
    step3_x11vnc(password=args.vnc_password)
    step4_novnc()

    # Derive public IP for convenience
    try:
        import urllib.request
        public_ip = urllib.request.urlopen(
            "http://169.254.169.254/latest/meta-data/public-ipv4", timeout=2
        ).read().decode()
    except Exception:
        public_ip = "<your-ec2-ip>"

    log.info("")
    log.info("=" * 55)
    log.info("  GUI ready — open in your browser:")
    log.info("  http://%s:%d/vnc.html", public_ip, NOVNC_PORT)
    log.info("=" * 55)
    log.info("")
    log.info("Press Ctrl+C to stop all services.")

    # Keep the script alive; child processes run until interrupted
    try:
        while True:
            # Restart any crashed child
            for proc in _procs:
                if proc.poll() is not None:
                    log.warning("Process PID=%d exited unexpectedly (rc=%d).", proc.pid, proc.returncode)
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
