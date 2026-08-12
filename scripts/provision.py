#!/usr/bin/env python3
"""Serve the built APK over HTTP and show a QR code that provisions it.

Everything in the QR is derived from the APK you are actually serving -- the
package name, the admin component, and the signing-certificate checksum -- so
the QR can never drift from the build the way a hand-maintained JSON does.

    .venv/bin/python scripts/provision.py --build

Then factory reset the device, tap 6 times on the setup-wizard welcome screen,
and scan the QR from the page it prints. The page also lets you override the
download address and toggle the common provisioning extras.

Requires: segno (pip install segno), plus aapt2/apksigner from the Android SDK.
"""

import argparse
import base64
import functools
import glob
import http.server
import io
import ipaddress
import json
import os
import re
import socket
import string
import subprocess
import sys
import threading
import urllib.parse

import segno

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_APK = os.path.join(REPO, "bazel-bin", "testdpc.apk")
APK_PATH = "/testdpc.apk"

EXTRA = "android.app.extra.PROVISIONING_"

# The setup-wizard scanner is far less capable than a camera app, so the code
# is tuned for readability over compactness:
#   * PNG at an integer scale, shown at natural size -- a CSS-resized SVG
#     resamples the modules into grey mush and stops scanning entirely.
#   * QUIET_ZONE 4 is the spec-mandated margin; strict scanners reject less.
#   * Error correction "L" keeps the grid coarse. Damage tolerance is
#     pointless for a code displayed on a screen, and every extra version
#     shrinks the modules.
QR_SCALE = 6
QUIET_ZONE = 4
QR_ERROR = "l"

# Versions past these get visibly harder for the wizard's scanner to lock on,
# which is what the density readout on the page is warning about.
DENSE_VERSION = 16
VERY_DENSE_VERSION = 20

# Boolean extras worth toggling per-run. Order is the order shown on the page.
FLAGS = [
    ("leave_system_apps", "LEAVE_ALL_SYSTEM_APPS_ENABLED",
     "Leave all system apps enabled",
     "Keeps the stock apps around instead of disabling them on setup."),
    ("skip_consent", "SKIP_USER_CONSENT",
     "Skip user consent",
     "Suppresses the confirmation screen. Test devices only."),
    ("skip_education", "SKIP_EDUCATION_SCREENS",
     "Skip education screens",
     "Drops the explanatory screens for a faster loop."),
    ("allow_offline", "ALLOW_OFFLINE",
     "Allow offline provisioning",
     "Permits setup without a network round-trip."),
    ("skip_encryption", "SKIP_ENCRYPTION",
     "Skip encryption",
     "Only affects pre-N devices; ignored on anything modern."),
]

WIFI_SECURITY = ["WPA", "WEP", "EAP", "NONE"]


# --------------------------------------------------------------------------
# Android SDK tools
# --------------------------------------------------------------------------

def find_sdk_tool(name):
    """Return the newest build-tools copy of `name`, or None."""
    roots = [
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        os.path.expanduser("~/Library/Android/sdk"),
        os.path.expanduser("~/Android/Sdk"),
    ]
    found = []
    for root in filter(None, roots):
        found += glob.glob(os.path.join(root, "build-tools", "*", name))

    def version_key(path):
        parts = os.path.basename(os.path.dirname(path)).split(".")
        return [int(p) if p.isdigit() else 0 for p in parts]

    return sorted(found, key=version_key)[-1] if found else None


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


# --------------------------------------------------------------------------
# Facts extracted from the APK
# --------------------------------------------------------------------------

