# ENIMAS Installer Repair and Output Preferences Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Use superpowers:test-driven-development for every behavior change.

**Goal:** Make Repair recover from an active same-release control-only bootstrap and persist final JPEG/TIFF and scale-bar choices across ENIMAS restarts.

**Architecture:** Preserve the protected updater architecture and promote a verified control-only directory to the complete payload in place, with the maintenance manifest written last as the completion marker. Persist the two UI choices through the existing user-owned `config.txt` flow using a small pure validation module, then initialize existing runtime state from the validated values.

**Tech Stack:** Python 3, `unittest`, PyQt5, Windows batch installer, JSON update manifests, SHA-256 verified files.

## Global Constraints

- Work only in the isolated `codex/fix-installer-repair-preferences` worktree.
- Do not commit, stage, amend, tag, push, change branches, or create a pull request.
- Do not change `constants.APP_VERSION`, `config.txt` version, `install.bat` revision, `CHANGELOG.md`, `README.md`, or `update_manifest.json`.
- Do not change cameras, autofocus, focus stacking, drivers, SDK versions, Python dependencies, virtual environments, lens calibration, user outputs, or update-channel URLs.
- Preserve the fixed protected ProgramData path, canonical manifest enforcement, ACL checks, verified file copies, pointer activation validation, and durable diagnostics.
- Use `config.txt`, which the updater already preserves, for the two preferences; add no registry or machine-wide settings.
- Raw focus-stack frames remain TIFF. Only final stacked/no-stack output follows the saved final-output preference.
- A saved scale-bar preference never bypasses the existing valid-calibration check.
- New regression tests must be observed failing for the expected reason before production code changes, then observed passing.
- Stop after implementation, review, and verification; wait for explicit user confirmation before every release action.

---

### Task 1: Promote a same-release control-only protected installer to the full payload

**Files:**
- Modify: `Tools/enimas_updater.py:2508-2584`
- Modify: `tests/test_updater.py:1364-1530`

**Interfaces:**
- Consumes: validated release source root, requested validated manifest, fixed protected control root, `current.version`, and the existing version directory's `maintenance_manifest.json`.
- Produces: `_stage_privileged_control_plane(source_root, manifest, control_root)` retaining its signature and either reusing, promoting, or rejecting the existing version before writing `pending.version`.

- [ ] **Step 1: Add the exact failing recovery regression**

Add `ProtectedControlPlaneTests.test_same_release_control_only_version_promotes_to_full_payload`. Build one manifest containing the updater, public installer, protected dispatcher, and at least two system payloads. Use real `_stage_privileged_control_plane` calls to:

1. stage a control-only copy of that manifest;
2. activate it so `current.version` names the release;
3. stage the complete manifest from a validated local source root.

Assert that the current behavior raises `UpdateError` naming the first absent system payload. This is the RED proof.

- [ ] **Step 2: Run the focused RED test**

Run:

```powershell
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_updater.ProtectedControlPlaneTests.test_same_release_control_only_version_promotes_to_full_payload -v
```

Expected before implementation: FAIL because the active control-only directory is validated against the requested full manifest instead of promoted.

- [ ] **Step 3: Implement guarded in-place promotion**

Update the active-same-version branch in `_stage_privileged_control_plane` so it:

1. reads and `validate_manifest`s the existing `maintenance_manifest.json`;
2. verifies the existing version against its own stored manifest;
3. requires identical `source_ref` and identical archive metadata for the updater, `install.bat`, and protected dispatcher;
4. requires every existing system-file entry to exist identically in the requested manifest;
5. reuses an already complete directory without rewriting it;
6. retains a complete directory when the request is control-only, so it cannot be downgraded;
7. promotes a valid subset by copying each requested system payload through `_copy_verified_release_file`;
8. writes the requested full `maintenance_manifest.json` only after all payload copies succeed;
9. validates the completed version against the requested manifest before writing `pending.version`.

Do not delete or replace the running active directory. Treat missing, invalid, mismatched, or corrupt stored control state as `UpdateError` and leave the existing durable error-reporting path intact.

- [ ] **Step 4: Verify GREEN for the original incident**

Run the Step 2 command again. Expected: PASS, with both system payloads present and matching their manifest hashes and the stored maintenance manifest now containing the full system payload list.

- [ ] **Step 5: Add edge-case regressions one at a time**

