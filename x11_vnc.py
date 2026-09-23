#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_x11_gui.py
Headless X11 GUI setup (Xvfb + XFCE + x11vnc + noVNC).

Order of operations:
  0a. Detect OS from /etc/os-release  -> choose the package/repo plan
  0b. Detect Python version           -> only 3.6 and 3.9 are supported
  1.  Install dependencies (OS-specific commands) + generate TLS keys
      (novnc-cert.pem / novnc-key.pem next to this script)
  2.  Start Xvfb on display :1
  3.  Start XFCE + x11vnc on localhost:5900
  4.  Start noVNC / websockify on port 4444

Supported OS:
  CentOS 7, CentOS Stream 8/9, RHEL 7/8/9, Rocky 8/9, AlmaLinux 8/9,
  Debian / Ubuntu (apt path kept from the original script)

NOTE: this file intentionally avoids f-strings and type annotations so it
still *parses* on old interpreters (e.g. `python` = 2.7 on CentOS 7) and can
print a clear "unsupported Python" message instead of a SyntaxError.
"""

import argparse
import logging
import os
import platform
import shlex
import signal
import socket
import subprocess
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("setup_x11_gui")

DISPLAY = ":1"
VNC_PORT = 5900
NOVNC_PORT = 4444
NOVNC_WEB_DIRS = ["/usr/share/novnc", "/usr/local/share/novnc", "/opt/novnc"]

# On EL the EPEL websockify RPMs are too old (reject modern noVNC clients) or
# broken under python3, so websockify is installed into its own virtualenv.
WEBSOCKIFY_VENV = "/opt/websockify"
WEBSOCKIFY_VENV_BIN = os.path.join(WEBSOCKIFY_VENV, "bin", "websockify")

SUPPORTED_PY = ((3, 6), (3, 9))
EL_DISTROS = ("centos", "rhel", "rocky", "almalinux")

_procs = []      # background processes we started, for cleanup
PY = None        # active Python profile, set after the version check


# =============================================================================
# Step 0a: OS detection
# =============================================================================

class OSInfo(object):
    def __init__(self, family, distro, major, pretty, eol=False):
        self.family = family    # "el" or "debian"
        self.distro = distro    # centos / rhel / rocky / almalinux / ubuntu / debian
        self.major = major      # int for EL, string for debian family
        self.pretty = pretty
        self.eol = eol

    def __repr__(self):
        return "OSInfo(family={0}, distro={1}, major={2})".format(
            self.family, self.distro, self.major)


def read_os_release(path):
    data = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                data[key] = val.strip().strip('"').strip("'")
    except (IOError, OSError):
        pass
    return data


def detect_os(os_release_path="/etc/os-release"):
    rel = read_os_release(os_release_path)
    if not rel:
        return None

    os_id = rel.get("ID", "").lower()
    like = rel.get("ID_LIKE", "").lower().split()
    version = rel.get("VERSION_ID", "")
    major = version.split(".")[0] if version else ""
    pretty = rel.get("PRETTY_NAME", rel.get("NAME", os_id))

    if os_id in EL_DISTROS:
        if major not in ("7", "8", "9"):
            return None
        if os_id == "rocky" or os_id == "almalinux":
            if major == "7":
                return None
        # CentOS Linux 7, CentOS Linux 8 and CentOS Stream 8 are all end-of-life
        eol = os_id == "centos" and major in ("7", "8")
        return OSInfo("el", os_id, int(major), pretty, eol)

    if os_id in ("ubuntu", "debian") or "debian" in like:
        return OSInfo("debian", os_id, major, pretty)

    return None


# =============================================================================
# Step 0b: Python version check
# =============================================================================

def _which(name):
    # shutil.which does not exist on python2, and this runs before the check
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def python_hint(osi):
    if osi.family == "el":
        if osi.major == 7:
            return "CentOS/RHEL 7: 'yum install -y python3' provides Python 3.6."
        if osi.major == 8:
            return ("EL8: python3 (3.6) is the default; for 3.9 run "
                    "'dnf install -y python39' and use python3.9.")
        if osi.major == 9:
            return "EL9: the default python3 is 3.9."
    return ("Debian/Ubuntu: Debian 11 ships 3.9, Ubuntu 18.04 ships 3.6; "
            "Ubuntu 20.04 has a 'python3.9' package in universe.")


def check_python(osi):
    current = tuple(sys.version_info[:2])
    if current in SUPPORTED_PY:
        return current

    supported = ", ".join("{0}.{1}".format(*v) for v in SUPPORTED_PY)
    lines = [
        "",
        "Unsupported Python version: {0}.{1}.{2} ({3})".format(
            sys.version_info[0], sys.version_info[1], sys.version_info[2],
            sys.executable),
        "Supported Python versions: {0}".format(supported),
        "",
    ]
    found = [p for p in (_which("python3.6"), _which("python3.9")) if p]
    if found:
        lines.append("Supported interpreters found on this host:")
        for p in found:
            lines.append("  {0} {1}".format(p, os.path.abspath(sys.argv[0])))
    else:
        lines.append("No supported interpreter found on PATH.")
        lines.append(python_hint(osi))
    lines.append("")
    sys.stderr.write("\n".join(lines) + "\n")
    sys.exit(2)


def _fmt_cmd_36(cmd):
    return " ".join(shlex.quote(c) for c in cmd)


def _fmt_cmd_39(cmd):
    return shlex.join(cmd)          # added in 3.8


# Per-Python-version behaviour
PY_PROFILES = {
    (3, 6): {
        "text_kwargs": {"universal_newlines": True},   # text= is 3.7+
        "fmt_cmd": _fmt_cmd_36,
    },
    (3, 9): {
        "text_kwargs": {"text": True},
        "fmt_cmd": _fmt_cmd_39,
    },
}


# =============================================================================
# Helpers
# =============================================================================

def sudo():
    return [] if os.geteuid() == 0 else ["sudo"]


def run(cmd, check=True, capture=False):
    log.info("$ %s", PY["fmt_cmd"](cmd))
    kwargs = dict(PY["text_kwargs"])
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    return subprocess.run(cmd, check=check, **kwargs)


def spawn(cmd, env=None):
    """Start a background process and register it for cleanup."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    log.info("& %s", PY["fmt_cmd"](cmd))
    proc = subprocess.Popen(cmd, env=merged_env)
    _procs.append(proc)
    return proc


