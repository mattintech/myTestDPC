myTestDPC
=========

A fork of Google's [Test DPC](https://github.com/googlesamples/android-testdpc)
for exercising Android Enterprise behaviour that upstream TestDPC does not
surface.

Upstream is a reference sample: it demonstrates the APIs Google wants to
document. That leaves gaps — policies that exist in `DevicePolicyManager` but
have no UI, combinations that only misbehave on real OEM hardware, and
provisioning paths that are tedious to reproduce by hand. This fork fills those
gaps so they can be tested on a device in minutes.

Everything here tracks upstream closely and stays additive, so fixes from
Google merge in cleanly. See [Fork layout](#fork-layout).

## What this fork adds

### Set default launcher

Pins a HOME app as the default launcher on a Device Owner device **without**
LockTask. Upstream only offers lock task mode, which takes over the device
entirely and breaks Samsung DeX.

This surfaces the existing `addPersistentPreferredActivity` API in the UI:
*Policy management → Apps management → Set default launcher*. Enter a package
name; it resolves that package's HOME activity and registers a persistent
preferred activity for the MAIN/HOME/DEFAULT filter. DeX keeps working.

### One-command QR provisioning

`scripts/provision.py` builds the APK, serves it over HTTP, and renders a
provisioning QR — all from one command, with every value derived from the
artifact you are actually serving.

```console
.venv/bin/python scripts/provision.py --build
```

Open the printed URL, factory reset the device, tap the welcome screen 6×, and
scan. The page lets you flip the common provisioning extras and regenerates the
QR live.

See [docs/QR_PROVISIONING.md](docs/QR_PROVISIONING.md) for the full flow and
why hand-built QR codes tend to fail.

## Getting started

Build with Bazel:

```console
./build.sh          # or: bazel build testdpc
```

Bazel needs `ANDROID_HOME` pointed at your Android SDK, and `ed` on your `PATH`
(the setupdesign library is patched dynamically at build time).

For the provisioning script, create the virtualenv once:

```console
python3 -m venv .venv
.venv/bin/pip install segno
```

### Android Studio import

Use the
[Bazel for Android Studio](https://plugins.jetbrains.com/plugin/9185-bazel-for-android-studio)
plugin and select the folder containing the `BUILD` file. When prompted for a
"project view", choose "Copy external" and pick `scripts/ij.bazelproject`. Then
create a Run Configuration of type "Bazel Command" with `//:testdpc` as the
target expression.

## Provisioning

`scripts/provision.py` covers the QR path end to end. The methods below come
from upstream and still work unchanged.

### AFW# code (Device Owner M+)

1. Factory reset the device.
2. Set up Wi-Fi.
3. When prompted to sign in, enter **afw#testdpc**.
4. Follow the onscreen instructions — choose "Use for work only" for a fully
   managed setup.

### ADB

```console
# Device Owner
adb shell dpm set-device-owner com.afwsamples.testdpc/.DeviceAdminReceiver

# Profile Owner, corporate-owned (after creating a managed profile)
adb shell dpm mark-profile-owner-on-organization-owned-device \
    --user 10 com.afwsamples.testdpc/.DeviceAdminReceiver
```

For Profile Owner on a personal device (BYOD), launch the "Set up TestDPC" app
to create a managed profile and skip adding an account.

### As DM role holder

```console
adb shell cmd role set-bypassing-role-qualification true
adb shell cmd role add-role-holder \
    android.app.role.DEVICE_POLICY_MANAGEMENT com.afwsamples.testdpc
```

Not persisted across reboots — re-run after each restart.

## Fork layout

`master` mirrors `upstream/master` untouched. Development happens on
`matts-testdpc`, which is `master` plus this fork's commits.

```console
git remote add upstream https://github.com/googlesamples/android-testdpc.git

# refresh the mirror
git checkout master
git fetch upstream && git merge --ff-only upstream/master

# replay this fork's commits on top
git checkout matts-testdpc
git rebase upstream/master
```

Changes here are kept **additive** — new files and appended entries rather than
edits to upstream-owned lines — so rebases stay conflict-free. The package name
is deliberately left as `com.afwsamples.testdpc`; overriding the `applicationId`
was tried and reverted, since it breaks the FileProvider authority and the
provisioning component name for no real benefit on a debug build.

## Relationship to upstream

This is a personal testing fork, not a replacement for TestDPC and not
affiliated with Google. For the reference implementation, the Play Store build,
or to report bugs in TestDPC itself, use
[googlesamples/android-testdpc](https://github.com/googlesamples/android-testdpc).

## License

Apache 2.0, inherited from upstream. See the LICENSE file.
