# ENIMAS update release procedure

The stable update channel is main/update_manifest.json. Do not merge a release
manifest into main until the release is approved and its immutable tag is
available. A feature-branch manifest cannot notify installed users.

## Release gate

1. Work on a codex feature branch and keep main at the currently released
   version.
2. Run the complete updater suite and the hardware-independent health check:

       python -m unittest tests.test_updater -v
       & 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' Tools\health_check.py --root . --expected-version 1.4.3

   Also run disposable Windows migration tests from 1.0.7, 1.2.0, and 1.2.4, including
   forced termination at every transaction phase, network loss, disk-full,
   locked-file, dependency-failure, repair, and rollback cases. Hardware is not
   required for an application-only release.
3. Set constants.APP_VERSION, the repository default config.txt, and
   CHANGELOG.md to the same release version.
4. Hydrate every Git LFS object, then commit the complete local release
   candidate without tagging or pushing it. The generator hashes ordinary
   committed blobs and the actual LFS payload described by each committed
   pointer. It fails if an LFS payload is absent or has the wrong size or hash.
5. Generate the manifest from that clean commit:

       python Tools\build_update_manifest.py --version 1.4.3 --source-ref v1.4.3 --release-date 2026-07-22

6. Validate the generated manifest, rerun all updater tests, and amend the
   candidate commit to include only the manifest. Any managed-file change after
   generation invalidates hashes, so repeat from step 4.
7. Obtain explicit release approval. Then create the immutable v1.4.3 tag from
   that exact release commit.
8. Verify several tag-pinned raw file URLs and the complete tag archive before
   merging the same release commit to main. This includes validating at least
   one hydrated Git LFS payload from the archive against the manifest.
9. Merge to main only after the tag checks pass. The manifest becoming visible
   on main is the user-notification event.

Never move or reuse a release tag. If a published payload is wrong, prepare a
new patch version and tag.

## Privilege and system-maintenance gate

Normal source updates, virtual-environment changes, and health checks run as the
installing user. UAC is used only by the fixed control plane under
C:\ProgramData\ENIMAS\admin. Its dispatcher selects a checksum-verified,
versioned installer/updater pair; staging a candidate does not replace the
active pair.

Legacy 1.0.7-1.2.x installations predate this protected control plane and granted
broad write access to the installation. Before applying the first incremental
update, install.bat requests one UAC approval to download and checksum-verify
only the tagged control files, activate them under C:\ProgramData\ENIMAS\admin,
and replace the legacy ACL with SYSTEM/Administrators plus Modify access for the
installing user. It does not reinstall ENIMAS, Python packages, camera drivers,
or the virtual environment. Later application-only updates remain unelevated.
Installations below 1.0.7, incomplete legacy layouts, and manifest-era
installations with a missing or mismatched installed manifest must fail closed
to the explicit data-preserving Repair path before this UAC migration.
The initial broker still starts from the user-downloaded batch file; use a newly
downloaded stable-branch copy from the official KIT GitLab project. A future
signed bootstrap executable is required before claiming resistance to a
malicious local user who can alter that legacy file before UAC approval.

For the 1.4.3 application release, system_update_required must remain false
unless tracked files under camera-driver differ from stable main. The manifest
generator enforces that relationship.

Camera driver or vendor SDK changes are a distinct maintenance release. Windows
driver installation is not transactionally reversible in the same way as
application files. Before approving such a release, require an Entomoscope
hardware test, documented backward compatibility with the previous ENIMAS
version, and a vendor-supported uninstall or recovery procedure. Do not
describe a system-maintenance release as automatically rolled back.

The VAImaging installer currently has no valid Authenticode signature. Its only
approved exception is the pinned SHA-256 digest recorded in install.bat;
changing that payload or digest requires explicit release review.

## Python runtime gate

The first incremental-updater release deliberately retains Python 3.10.11 so a
runtime migration is not combined with updater and installer changes. Python
3.10 reaches end of life in October 2026 and newer security releases have no
official Windows installers. Therefore:

1. Do not treat Python 3.10.11 as a long-term supported runtime.
2. Prepare Python 3.12 in a separate branch and a side-by-side virtual
   environment.
3. Re-lock dependencies under 3.12 and test camera discovery and capture, motor
   movement, stacking, every built-in plugin, ONNX, PyTorch, and rollback on a
   real Entomoscope.
4. Switch the installed runtime only after those tests pass, retaining the 3.10
   environment until the migrated application passes its post-start health
   check.

Do not publish an ENIMAS release that continues to provision Python 3.10 after
its end-of-life date without a separately approved, time-limited support plan.

## Dependency lock

requirements.lock is the install authority. Rebuild it only with Python 3.10
using Tools\build_requirements_lock.py, review the diff, and verify that every
artifact has a SHA-256 hash. The vendored refiners wheel must match the commit
and digest documented under vendor. Dependency-changing updates build a
verified wheelhouse before application, install offline, then remove
distributions absent from the target lock (except the pip/setuptools/wheel
bootstrap tools). Fresh installation and Repair perform the same exact
synchronization after the hashed install.

## Support and recovery

Normal updates use <install root>\updates\bin\enimas_updater.py, keep one
affected-file backup, and restore automatically after application or
health-check failure. Logs are in <install root>\updates\update.log. Completed
fresh or repair staging is cleaned only after a durable successful health
check.

To restore the most recent known-good version for support, close ENIMAS and run
from an elevated terminal only when normal write permission is unavailable:

    & 'C:\Program Files\Python310\python.exe' 'C:\Program Files\ENIMAS\updates\bin\enimas_updater.py' --install-root 'C:\Program Files\ENIMAS' rollback-last

Use install.bat --repair only for a damaged installation or a release marked as
requiring system maintenance. Repair preserves detected configuration, lenses,
model data, and unshipped plugin files, and restores the previous source tree if
setup fails.