Add and individually observe RED then GREEN for:

- `test_same_release_full_version_is_not_downgraded_by_control_only_stage`
- `test_interrupted_same_release_promotion_can_be_retried`
- `test_same_release_promotion_rejects_changed_control_metadata`
- `test_same_release_promotion_rejects_corrupt_active_control_file`
- `test_same_release_complete_stage_remains_idempotent`

For interruption, make the second payload copy fail once, assert the old control-only maintenance manifest remains, then retry and assert the complete version validates. Avoid mocking `_stage_privileged_control_plane`; mocking a single verified-copy boundary to inject interruption is allowed.

- [ ] **Step 6: Run protected-installer and full updater tests**

Run:

```powershell
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_updater.ProtectedControlPlaneTests -v
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_updater -v
```

Expected: all tests pass with no unexpected warnings or errors.

- [ ] **Step 7: Self-review without committing**

Inspect the complete Task 1 diff for unsafe deletion, same-release downgrade, unverified copies, early manifest writes, relaxed URL/path/ACL checks, and test-only production behavior. Record commands, RED/GREEN evidence, changed files, and concerns in the assigned report file. Do not commit.

---

### Task 2: Persist final output format and scale-bar preference

**Files:**
- Create: `Tools/user_preferences.py`
- Modify: `constants.py:75-80`
- Modify: `main.py:90-131`
- Modify: `UserInterface/ui.py:702-710`
- Modify: `UserInterface/Image_save_settings.py:159-180`
- Modify: `UserInterface/method_selection.py:52-61,109-110`
- Modify: `tests/test_output_workflows.py:119-190`
- Create: `tests/test_user_preferences.py`

**Interfaces:**
- Produces: `normalize_stack_output_extension(value) -> str`, `parse_config_bool(value, default=False) -> bool`, `load_output_preferences(config) -> None`, and `store_output_preferences(config) -> None` in `Tools.user_preferences`.
- Runtime state: `constants.STACK_OUTPUT_EXTENSION` and new `constants.SHOW_SCALEBAR`.
- Configuration keys: `stack-output-extension=tiff|jpg` and `show-scalebar=true|false`.

- [ ] **Step 1: Add failing pure preference tests**

In `tests/test_user_preferences.py`, add tests that require:

- missing/invalid `stack-output-extension` to load `tiff`;
- case-normalized valid `tiff` and `jpg` to load correctly;
- missing/invalid `show-scalebar` to load `False`;
- case-normalized `true` and `false` to load correctly;
- `store_output_preferences` to serialize only canonical `tiff|jpg` and lowercase `true|false`;
- a store/load round trip to reproduce TIFF/JPEG and enabled/disabled values as a simulated restart.

Name the production function each test exercises and reset changed constants in cleanup.

- [ ] **Step 2: Run preference tests to verify RED**

Run:

```powershell
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_user_preferences -v
```

Expected before implementation: import/test failure because the pure preference module and `constants.SHOW_SCALEBAR` do not yet exist.

- [ ] **Step 3: Implement minimal validated preference helpers**

Create `Tools/user_preferences.py` with the four specified functions. Strip and lowercase text values. Accept only `tiff` or `jpg`; return `tiff` for everything else. Accept only `true` or `false`; return the supplied default for everything else. Loading mutates the two runtime constants. Storing updates the passed configuration mapping using canonical serialized values and preserves unrelated keys.

Add `constants.SHOW_SCALEBAR = False` and update comments to describe the final format as a persisted user choice.

- [ ] **Step 4: Verify pure preference tests GREEN**

Run the Step 2 command again. Expected: all tests pass.

- [ ] **Step 5: Add failing integration tests for startup and dialogs**

Extend output/config tests to require:

- the application config load path applies both new keys before MainWindow construction;
- the application config write path includes both canonical keys while preserving unrelated configuration;
- `MainWindow` initializes `show_scalebar` from `constants.SHOW_SCALEBAR`;
- accepting Image Settings updates both the window and `constants.SHOW_SCALEBAR`;
- cancelling Image Settings leaves both runtime and persisted preference state unchanged;
- accepting Method Settings updates `constants.STACK_OUTPUT_EXTENSION`;
- a JPEG final-output preference still leaves raw frames using `constants.IMG_EXTENSION == "tiff"`.