def signature_checksum(apk):
    """base64url(sha256(signing cert DER)), padding stripped.

    The device recomputes this after downloading and aborts on a mismatch,
    which is why it is always read off the real artifact rather than cached.
    """
    apksigner = find_sdk_tool("apksigner")
    cert_hex = None

    if apksigner:
        out = run([apksigner, "verify", "--print-certs", apk])
        # Must be the *certificate* digest, and the label has to be matched
        # exactly: the real line reads "V3.0 Signer: certificate SHA-256
        # digest: <hex>", so splitting on ": " and taking field 2 yields the
        # label rather than the hex, and silently produces a bogus checksum.
        m = re.search(r"certificate SHA-256 digest:\s*([0-9a-fA-F]{64})", out)
        if m:
            cert_hex = m.group(1)

    if cert_hex is None:
        # No apksigner: pull the cert straight out of the signature block.
        out = run(
            "unzip -p %s 'META-INF/*.RSA' 'META-INF/*.DSA' 'META-INF/*.EC' 2>/dev/null"
            " | openssl pkcs7 -inform DER -print_certs"
            " | openssl x509 -outform DER | openssl dgst -sha256 -hex" % apk,
            shell=True,
        )
        m = re.search(r"([0-9a-f]{64})", out)
        if not m:
            sys.exit("Could not read the signing certificate from %s" % apk)
        cert_hex = m.group(1)

    return base64.urlsafe_b64encode(bytes.fromhex(cert_hex)).decode().rstrip("=")


def admin_component(apk):
    """Return "<applicationId>/<receiver class>" read from the APK manifest.

    Both halves come from the built manifest on purpose. The class name is
    resolved against the manifest `package`, while the package half is the
    installed applicationId -- if those two ever diverge again the component
    is *mixed*, and hand-writing it is the classic cause of "can't set up
    device".
    """
    aapt2 = find_sdk_tool("aapt2")
    if not aapt2:
        sys.exit("aapt2 not found. Set ANDROID_HOME to your SDK.")

    tree = run([aapt2, "dump", "xmltree", apk, "--file", "AndroidManifest.xml"])

    package = re.search(r'A: package="([^"]+)"', tree)
    if not package:
        sys.exit("No package name in %s" % apk)

    # Walk the element tree by indentation and take the <receiver> guarded by
    # BIND_DEVICE_ADMIN.
    receiver, depth, name = None, None, None
    for line in tree.splitlines():
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if stripped.startswith("E: "):
            if depth is not None and indent <= depth:
                depth, name = None, None  # left the receiver block
            if stripped.startswith("E: receiver"):
                depth, name = indent, None

        if depth is None:
            continue
        if m := re.search(r'android:name\(0x01010003\)="([^"]+)"', stripped):
            name = name or m.group(1)
        if "BIND_DEVICE_ADMIN" in stripped and name:
            receiver = name
            break

    if not receiver:
        sys.exit("No BIND_DEVICE_ADMIN receiver found in %s" % apk)

    return "%s/%s" % (package.group(1), receiver)


# --------------------------------------------------------------------------
# Where the device should download from
# --------------------------------------------------------------------------