def require(binary):
    return _which(binary) is not None


def wait_for_display(display=DISPLAY, timeout=15.0):
    log.info("Waiting for display %s to be ready ...", display)
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc = subprocess.call(["xdpyinfo", "-display", display],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if rc == 0:
            log.info("Display %s is ready.", display)
            return True
        time.sleep(0.5)
    return False


def wait_for_port(port, timeout=15.0):
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


def fail(msg):
    log.error(msg)
    cleanup()
    sys.exit(1)


# =============================================================================
# Step 1: Install dependencies (OS-specific plan)
# =============================================================================
# A plan is a list of (description, cmd, must_succeed).

def _plan_el(osi):
    s = sudo()
    arch = platform.machine()
    pm = "yum" if osi.major == 7 else "dnf"
    plan = []

    # --- EPEL + the extra repo EPEL depends on ---
    if osi.major == 7:
        plan.append(("Enable EPEL", s + ["yum", "install", "-y", "epel-release"], True))
        if osi.distro == "rhel":
            plan.append(("Enable optional repo",
                         s + ["subscription-manager", "repos", "--enable",
                              "rhel-7-server-optional-rpms"], False))
    else:
        plan.append(("Install dnf-plugins-core",
                     s + ["dnf", "install", "-y", "dnf-plugins-core"], True))
        if osi.distro == "rhel":
            plan.append(("Enable CodeReady Builder",
                         s + ["subscription-manager", "repos", "--enable",
                              "codeready-builder-for-rhel-{0}-{1}-rpms".format(osi.major, arch)],
                         True))
            plan.append(("Enable EPEL",
                         s + ["dnf", "install", "-y",
                              "https://dl.fedoraproject.org/pub/epel/"
                              "epel-release-latest-{0}.noarch.rpm".format(osi.major)],
                         True))
        else:
            extra_repo = "powertools" if osi.major == 8 else "crb"
            plan.append(("Enable " + extra_repo,
                         s + ["dnf", "config-manager", "--set-enabled", extra_repo], True))
            plan.append(("Enable EPEL", s + [pm, "install", "-y", "epel-release"], True))

    # --- Packages ---
    xdpyinfo_pkg = "xdpyinfo" if osi.major >= 9 else "xorg-x11-utils"
    # novnc is installed only for its web files in /usr/share/novnc; the RPM
    # websockify it drags in is never used.
    packages = ["xorg-x11-server-Xvfb", xdpyinfo_pkg, "x11vnc", "novnc",
                "dbus-x11", "xterm", "openssl"]

    plan.append(("Install X11 / VNC packages", s + [pm, "install", "-y"] + packages, True))
    plan.append(("Install Xfce group", s + [pm, "groupinstall", "-y", "Xfce"], True))
    plan.extend(_plan_websockify_venv())
    return plan


def _plan_websockify_venv():
    """Current websockify in an isolated venv, built with the running Python."""
    s = sudo()
    pip = os.path.join(WEBSOCKIFY_VENV, "bin", "pip")
    return [
        ("Create websockify venv",
         s + [sys.executable, "-m", "venv", WEBSOCKIFY_VENV], True),
        # the pip bundled with 3.6 is old; upgrading lets it pick the newest
        # releases (pip itself, websockify, deps) that still support 3.6
        ("Upgrade pip in venv",
         s + [pip, "install", "--no-cache-dir", "--upgrade", "pip"], True),
        ("Install websockify in venv",
         s + [pip, "install", "--no-cache-dir", "--upgrade", "websockify"], True),
    ]


def _plan_debian(osi):
    s = sudo()
    packages = ["xvfb", "x11vnc", "xfce4", "xfce4-goodies", "dbus-x11",
                "x11-utils", "novnc", "websockify", "xterm", "openssl"]
    return [
        ("apt update", s + ["apt-get", "update", "-y"], True),
        ("Install packages", s + ["apt-get", "install", "-y"] + packages, True),
    ]


def build_install_plan(osi):
    if osi.family == "el":
        return _plan_el(osi)
    return _plan_debian(osi)


def check_repos_reachable(osi):
    """EOL CentOS mirrors are gone; catch that before a confusing failure."""
    if osi.family != "el":
        return
    pm = "yum" if osi.major == 7 else "dnf"
    res = run(sudo() + [pm, "-q", "makecache"], check=False, capture=True)
    if res.returncode != 0:
        log.error("'%s makecache' failed:\n%s", pm, (res.stderr or "").strip())
        if osi.eol:
            log.error("%s is end-of-life and its default mirrors are offline. "
                      "Point the repos at vault.centos.org (and EPEL at "
                      "archives.fedoraproject.org), then rerun.", osi.pretty)
        sys.exit(1)


def websockify_cmd(osi):
    """Return the websockify executable to use, or None."""
    if os.access(WEBSOCKIFY_VENV_BIN, os.X_OK):
        return WEBSOCKIFY_VENV_BIN
    if osi.family == "el":
        return None          # never fall back to the broken/old RPM on EL
    return _which("websockify")


def websockify_works(binary):
    """Importing the module is what failed with the RPM build, so try --help."""
    res = run([binary, "--help"], check=False, capture=True)
    if res.returncode != 0:
        log.error("%s --help failed:\n%s", binary, (res.stderr or "").strip())
        return False
    return True


def step1_install(osi, plan, skip=False, tls_host=None):
    if skip:
        log.info("Step 1 skipped (--no-install).")
        return
    log.info("=== Step 1: Installing dependencies for %s ===", osi.pretty)
    check_repos_reachable(osi)
    for desc, cmd, must in plan:
        log.info("--- %s", desc)
        res = run(cmd, check=False)
        if res.returncode != 0:
            if must:
                log.error("'%s' failed (rc=%d).", desc, res.returncode)
                sys.exit(1)
            log.warning("'%s' failed (rc=%d), continuing.", desc, res.returncode)

    missing = [b for b in ("Xvfb", "xdpyinfo", "x11vnc",
                           "startxfce4", "dbus-launch") if not require(b)]
    ws = websockify_cmd(osi)
    if ws is None:
        missing.append("websockify ({0})".format(
            WEBSOCKIFY_VENV_BIN if osi.family == "el" else "on PATH"))
    if missing:
        log.error("Still missing after install: %s", ", ".join(missing))
        sys.exit(1)
    if not websockify_works(ws):
        sys.exit(1)
    if tls_host is not None:
        log.info("--- Generate TLS keys in %s", SCRIPT_DIR)
        generate_self_signed(tls_host)
    log.info("Step 1 complete. websockify: %s", ws)


# =============================================================================
# Step 2: Xvfb
# =============================================================================

def step2_xvfb(resolution):
    log.info("=== Step 2: Starting Xvfb on display %s ===", DISPLAY)
    if not require("Xvfb"):
        fail("Xvfb not found. Run without --no-install or install it manually.")

    lock = "/tmp/.X{0}-lock".format(DISPLAY.lstrip(":"))
    if os.path.exists(lock):
        log.warning("Removing stale X lock file: %s", lock)
        os.remove(lock)

    proc = spawn(["Xvfb", DISPLAY, "-screen", "0", resolution, "-nolisten", "tcp"])
    if not wait_for_display(DISPLAY, timeout=15):
        fail("Xvfb did not become ready in time.")
    log.info("Step 2 complete. Xvfb PID=%d", proc.pid)
    return proc


# =============================================================================
# Step 3: XFCE + x11vnc
# =============================================================================

def step3_x11vnc(password=None):
    log.info("=== Step 3: Starting XFCE4 and x11vnc ===")
    if not require("x11vnc"):
        fail("x11vnc not found.")

    spawn(["dbus-launch", "--exit-with-session", "startxfce4"],
          env={"DISPLAY": DISPLAY})
    time.sleep(3)   # let the DE initialise before VNC attaches

    vnc_cmd = ["x11vnc", "-display", DISPLAY, "-listen", "localhost",
               "-xkb", "-forever", "-shared", "-rfbport", str(VNC_PORT)]

    if password:
        passwd_file = os.path.expanduser("~/.vnc/x11vnc.passwd")
        os.makedirs(os.path.dirname(passwd_file), exist_ok=True)
        run(["x11vnc", "-storepasswd", password, passwd_file])
        vnc_cmd += ["-rfbauth", passwd_file]
        log.info("VNC password stored at %s", passwd_file)
    else:
        vnc_cmd.append("-nopw")
        log.warning("No VNC password set (-nopw).")

    proc = spawn(vnc_cmd)
    if not wait_for_port(VNC_PORT, timeout=15):
        fail("x11vnc did not start listening on port {0} in time.".format(VNC_PORT))
    log.info("Step 3 complete. x11vnc PID=%d, VNC on localhost:%d", proc.pid, VNC_PORT)
    return proc


# =============================================================================
# Step 4: noVNC / websockify
# =============================================================================

# =============================================================================
# TLS (HTTPS / WSS) for noVNC
# =============================================================================

# Self-signed keys live next to this script (don't commit them to git)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SELF_SIGNED_CERT = os.path.join(SCRIPT_DIR, "novnc-cert.pem")
SELF_SIGNED_KEY = os.path.join(SCRIPT_DIR, "novnc-key.pem")


def _is_ip(value):
    try:
        socket.inet_aton(value)
        return value.count(".") == 3
    except (OSError, socket.error):
        return False


def check_tls_args(args):
    """Fail fast on bad TLS options, before spending minutes on installs."""
    if args.no_tls and (args.cert or args.key):
        sys.stderr.write("--no-tls cannot be combined with --cert/--key.\n")
        sys.exit(1)
    if args.key and not args.cert:
        sys.stderr.write("--key requires --cert.\n")
        sys.exit(1)
    for label, path in (("--cert", args.cert), ("--key", args.key)):
        if path and not os.access(path, os.R_OK):
            sys.stderr.write(
                "{0} {1} is missing or not readable by this user.\n"
                "Let's Encrypt keys under /etc/letsencrypt are root-only: copy them next to "
                "this script with chmod 600, or run with a user that can read them.\n"
                .format(label, path))
            sys.exit(1)


def _san_list(host):
    sans = ["DNS:localhost", "IP:127.0.0.1"]
    if host and host != "localhost" and not host.startswith("<"):
        sans.append(("IP:" if _is_ip(host) else "DNS:") + host)
    return sans


def _existing_cert_ok(cert, key, host):
    """True if the saved pair exists, isn't expiring and still covers host."""
    if not (os.path.exists(cert) and os.path.exists(key)):
        return False
    if run(["openssl", "x509", "-checkend", "86400", "-noout", "-in", cert],
           check=False, capture=True).returncode != 0:
        log.info("Self-signed certificate expired or expiring; regenerating.")
        return False
    text = run(["openssl", "x509", "-noout", "-text", "-in", cert],
               check=False, capture=True).stdout or ""
    for san in _san_list(host):
        # openssl prints "IP Address:1.2.3.4" / "DNS:name"
        if san.replace("IP:", "IP Address:") not in text:
            log.info("Certificate does not cover %s (address changed?); regenerating.", san)
            return False
    return True


def generate_self_signed(host, days=365):
    """Create (or reuse) the self-signed cert/key next to this script."""
    cert, key = SELF_SIGNED_CERT, SELF_SIGNED_KEY
    if not require("openssl"):
        fail("openssl not found; needed to generate the TLS keys.")
    if _existing_cert_ok(cert, key, host):
        log.info("Reusing TLS certificate %s", cert)
        return cert, key

    if not os.access(SCRIPT_DIR, os.W_OK):
        fail("Cannot write TLS keys to {0} (not writable by this user).".format(SCRIPT_DIR))
    sans = _san_list(host)

    # A config file keeps SANs working on OpenSSL 1.0.2 (CentOS 7), which has no -addext
    conf_path = os.path.join(SCRIPT_DIR, ".novnc-openssl.cnf")
    with open(conf_path, "w") as fh:
        fh.write("[req]\ndistinguished_name = dn\nx509_extensions = ext\nprompt = no\n"
                 "[dn]\nCN = {0}\n"
                 "[ext]\nsubjectAltName = {1}\n"
                 "basicConstraints = critical,CA:FALSE\n"
                 "keyUsage = critical,digitalSignature,keyEncipherment\n"
                 "extendedKeyUsage = serverAuth\n".format(host or "localhost", ",".join(sans)))

    old_umask = os.umask(0o077)          # key is created 0600
    try:
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
             "-days", str(days), "-config", conf_path,
             "-keyout", key, "-out", cert], capture=True)
    finally:
        os.umask(old_umask)
        os.remove(conf_path)
    os.chmod(key, 0o600)
    log.info("Generated self-signed certificate %s (SAN: %s)", cert, ", ".join(sans))
    return cert, key