Use the existing offscreen Qt test setup and minimal dummy objects. Observe the relevant new tests fail before modifying the application/UI paths.

- [ ] **Step 6: Wire preferences into existing lifecycle**

Call `load_output_preferences(cfg)` from `main.save_config` and `store_output_preferences(params)` from `main.write_config`. Initialize `MainWindow.show_scalebar` from `constants.SHOW_SCALEBAR`. On successful Image Settings acceptance, update both the window field and the constant. Keep cancellation side-effect-free. Update Method Settings wording to state that the final format is persisted; retain its existing runtime assignment so the next config write saves it.

Do not persist cropping, background replacement, lens scale, or other unrelated dialog fields.

- [ ] **Step 7: Run preference and output workflow suites**

Run:

```powershell
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_user_preferences tests.test_output_workflows -v
```

Expected: all tests pass and Qt runs offscreen without dialogs blocking the suite.

- [ ] **Step 8: Self-review without committing**

Inspect the Task 2 diff for unsafe config overwrite, invalid-value startup failure, scale-bar calibration bypass, cancelled-dialog mutations, raw-frame format changes, unrelated preference persistence, and test contamination. Record commands, RED/GREEN evidence, changed files, and concerns in the assigned report file. Do not commit.

---

### Task 3: Integrated pre-release verification

**Files:**
- Modify only if a regression found by verification requires a reviewed fix within Tasks 1 or 2.

**Interfaces:**
- Consumes: completed Task 1 and Task 2 working-tree changes.
- Produces: evidence suitable for a pre-commit production-readiness decision, while explicitly leaving immutable release construction pending.

- [ ] **Step 1: Run the complete relevant test set**

Use a sandbox-writable test temporary directory, because these Windows suites otherwise default to `C:\tmp`:

```powershell
$env:ENIMAS_TEST_TMP = Join-Path ([System.IO.Path]::GetTempPath()) 'enimas-test-tmp'
New-Item -ItemType Directory -Path $env:ENIMAS_TEST_TMP -Force | Out-Null
```

Run each module in a separate process to avoid dependency-stub contamination:

```powershell
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_updater -v
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_output_workflows -v
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_camera_freshness -v
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_stack_capture_and_stacker -v
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_update_handoff -v
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_classification_plugin_results -v
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m unittest tests.test_ml_models_routing -v
```

The unchanged baseline is 209 tests run successfully, with 7 model-fixture skips. The existing Google API Python 3.10 future-support warning is allowed but must be reported separately from test failures.

- [ ] **Step 2: Run static and repository checks**

Run:

```powershell
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' -m py_compile Tools/enimas_updater.py Tools/user_preferences.py main.py constants.py UserInterface/ui.py UserInterface/Image_save_settings.py UserInterface/method_selection.py tests/test_updater.py tests/test_user_preferences.py tests/test_output_workflows.py
git diff --check
git status --short
git diff --name-only
git diff --exit-code -- update_manifest.json install.bat CHANGELOG.md README.md
$versionLine = (Select-String -LiteralPath constants.py -Pattern '^APP_VERSION = ').Line
if ($versionLine -ne 'APP_VERSION = "1.4.6"') { throw "Unexpected application version: $versionLine" }
& 'C:\Program Files\ENIMAS\src\venv\Scripts\python.exe' Tools/health_check.py --root . --expected-version 1.4.6
```

`constants.py` legitimately changes for preference defaults, so the explicit `APP_VERSION` assertion protects the release version. The zero-diff assertion confirms separately that `install.bat` remains byte-for-byte unchanged; its existing CRLF behavior is therefore unchanged.

- [ ] **Step 3: Reproduce the production incident end to end in a temporary directory**

Using real staging and activation functions with a synthetic signed-by-hash local manifest, exercise control-only preparation/activation followed by full same-release preparation/activation. Verify the complete protected payload, full maintenance manifest, pointer state, and idempotent repeat. Do not write Program Files or ProgramData.

- [ ] **Step 4: Record the release-boundary limitation**

Do not run a manifest-builder or remote archive validation against uncommitted files. Record that version bump, release notes, committed-blob manifest generation, tag validation, and remote verification remain pending until the user authorizes commit and release preparation.

- [ ] **Step 5: Stop for explicit confirmation**

Report exact evidence, any remaining risks, and the changed-file list. Do not commit, stage, tag, push, or publish.
