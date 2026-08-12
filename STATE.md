---
description: Fork of Google's TestDPC for testing Android Enterprise policies upstream doesn't expose
status: active
---

## Current state

- Work lives on `matts-testdpc` (4 commits on top of upstream). `master` is a
  pristine mirror of `upstream/master` — never commit there. Still need to flip
  the GitHub default branch to `matts-testdpc` in repo settings.
- **Set default launcher** policy works: pins a HOME app on a Device Owner
  device without LockTask, so Samsung DeX keeps working. Under
  *Policy management → Apps management*.
- **`scripts/provision.py`** does QR provisioning end to end — builds the APK,
  serves it over HTTP, renders the QR. Run it with
  `.venv/bin/python scripts/provision.py --build`, default port 8777.
  Reads the component name and signature checksum straight off the built APK,
  so the QR can't go stale. Web UI toggles the common provisioning extras
  (wifi, leave-system-apps, skip-consent, etc.) and warns when the QR gets too
  dense to scan.
- The QR scanning problem is fixed. It failed because the quiet zone was 2
  modules instead of the required 4, and the SVG was being CSS-downscaled into
  grey mush. Now a PNG at integer scale shown at native size.
- The `applicationId` override (`com.mattintech.mytestdpc`) was tried and
  **removed entirely** from history. It broke the FileProvider authority and
  the provisioning component name for no real benefit. Package stays
  `com.afwsamples.testdpc`. Don't retry this.
- `feature/set-default-launcher` still exists on origin and points at the old
  history containing those removed commits — don't branch off it.
- Changes are deliberately kept **additive** (new files, appended entries) so
  `git rebase upstream/master` stays conflict-free. Only README and .gitignore
  edit upstream-owned lines.

## Next steps

- [ ] None at this time.