def lan_ip():
    """This machine's LAN address, or None if we aren't on a network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # UDP: no packets leave the machine
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ipaddress.ip_address(ip).is_loopback else ip


def host_problem(host):
    """Return why `host` is unusable in a QR, or None if it is fine.

    The QR is read by a freshly wiped phone, so a loopback address points the
    device at itself and the download fails with no useful error.
    """
    if not host:
        return "Enter an address."
    if host == "localhost":
        return "localhost points the device at itself."
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None  # a hostname we cannot judge; trust the user
    if addr.is_loopback:
        return "%s is loopback -- the device would download from itself." % host
    if addr.is_unspecified:
        return "%s is not a reachable address." % host
    return None


def resolve_startup_host(override, allow_loopback):
    """Pick the address baked into the QR at startup."""
    host = override or lan_ip()
    if host is None:
        sys.exit(
            "Not on a network -- no LAN address to advertise.\n"
            "Join the same Wi-Fi as the device, or pass --host <ip>."
        )
    problem = host_problem(host)
    if problem and not allow_loopback:
        sys.exit("Refusing to build a QR: %s\n"
                 "Omit --host to auto-detect this machine's LAN address." % problem)
    return host


# --------------------------------------------------------------------------
# Provisioning options
# --------------------------------------------------------------------------

class Options:
    """The knobs the page exposes, resolved per request."""

    def __init__(self, flags=None, wifi_ssid="", wifi_pass="",
                 wifi_security="WPA", wifi_hidden=False):
        self.flags = dict(flags or {})
        self.wifi_ssid = wifi_ssid
        self.wifi_pass = wifi_pass
        self.wifi_security = wifi_security if wifi_security in WIFI_SECURITY else "WPA"
        self.wifi_hidden = wifi_hidden

    @classmethod
    def from_query(cls, query, fallback):
        """Build from query params, falling back to the CLI defaults.

        A request that carries no option params at all (the initial page load)
        yields the defaults untouched.
        """
        if not any(k in query for k in
                   ["opts"] + [f[0] for f in FLAGS] +
                   ["wifi_ssid", "wifi_pass", "wifi_security", "wifi_hidden"]):
            return fallback

        def one(key, default=""):
            return (query.get(key, [default])[0] or "").strip()

        return cls(
            flags={key: one(key) == "1" for key, _, _, _ in FLAGS},
            wifi_ssid=one("wifi_ssid"),
            wifi_pass=one("wifi_pass"),
            wifi_security=one("wifi_security", "WPA"),
            wifi_hidden=one("wifi_hidden") == "1",
        )

    def extras(self):
        out = {}
        for key, extra, _, _ in FLAGS:
            if self.flags.get(key):
                out[EXTRA + extra] = True
        if self.wifi_ssid:
            out[EXTRA + "WIFI_SSID"] = self.wifi_ssid
            out[EXTRA + "WIFI_SECURITY_TYPE"] = self.wifi_security
            if self.wifi_pass and self.wifi_security != "NONE":
                out[EXTRA + "WIFI_PASSWORD"] = self.wifi_pass
            if self.wifi_hidden:
                out[EXTRA + "WIFI_HIDDEN"] = True
        return out


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

PAGE = string.Template("""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Provision myTestDPC</title>
<style>
  :root {
    --bg: #f4f5f7; --panel: #fff; --ink: #16181d; --dim: #6b7280;
    --line: #e4e6eb; --accent: #2f6df6; --ok: #128a5c; --bad: #c0392b;
    --warn: #b26a00; --field: #fff; --sunken: #fafbfc;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0e1014; --panel: #171a21; --ink: #eceef2; --dim: #8b93a3;
      --line: #262b35; --accent: #6f9bff; --ok: #35c98a; --bad: #ff7a6b;
      --warn: #e0a33a; --field: #0f1218; --sunken: #12151b;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; padding: 2.5rem 1.25rem;
    background: var(--bg); color: var(--ink);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    display: flex; align-items: center; justify-content: center;
  }
  .card {
    width: 100%; max-width: 960px; background: var(--panel);
    border: 1px solid var(--line); border-radius: 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,.05), 0 12px 32px rgba(0,0,0,.06);
    overflow: hidden;
  }
  .grid { display: grid; grid-template-columns: auto 1fr; }
  @media (max-width: 820px) { .grid { grid-template-columns: 1fr; } }

  .left { padding: 2rem; border-right: 1px solid var(--line);
          background: var(--sunken); display: flex; flex-direction: column;
          align-items: center; gap: 1rem; }
  @media (max-width: 820px) {
    .left { border-right: 0; border-bottom: 1px solid var(--line); }
  }
  /* QR stays on white in both themes -- scanners need the contrast. */
  .frame { background: #fff; padding: 12px; border-radius: 12px;
           line-height: 0; transition: opacity .15s ease; }
  .frame.busy { opacity: .35; }
  /* Shown at its exact pixel size: any resampling blurs the modules and the
     setup-wizard scanner gives up. Only shrink if the window forces it. */
  .frame img { display: block; width: ${qrpx}px; height: auto;
               max-width: 100%; image-rendering: pixelated; }
  .density { font-size: .72rem; color: var(--dim);
             font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .density.warn { color: var(--warn); }
  .density.bad { color: var(--bad); }

  .body { padding: 2rem; display: flex; flex-direction: column; min-width: 0; }
  h1 { margin: 0 0 .25rem; font-size: 1.05rem; font-weight: 650;
       letter-spacing: -.01em; }
  .sub { margin: 0 0 1.25rem; color: var(--dim); font-size: .875rem; }

  .status {
    display: flex; align-items: center; gap: .55rem; margin-bottom: 1.25rem;
    padding: .6rem .85rem; border: 1px solid var(--line);
    border-radius: 9px; font-size: .85rem; color: var(--dim);
  }
  .status.hit { color: var(--ok);
                border-color: color-mix(in srgb, var(--ok) 40%, var(--line)); }
  .dot { width: .5rem; height: .5rem; border-radius: 50%;
         background: var(--dim); flex: none; }
  .status:not(.hit) .dot { animation: pulse 1.6s ease-in-out infinite; }
  .status.hit .dot { background: var(--ok); }
  @keyframes pulse { 0%,100% { opacity: .25 } 50% { opacity: 1 } }
  @media (prefers-reduced-motion: reduce) { .dot { animation: none !important } }

  .field { margin-bottom: 1.1rem; }
  .field > label { display: block; font-size: .78rem; color: var(--dim);
                   margin-bottom: .4rem; }
  .row { display: flex; gap: .5rem; }
  input[type=text], input[type=password], select {
    flex: 1; min-width: 0; padding: .5rem .65rem; color: var(--ink);
    background: var(--field); border: 1px solid var(--line);
    border-radius: 8px;
    font: .82rem ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  select { flex: 0 0 auto; }
  input:focus, select:focus { outline: 2px solid var(--accent);
                              outline-offset: -1px; }
  button {
    padding: .5rem .9rem; border: 0; border-radius: 8px; cursor: pointer;
    background: var(--accent); color: #fff; font-size: .82rem; font-weight: 600;
  }
  button.ghost { background: transparent; color: var(--dim);
                 border: 1px solid var(--line); }
  button:disabled { opacity: .5; cursor: default; }
  .msg { margin-top: .45rem; font-size: .78rem; min-height: 1.1em;
         color: var(--dim); }
  .msg.err { color: var(--bad); }

  fieldset { border: 1px solid var(--line); border-radius: 10px;
             padding: .9rem 1rem 1rem; margin: 0 0 1.1rem; }
  legend { font-size: .74rem; color: var(--dim); padding: 0 .35rem;
           text-transform: uppercase; letter-spacing: .05em; }
  .flag { display: flex; gap: .55rem; align-items: flex-start;
          padding: .3rem 0; }
  .flag input { margin: .2rem 0 0; flex: none; accent-color: var(--accent); }
  .flag .t { font-size: .84rem; }
  .flag .d { font-size: .74rem; color: var(--dim); }
  .wifi { display: grid; grid-template-columns: 1fr 1fr; gap: .5rem;
          margin-top: .75rem; }
  @media (max-width: 560px) { .wifi { grid-template-columns: 1fr; } }

  dl { margin: auto 0 0; padding-top: 1.15rem; border-top: 1px solid var(--line);
       display: grid; grid-template-columns: auto 1fr; gap: .4rem 1rem;
       font-size: .76rem; }
  dt { color: var(--dim); }
  dd { margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
       word-break: break-all; }
  details.steps { margin-top: .35rem; font-size: .8rem; color: var(--dim); }
  details.steps ol { margin: .5rem 0 0; padding-left: 1.1rem; }
</style>
<div class="card">
  <div class="grid">
    <div class="left">
      <div class="frame" id="frame">
        <img id="qr" src="/qr.png" alt="Provisioning QR code">
      </div>
      <div class="density" id="density">$density</div>
      <details class="steps">
        <summary>How to scan</summary>
        <ol>
          <li>Factory reset the device.</li>
          <li>Tap <b>6&times;</b> on the welcome screen.</li>
          <li>Join this machine's Wi-Fi, then scan.</li>
        </ol>
      </details>
    </div>

    <div class="body">
      <h1>Provision myTestDPC</h1>
      <p class="sub">Device Owner via QR &mdash; $size&nbsp;MB APK served from this machine.</p>

      <div class="status" id="status">
        <span class="dot"></span><span id="statusText">Waiting for the device&hellip;</span>
      </div>

      <div class="field">
        <label for="host">Download address &mdash; auto-detected, override if wrong</label>
        <div class="row">
          <input type="text" id="host" value="$host" spellcheck="false" autocapitalize="off">
          <button id="apply">Apply</button>
          <button id="auto" class="ghost" title="Back to the detected address">Auto</button>
        </div>
        <div class="msg" id="msg"></div>
      </div>

      <fieldset>
        <legend>Provisioning extras</legend>
        $flags
        <div class="wifi">
          <input type="text" id="wifi_ssid" placeholder="Wi-Fi SSID"
                 value="$wifi_ssid" spellcheck="false" autocapitalize="off">
          <input type="password" id="wifi_pass" placeholder="Wi-Fi password"
                 value="$wifi_pass">
          <select id="wifi_security" title="Security type">$wifi_security</select>
          <label class="flag" style="align-items:center">
            <input type="checkbox" id="wifi_hidden" $wifi_hidden>
            <span class="t">Hidden network</span>
          </label>
        </div>
      </fieldset>

      <dl>
        <dt>Component</dt><dd>$component</dd>
        <dt>Checksum</dt><dd>$checksum</dd>
        <dt>APK URL</dt><dd id="url">$url</dd>
      </dl>
    </div>
  </div>
</div>
<script>
  var detected = "$host";
  var FLAG_IDS = $flag_ids;
  var frame = document.getElementById('frame');
  var hostInput = document.getElementById('host');
  var msg = document.getElementById('msg');
  var apply = document.getElementById('apply');
  var density = document.getElementById('density');

  function query(host) {
    var q = ['host=' + encodeURIComponent(host), 'opts=1'];
    FLAG_IDS.forEach(function (id) {
      q.push(id + '=' + (document.getElementById(id).checked ? '1' : '0'));
    });
    q.push('wifi_ssid=' + encodeURIComponent(document.getElementById('wifi_ssid').value));
    q.push('wifi_pass=' + encodeURIComponent(document.getElementById('wifi_pass').value));
    q.push('wifi_security=' + encodeURIComponent(document.getElementById('wifi_security').value));
    q.push('wifi_hidden=' + (document.getElementById('wifi_hidden').checked ? '1' : '0'));
    return q.join('&');
  }

  function regen() {
    var host = hostInput.value.trim();
    var qs = query(host);
    msg.className = 'msg';
    msg.textContent = '';
    frame.classList.add('busy');
    apply.disabled = true;

    fetch('/qr?' + qs)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) {
          msg.className = 'msg err';
          msg.textContent = d.error;
          return;
        }
        var img = document.getElementById('qr');
        img.style.width = d.px + 'px';
        img.src = '/qr.png?' + qs;
        document.getElementById('url').textContent = d.url;
        density.textContent = d.density;
        density.className = 'density' + (d.densityClass ? ' ' + d.densityClass : '');
        msg.textContent = d.host === detected
          ? 'Using the detected address.'
          : 'Overridden \\u2014 QR now points at ' + d.host + '.';
      })
      .catch(function () {
        msg.className = 'msg err';
        msg.textContent = 'Server unreachable.';
      })
      .then(function () {
        frame.classList.remove('busy');
        apply.disabled = false;
      });
  }

  var timer = null;
  function regenSoon() { clearTimeout(timer); timer = setTimeout(regen, 250); }

  apply.onclick = regen;
  hostInput.onkeydown = function (e) { if (e.key === 'Enter') regen(); };
  document.getElementById('auto').onclick = function () {
    hostInput.value = detected;
    regen();
  };

  FLAG_IDS.concat(['wifi_hidden']).forEach(function (id) {
    document.getElementById(id).onchange = regen;
  });
  document.getElementById('wifi_security').onchange = regen;
  ['wifi_ssid', 'wifi_pass'].forEach(function (id) {
    document.getElementById(id).oninput = regenSoon;
  });

  var st = document.getElementById('status');
  var stText = document.getElementById('statusText');
  setInterval(function () {
    fetch('/status').then(function (r) { return r.json(); }).then(function (d) {
      if (d.downloads > 0) {
        st.classList.add('hit');
        stText.textContent = d.downloads === 1
          ? 'Device downloaded the APK \\u2014 provisioning underway'
          : 'APK downloaded ' + d.downloads + '\\u00d7';
      }
    }).catch(function () { /* server stopped; leave the last state up */ });
  }, 2000);
