# ENIMAS Installer Repair and Output Preference Design

## Goal

Restore reliable installation recovery after a partial first install, and persist the final stacked-image format and scale-bar preference between ENIMAS sessions.

## Scope

This change covers three verified defects:

1. Full Repair cannot promote an active control-only release to the full protected payload when both use the same immutable release name.
2. The final stacked-image JPEG/TIFF choice is stored only in memory.
3. The scale-bar enabled choice is reset to disabled at each startup.

It does not change camera capture, autofocus, focus stacking, image conversion, scale calibration, drivers, SDK versions, Python dependencies, virtual environments, user output files, or release metadata.

## Installer Design

The protected control-plane directory remains versioned by the immutable manifest `source_ref`. A control-only bootstrap and a full Repair may legitimately target the same release directory.

When `_stage_privileged_control_plane` encounters an existing active directory for the requested release:

- Validate the directory's stored maintenance manifest and its existing control files before trusting or modifying it.
- Require the stored manifest to identify the same immutable release and require the existing control-file metadata to agree with the requested release.
- If the existing directory already satisfies the requested full manifest, keep the current idempotent reuse behavior.
- If the existing directory is already complete and a later control-only request targets the same release, retain the complete manifest and payloads; never downgrade it to a control-only state.
- If it is a valid control-only subset of the same release and the requested manifest adds protected system payloads, copy every requested payload through the existing size-and-SHA-256 verification path.
- Write the requested full maintenance manifest only after all files have been durably copied.
- Validate the completed directory against the full manifest before setting `pending.version`.
- Reject malformed, mismatched, or corrupted existing state instead of deleting or silently replacing trusted control files.

Promotion is performed safely in the existing release directory because its versioned `install.bat` and updater may be executing during Repair. The final manifest acts as the completion marker: an interrupted promotion remains identifiable as control-only, and a subsequent Repair can retry verified payload copies.

The protected dispatcher, fixed ProgramData path checks, ACL checks, canonical manifest URL enforcement, archive validation, hash validation, activation validation, and rollback behavior remain unchanged.

## Preference Persistence Design

Use the existing user-owned and updater-preserved `config.txt`; do not add registry or machine-wide settings.

Add two keys:

- `stack-output-extension`: accepted values are `tiff` and `jpg`; missing or invalid values use `tiff`.
- `show-scalebar`: serialized as `true` or `false`; missing or invalid values use `false`.

Startup loads the validated values into the existing runtime state. Choosing a final stack format or accepting Image Settings updates runtime state immediately, and the existing configuration write points persist it. Cancelled dialogs must not change the saved preference.

The scale-bar preference does not bypass calibration safety. If the saved value is enabled but the active camera/lens lacks a valid scale, existing image-save safeguards continue to refuse the scale bar and notify the user. Lens-specific calibration remains in `lenses.json`.

Raw focus-stack frames continue to use TIFF. Only the final stacked or no-stack output follows `stack-output-extension`.

## Compatibility and Failure Handling

- Existing installations without the new keys retain current safe defaults.
- Existing user configuration remains preserved by update and Repair.
- Invalid values are ignored without preventing ENIMAS startup.
- A partial or corrupt protected control directory is rejected with the existing durable installer diagnostics.
- No new dependency is introduced.

## Test Design

Installer regression tests must exercise real staging code and cover:

- control-only activation followed by same-release full Repair;
- all protected payloads present and verified after promotion;
- a repeated full Repair remaining idempotent;
- retry after an interrupted payload promotion;
- rejection of mismatched or corrupted existing control state;
- no weakening of pointer activation or protected path validation.

Preference tests must cover:

- loading old configuration without either key;
- valid and invalid format values;
- valid and invalid scale-bar booleans;
- write/read restart round-trips for TIFF, JPEG, enabled, and disabled;
- dialog acceptance changing runtime values while cancellation leaves them unchanged;
- raw frames remaining TIFF when final output is JPEG.

The focused suites, full relevant updater/camera/UI suites, Python compilation, batch/CRLF checks, `git diff --check`, and release health checks must pass before the changes are described as production-ready.

## Release Boundary

Implementation and verification occur without committing, changing the ENIMAS version, editing `update_manifest.json`, tagging, or pushing. After independent review and full validation, work stops for explicit user confirmation. A later release must use a new immutable tag and regenerate the manifest from committed Git blobs.