def prepare_tls(args, host):
    """Return (cert, key) or None. key may be None if cert is a combined PEM."""
    if args.no_tls:
        return None
    if args.cert:
        cert, key = args.cert, args.key
    else:
        # normally created in step 1; this also covers --no-install on a fresh host
        cert, key = generate_self_signed(host)

    # Validate here: websockify only loads the cert per connection, so a bad
    # cert/key pair would otherwise surface as a vague browser error.
    import ssl
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
    except (ssl.SSLError, OSError) as exc:
        fail("Certificate/key could not be loaded ({0}): {1}".format(cert, exc))
    return cert, key


def step4_novnc(osi, bind, tls=None):
    log.info("=== Step 4: Starting noVNC / websockify on %s:%d ===", bind, NOVNC_PORT)
    ws = websockify_cmd(osi)
    if ws is None:
        if osi.family == "el":
            fail("websockify venv not found at {0}. Run without --no-install "
                 "to create it.".format(WEBSOCKIFY_VENV))
        fail("websockify not found.")

    web_dir = next((d for d in NOVNC_WEB_DIRS if os.path.isdir(d)), None)
    cmd = [ws]
    if web_dir:
        cmd += ["--web", web_dir]
    else:
        log.warning("noVNC web directory not found; browser UI may not load.")
    if tls:
        cert, key = tls
        cmd += ["--cert", cert, "--ssl-only"]
        if key:
            cmd += ["--key", key]
    cmd += ["{0}:{1}".format(bind, NOVNC_PORT), "localhost:{0}".format(VNC_PORT)]

    proc = spawn(cmd)
    if not wait_for_port(NOVNC_PORT, timeout=15):
        fail("websockify did not start on port {0} in time.".format(NOVNC_PORT))
    log.info("Step 4 complete. noVNC PID=%d", proc.pid)
    return proc