</script>
""")


def render_flags(opts):
    out = []
    for key, _, title, desc in FLAGS:
        checked = " checked" if opts.flags.get(key) else ""
        out.append(
            '<label class="flag"><input type="checkbox" id="%s"%s>'
            '<span><span class="t">%s</span><br><span class="d">%s</span></span>'
            "</label>" % (key, checked, title, desc))
    return "\n        ".join(out)


def render_security(selected):
    return "".join(
        '<option%s>%s</option>' % (" selected" if s == selected else "", s)
        for s in WIFI_SECURITY)


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------

class Session:
    """Everything the handler needs; the QR is rebuilt per request."""

    def __init__(self, apk, port, detected_host, defaults):
        self.apk = apk
        self.port = port
        self.detected_host = detected_host
        self.defaults = defaults
        self.size_mb = os.path.getsize(apk) / 1e6

        self.component = admin_component(apk)
        self.checksum = signature_checksum(apk)

        self.downloads = 0
        self._lock = threading.Lock()

    def record_download(self):
        with self._lock:
            self.downloads += 1

    def url(self, host):
        return "http://%s:%d%s" % (host, self.port, APK_PATH)

    def payload(self, host, opts):
        # Every character here costs modules, and denser codes are the main
        # reason a QR stops scanning -- which is what the density readout on
        # the page is for.
        extras = {
            EXTRA + "DEVICE_ADMIN_COMPONENT_NAME": self.component,
            EXTRA + "DEVICE_ADMIN_SIGNATURE_CHECKSUM": self.checksum,
            EXTRA + "DEVICE_ADMIN_PACKAGE_DOWNLOAD_LOCATION": self.url(host),
        }
        extras.update(opts.extras())
        return json.dumps(extras)

    def qr(self, host, opts):
        return segno.make(self.payload(host, opts), error=QR_ERROR)

    def qr_png(self, host, opts):
        buf = io.BytesIO()
        self.qr(host, opts).save(buf, kind="png", scale=QR_SCALE, border=QUIET_ZONE)
        return buf.getvalue()

    def qr_info(self, host, opts):
        code = self.qr(host, opts)
        modules = code.symbol_size(border=0)[0]
        px = code.symbol_size(scale=QR_SCALE, border=QUIET_ZONE)[0]
        cls = ""
        note = ""
        if code.version >= VERY_DENSE_VERSION:
            cls, note = "bad", " - very dense, may not scan"
        elif code.version >= DENSE_VERSION:
            cls, note = "warn", " - dense, harder to scan"
        return {"version": code.version, "modules": modules, "px": px,
                "density": "v%d %s%d×%d modules%s"
                           % (code.version, "", modules, modules, note),
                "densityClass": cls}

    def page(self, host, opts):
        info = self.qr_info(host, opts)
        return PAGE.substitute(
            host=host, url=self.url(host), qrpx=info["px"],
            density=info["density"],
            component=self.component, checksum=self.checksum,
            size="%.1f" % self.size_mb,
            flags=render_flags(opts),
            flag_ids=json.dumps([f[0] for f in FLAGS]),
            wifi_ssid=opts.wifi_ssid, wifi_pass=opts.wifi_pass,
            wifi_security=render_security(opts.wifi_security),
            wifi_hidden=" checked" if opts.wifi_hidden else "")


class Handler(http.server.BaseHTTPRequestHandler):
    def __init__(self, *a, session=None, **kw):
        self.session = session
        super().__init__(*a, **kw)

    def _send(self, body, content_type, extra_headers=()):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj):
        self._send(json.dumps(obj), "application/json")

    def do_GET(self):
        parts = urllib.parse.urlparse(self.path)
        path = parts.path
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        host = (query.get("host", [self.session.detected_host])[0] or "").strip()
        host = host or self.session.detected_host
        opts = Options.from_query(query, self.session.defaults)

        if path == "/":
            self._send(self.session.page(self.session.detected_host,
                                         self.session.defaults),
                       "text/html; charset=utf-8")

        elif path == "/qr":
            problem = host_problem(host)
            if problem:
                self._json({"error": problem})
            else:
                info = self.session.qr_info(host, opts)
                info.update({"url": self.session.url(host), "host": host})
                self._json(info)

        elif path == "/qr.png":
            if host_problem(host):
                self.send_error(400)
            else:
                self._send(self.session.qr_png(host, opts), "image/png")

        elif path == "/status":
            self._json({"downloads": self.session.downloads})

        elif path == "/provisioning.json":
            self._send(self.session.payload(host, opts), "application/json")

        elif path == APK_PATH:
            with open(self.session.apk, "rb") as f:
                body = f.read()
            if self.command != "HEAD":
                self.session.record_download()
            self._send(body, "application/vnd.android.package-archive",
                       [("Content-Disposition", 'attachment; filename="testdpc.apk"')])
        else:
            self.send_error(404)

    # Some download managers probe with HEAD before fetching.
    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        # Watching this line appear is how you know the device found us.
        sys.stderr.write("  %s\n" % (fmt % args))


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apk", default=DEFAULT_APK, help="APK to serve")
    # Not 8000: too often already taken by some other local dev server.
    p.add_argument("--port", type=int, default=8777)
    p.add_argument("--build", action="store_true", help="run ./build.sh first")
    p.add_argument("--host", help="override the detected download address")
    p.add_argument("--allow-loopback", action="store_true",
                   help="permit a loopback --host (local testing only)")
    p.add_argument("--wifi-ssid", default="", help="join this network first")
    p.add_argument("--wifi-pass", default="")
    p.add_argument("--wifi-security", default="WPA", choices=WIFI_SECURITY)
    for key, extra, title, _ in FLAGS:
        p.add_argument("--" + key.replace("_", "-"), action="store_true",
                       help="%s (%s)" % (title, extra))
    p.add_argument("--terminal", action="store_true", help="also print the QR here")
    args = p.parse_args()

    if args.build:
        print("building...", flush=True)
        subprocess.run([os.path.join(REPO, "build.sh")], check=True, cwd=REPO)

    apk = os.path.abspath(args.apk)
    if not os.path.exists(apk):
        sys.exit("No APK at %s -- run ./build.sh or pass --build" % apk)

    host = resolve_startup_host(args.host, args.allow_loopback)
    defaults = Options(
        flags={key: getattr(args, key) for key, _, _, _ in FLAGS},
        wifi_ssid=args.wifi_ssid, wifi_pass=args.wifi_pass,
        wifi_security=args.wifi_security)

    session = Session(apk, args.port, host, defaults)
    info = session.qr_info(host, defaults)

    print("\n  apk        %s (%.1f MB)" % (os.path.relpath(apk, REPO), session.size_mb))
    print("  component  %s" % session.component)
    print("  checksum   %s" % session.checksum)
    print("  serving    %s" % session.url(host))
    print("  qr         v%d, %dx%d modules at %dpx (quiet zone %d)"
          % (info["version"], info["modules"], info["modules"],
             QR_SCALE, QUIET_ZONE))
    print("\n  Open http://%s:%d/ to scan and tweak extras." % (host, args.port))
    print("  Ctrl-C to stop.\n", flush=True)

    if args.terminal:
        session.qr(host, defaults).terminal(compact=True, border=QUIET_ZONE)

    handler = functools.partial(Handler, session=session)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
