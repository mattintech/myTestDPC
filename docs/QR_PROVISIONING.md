# QR provisioning

`scripts/provision.py` builds the APK, serves it, and renders a provisioning QR
in one command. Every value in the QR is read from the artifact being served —
the package name, the admin component, and the signing-certificate checksum —
so the QR cannot drift from the build the way a hand-maintained JSON does.

```console
.venv/bin/python scripts/provision.py --build
```

Then:

1. **Factory reset** the device.
2. On the setup-wizard welcome screen, **tap 6 times** in the same spot.
   - Android O and older first prompt to connect to the internet so the wizard
     can download a QR reader. Android P+ already has one.
3. Join the same Wi-Fi as this machine and **scan the code**.

The page shows a live indicator that flips when the device pulls the APK, which
is the quickest way to tell a scan succeeded from a download failing.

## Setup

Once:

```console
python3 -m venv .venv
.venv/bin/pip install segno
```

`aapt2` and `apksigner` are located automatically from `ANDROID_HOME`,
`ANDROID_SDK_ROOT`, or the default SDK path.

## Options

| Flag | Effect |
| --- | --- |
| `--build` | Run `./build.sh` before serving |
| `--apk PATH` | Serve a specific APK (default `bazel-bin/testdpc.apk`) |
| `--port N` | Listen on a different port (default 8777) |
| `--host IP` | Override the detected download address |
| `--wifi-ssid` / `--wifi-pass` / `--wifi-security` | Join a network during provisioning |
| `--leave-system-apps` | `LEAVE_ALL_SYSTEM_APPS_ENABLED` |
| `--skip-consent` | `SKIP_USER_CONSENT` |
| `--skip-education` | `SKIP_EDUCATION_SCREENS` |
| `--allow-offline` | `ALLOW_OFFLINE` |
| `--skip-encryption` | `SKIP_ENCRYPTION` (pre-N only) |
| `--terminal` | Also print the QR to the terminal |

All of these are also togglable in the web UI, which regenerates the QR live —
the CLI flags just seed the initial state.

## The download address

The device downloads the APK from this machine, so the QR must contain an
address the device can reach on the LAN. The script detects it automatically
and **refuses to build a QR containing a loopback address**, because a device
pointed at `127.0.0.1` tries to download from itself and fails with no useful
error.

If the detected address is wrong — multiple interfaces, a VPN, a machine on two
networks — override it in the page's *Download address* field, or pass
`--host`. Auto-detection stays in place; the override only applies to the QR
being rendered.

## Why hand-built QR codes fail

Three failure modes account for nearly all of them. The script avoids all three,
but they are worth knowing if you build a QR another way.

### 1. The checksum

`PROVISIONING_DEVICE_ADMIN_SIGNATURE_CHECKSUM` is
`base64url(sha256(signing-cert-DER))` with padding stripped. The device
recomputes it after downloading and aborts on a mismatch, so it must come from
the APK you are actually serving — it changes whenever you re-sign.

Reading it by hand is easy to get wrong. `apksigner` prints:

```
V3.0 Signer: certificate SHA-256 digest: 6379ddb41110a3f38dc9cd0855ffdb09…
```

There are **two** colon-separated fields before the hex, so the common
`awk -F': ' '{print $2}'` idiom returns the label `certificate SHA-256 digest`
rather than the digest, and silently produces a bogus checksum. Match the label
explicitly instead:

```console
APK=bazel-bin/testdpc.apk

HEX=$(apksigner verify --print-certs "$APK" \
      | sed -n 's/.*certificate SHA-256 digest: *\([0-9a-f]*\).*/\1/p' | head -1)
echo -n "$HEX" | xxd -r -p | openssl base64 | tr '+/' '-_' | tr -d '='
```

Or read the certificate straight out of the signature block, which needs no
`apksigner` at all:

```console
unzip -p "$APK" 'META-INF/*.RSA' \
  | openssl pkcs7 -inform DER -print_certs \
  | openssl x509 -outform DER \
  | openssl dgst -sha256 -binary \
  | openssl base64 | tr '+/' '-_' | tr -d '='
```

Both must agree.

### 2. The component name

`PROVISIONING_DEVICE_ADMIN_COMPONENT_NAME` is
`<applicationId>/<receiver class>`. The two halves come from different places:
the package half is the **installed applicationId**, while the class half is
resolved against the manifest's `package` attribute.

They match today (`com.afwsamples.testdpc/com.afwsamples.testdpc.DeviceAdminReceiver`),
but they are not the same thing. If the `applicationId` is ever overridden, the
component becomes *mixed* and hand-writing it is the most common cause of
"can't set up device". The script reads both halves out of the built APK, so it
stays correct either way.

### 3. Scannability

The setup-wizard scanner is far less capable than a camera app, and a QR that
reads fine on your phone's camera may not read there at all. What matters:

- **Quiet zone.** The spec requires a 4-module margin. Less than that and
  strict scanners reject the code outright.
- **No resampling.** Render at an integer pixel scale and display at native
  size. Scaling a QR to an arbitrary CSS width blurs the module edges into grey
  and the scanner gives up — this is easy to do by accident with an SVG.
- **Density.** Every extra you add grows the payload and shrinks the modules.
  The page shows the current QR version and module count, and warns when it
  gets dense enough to cause trouble.

For reference, the baseline QR here is version 12 (65×65 modules) and decodes
reliably down to about 150px. Turning on several extras pushes it past version
18, where the page starts warning.

## Endpoints

Useful for debugging:

| Path | Returns |
| --- | --- |
| `/` | The scan page |
| `/qr.png` | The QR as PNG |
| `/provisioning.json` | The exact JSON encoded in the QR |
| `/status` | APK download count as JSON |
| `/testdpc.apk` | The APK |

All accept the same query parameters as the UI (`host`, `wifi_ssid`,
`leave_system_apps`, …), so you can check what a given combination produces
without touching the page.