def handle_firewalld(osi, bind, open_port):
    if osi.family != "el" or bind in ("127.0.0.1", "localhost") or not require("firewall-cmd"):
        return
    state = run(sudo() + ["firewall-cmd", "--state"], check=False, capture=True)
    if state.returncode != 0:
        return   # firewalld not running
    if open_port:
        run(sudo() + ["firewall-cmd", "--add-port={0}/tcp".format(NOVNC_PORT)], check=False)
        log.info("Opened %d/tcp in firewalld (runtime only, gone after reload/reboot).",
                 NOVNC_PORT)
    else:
        log.warning("firewalld is running and may block port %d. Use --open-firewall, "
                    "or connect through an SSH tunnel.", NOVNC_PORT)


# =============================================================================
# Cleanup / misc
# =============================================================================

def cleanup(*_):
    if not _procs:
        return
    log.info("Shutting down background processes ...")
    for proc in reversed(_procs):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    del _procs[:]
    log.info("All processes stopped.")


def get_public_ip():
    """EC2 metadata, IMDSv2 first (IMDSv1 is disabled on many new instances)."""
    import urllib.request
    base = "http://169.254.169.254/latest"
    headers = {}
    try:
        req = urllib.request.Request(base + "/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        headers["X-aws-ec2-metadata-token"] = urllib.request.urlopen(req, timeout=2).read().decode()
    except Exception:
        pass
    try:
        req = urllib.request.Request(base + "/meta-data/public-ipv4", headers=headers)
        return urllib.request.urlopen(req, timeout=2).read().decode().strip()
    except Exception:
        return "<your-ec2-ip>"


def parse_args():
    p = argparse.ArgumentParser(description="Headless X11 GUI setup (CentOS/RHEL/Debian)")
    p.add_argument("--no-install", action="store_true", help="Skip package installation")
    p.add_argument("--resolution", default="1920x1080x24",
                   help="Xvfb screen resolution (default: 1920x1080x24)")
    p.add_argument("--vnc-password", metavar="PASS", help="Set a VNC password (recommended)")
    p.add_argument("--bind", default="0.0.0.0",
                   help="Address noVNC listens on (default 0.0.0.0; use 127.0.0.1 with an SSH tunnel)")
    p.add_argument("--open-firewall", action="store_true",
                   help="Open the noVNC port in firewalld (EL only, runtime rule)")
    p.add_argument("--cert", metavar="PATH",
                   help="Use your own TLS certificate (PEM) instead of the self-signed one")
    p.add_argument("--key", metavar="PATH",
                   help="TLS private key (PEM); omit if --cert contains the key")
    p.add_argument("--no-tls", action="store_true",
                   help="Serve plain HTTP (default: HTTPS with self-signed keys "
                        "generated next to this script during install)")
    p.add_argument("--dry-run", action="store_true",
                   help="Detect OS/Python, print the install plan and exit")
    p.add_argument("--os-release", default="/etc/os-release", help=argparse.SUPPRESS)
    return p.parse_args()


# =============================================================================
# Entry point
# =============================================================================

def main():
    global PY
    args = parse_args()

    # 0a. OS
    osi = detect_os(args.os_release)
    if osi is None:
        sys.stderr.write(
            "Unsupported or undetected OS (read {0}).\n"
            "Supported: CentOS 7, CentOS Stream 8/9, RHEL 7/8/9, Rocky 8/9, "
            "AlmaLinux 8/9, Debian/Ubuntu.\n".format(args.os_release))
        sys.exit(3)
    log.info("Detected OS: %s (%s %s)", osi.pretty, osi.distro, osi.major)
    if osi.eol:
        log.warning("%s is end-of-life; package repos may need to point at the vault.",
                    osi.pretty)

    # 0b. Python
    py = check_python(osi)
    PY = PY_PROFILES[py]
    log.info("Detected Python %d.%d (supported).", py[0], py[1])

    check_tls_args(args)
    use_tls = not args.no_tls
    self_signed = use_tls and not args.cert

    plan = build_install_plan(osi)
    if args.dry_run:
        if self_signed:
            log.info("TLS: self-signed keys generated during install in %s", SCRIPT_DIR)
        else:
            log.info("TLS: %s", "cert " + args.cert if args.cert else "off (plain HTTP)")
        log.info("Dry run - install plan for %s:", osi.pretty)
        for desc, cmd, must in plan:
            log.info("  [%s] %-28s %s", "required" if must else "optional",
                     desc, PY["fmt_cmd"](cmd))
        return

    if not use_tls and args.bind not in ("127.0.0.1", "localhost"):
        log.warning("noVNC is served over plain HTTP on %s: screen contents and "
                    "keystrokes are unencrypted. Drop --no-tls to use HTTPS.",
                    args.bind)
    if not args.vnc_password and args.bind not in ("127.0.0.1", "localhost"):
        log.warning("*** No VNC password and noVNC bound to %s: anyone who can reach "
                    "port %d gets this desktop. ***", args.bind, NOVNC_PORT)

    signal.signal(signal.SIGINT, lambda *a: (cleanup(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *a: (cleanup(), sys.exit(0)))

    host = "localhost" if args.bind in ("127.0.0.1", "localhost") else get_public_ip()
    step1_install(osi, plan, skip=args.no_install,
                  tls_host=host if self_signed else None)
    step2_xvfb(args.resolution)
    step3_x11vnc(args.vnc_password)
    tls = prepare_tls(args, host)
    step4_novnc(osi, args.bind, tls)
    handle_firewalld(osi, args.bind, args.open_firewall)

    log.info("=" * 55)
    log.info("  GUI ready - open in your browser:")
    log.info("  %s://%s:%d/vnc.html", "https" if tls else "http", host, NOVNC_PORT)
    if self_signed:
        log.info("  (self-signed: the browser will warn once; accept to continue)")
    log.info("=" * 55)
    log.info("Press Ctrl+C to stop all services.")

    reported = set()
    try:
        while True:
            for proc in _procs:
                if proc.poll() is not None and proc.pid not in reported:
                    reported.add(proc.pid)
                    log.warning("Process PID=%d (%s) exited (rc=%d).",
                                proc.pid, proc.args[0], proc.returncode)
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
