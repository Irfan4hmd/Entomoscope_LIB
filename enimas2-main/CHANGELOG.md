# ENIMAS Changelog

All notable changes to ENIMAS will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/).

## [1.4.11] - 2026-08-12

### Fixed
- Retry after an existing-installation update failure now rechecks the updater normally instead of reusing the menu choice exit code and immediately returning to the failure menu
- Elevated maintenance carries its one-time connection and operation parameters in the encoded child command, so approving UAC with a separate administrator account no longer depends on inheriting ENIMAS-specific environment variables
- Windows users with an exact Microsoft Entra SID shape (`S-1-12-1-a-b-c-d`) are accepted when their local Windows profile exists; existing local and domain SID handling is unchanged
- A failed hash-pinned dependency installation now retains the pip exit code in rollback diagnostics

### Safety
- Elevated targets, internal operations, caller SIDs, named-pipe access restrictions, timeouts, and protected installer validation remain enforced; encoding transports the parameters but does not replace those trust checks
- Camera application code, Python, dependencies, camera-driver binaries, SDK binaries, motor firmware, virtual environments, lenses, models, plugins, outputs, and existing user data are unchanged

## [1.4.10] - 2026-08-12

### Fixed
- The original installer now remains synchronized with elevated maintenance through a one-use private completion channel, preventing a launched archive download from being misreported as cancelled and then abandoned in the `downloading` phase
- Elevated maintenance explicitly reports when it has started and returns its final numeric result to the original window, including when Windows cannot provide a reliable elevated process handle

### Safety
- The random one-client completion channel permits only the invoking user and local administrators, supports approval with a separate administrator account, creates no privileged status file in a user-writable directory, and times out if maintenance does not finish
- Camera application code, Python, dependencies, camera-driver binaries, SDK binaries, motor firmware, virtual environments, lenses, models, plugins, outputs, and existing user data are unchanged

## [1.4.9] - 2026-08-12

### Fixed
- Administrator maintenance now elevates the trusted Windows command processor and passes the downloaded or protected installer path explicitly, avoiding failures after an approved UAC prompt when Windows cannot elevate a protected batch file through its file association
- Elevation-launch failures now display the underlying Windows error, while a genuine UAC cancellation still returns exit code 1223

### Safety
- The elevated installer still receives only the validated internal maintenance operation and installation-owner SID, waits synchronously, and returns the maintenance process exit code unchanged
- Camera application code, Python, dependencies, camera-driver binaries, SDK binaries, motor firmware, virtual environments, lenses, models, plugins, outputs, and existing user data are unchanged

## [1.4.8] - 2026-08-12

### Fixed
- Fresh installation and Repair now detect and reuse a complete compatible Galaxy SDK instead of reopening its vendor installer and asking users to uninstall it first
- Cancelling or failing the one-time administrator prompt now stops immediately with exit code 1223 instead of falsely reporting that protected security preparation completed

### Safety
- An incomplete Galaxy SDK is still rejected and repaired through the verified bundled installer; existing driver acceptance rules and driver binaries are unchanged
- Python, dependencies, camera-driver binaries, SDK binaries, motor firmware, virtual environments, lenses, models, plugins, outputs, and existing user data are unchanged

## [1.4.7] - 2026-08-12

### Fixed
- Fresh installation and Repair can now promote an already active control-only protected installer for the same immutable release to the complete verified camera-driver and Galaxy SDK payload instead of failing on `camera-driver/README.md`
- Interrupted first-time protected staging can be retried safely, while unreadable, malformed, dangling, linked, or reparse-point protected state is rejected before trusted files or pointers are changed
- The final stacked-image JPEG/TIFF choice and the scale-bar setting are now preserved in the existing user configuration and restored when ENIMAS starts again

### Safety
- Same-release promotion validates the stored manifest and control files, verifies every copied system payload by size and SHA-256, writes the complete manifest last, and activates it only after full validation
- Raw focus-stack frames remain TIFF, and a saved scale-bar preference still requires valid active-lens calibration before a scale bar can be applied
- Python, dependencies, camera-driver binaries, SDK binaries, motor firmware, virtual environments, lenses, models, plugins, outputs, and existing user data are unchanged

## [1.4.6] - 2026-08-11

### Fixed
- Repair failures now remain visible in the original installer window and identify the failed step, installer revision, and diagnostic log without removing existing files or user data
- Elevated system-maintenance failures, including protected payload preparation, driver installation, SDK installation, permissions, and UAC launch failures, are recorded in `updates/update.log` with safe fallback logs when the installation folder is unavailable
- The Repair failure screen now reports the log written for the current failure instead of selecting an older log merely because it already exists
- Installer-owner validation failures now use the visible failure screen and pause instead of closing immediately on unusual institutional or domain-account configurations

### Improved
- The installer now displays an independent revision and the official download URL so support can immediately identify an outdated `install.bat`

### Safety
- This release changes only installer diagnostics and protected maintenance logging; normal updates do not reinstall Python, dependencies, camera drivers, motor firmware, user data, or the virtual environment

## [1.4.5] - 2026-08-10

### Fixed
- Slow but responsive VAImaging and Arducam streams now receive independent bounded time for preview delivery, in-flight preview-lock drainage, and each required direct SDK read, preventing false fresh-frame timeouts during stacking
- ENIMAS now keeps exactly one preview worker and one camera lock for the connected camera instead of starting another camera-reading thread during UI or device-state refreshes
- Single-image capture now requires a newly delivered frame, uses the coordinated direct-camera fallback if preview delivery stalls, and never saves an older cached preview
- VAImaging single capture now handles a missing camera frame before color conversion instead of raising an OpenCV error
- Camera SDK timeouts during pre-stack movement or frame capture now stop the stack with a warning instead of closing ENIMAS
- Autofocus camera timeouts and SDK errors now stop autofocus with a warning instead of closing ENIMAS
- Autofocus completion and error dialogs are now routed through Qt signals so GUI controls are updated only on the main application thread
- Closing ENIMAS now cancels and waits for autofocus, saved-focus movement, and single-image capture before closing the motor or camera connections
- Saved lens-focus movement is tracked, blocks image capture while the stage is moving, and performs all widget updates through main-thread Qt signals
- Manual Up/Down focus movement is now tracked to completion, preventing capture, stacking, lens changes, or hardware shutdown while the stage is still moving
- Lens selection and camera settings are blocked throughout autofocus and stacking so they cannot change camera or motor state during an active image operation
- Final shutdown now ignores already-queued camera and worker UI callbacks, preventing late dialogs, control restoration, or image saving during hardware teardown
- Full Repair now reinstalls and verifies the Galaxy camera SDK and refreshes its runtime environment before validating ENIMAS, including on older installations with a missing `GALAXY_GENICAM_ROOT`

### Improved
- Autofocus allows a longer per-frame camera response window for computers and cameras with slower delivery
- Single-image fresh-frame retries now run in a background worker, keeping the ENIMAS window responsive while a slow camera is retried
- Autofocus retains its explicit Cancel workflow; manual Up/Down controls return only after the autofocus worker has safely released the camera and stage
- Stack/autofocus warnings now tell the user which support logs to send, and unexpected autofocus errors retain a full traceback in `debug.log`

### Safety
- Both camera types still require the configured number of post-movement frames before saving a stack image; a stale preview frame whose delivery counter has not advanced is never accepted
- Single-image capture is disabled and rejected while autofocus, stack capture, saved-focus movement, or manual focus movement is active, preventing camera reads while another camera or stage operation is running
- Stack and autofocus failures remain bounded and cancellable, preserve partial stack frames for diagnosis, and keep the application available for manual focusing or retry
- The normal incremental update does not change Python, dependencies, camera drivers, motor firmware, user data, or the virtual environment

## [1.4.4] - 2026-07-24

### Fixed
- Focus-stack capture no longer repeats preview frames already discarded when switching to direct-camera fallback, preventing false capture timeouts on slower VAImaging and Arducam streams
- A fresh preview frame published at the timeout boundary is now accepted instead of unnecessarily starting a fallback capture

### Safety
- Both camera types still require the configured number of post-movement frames before saving a stack image; cached preview frames are never accepted
- If no preview frames arrive, the direct fallback still performs the full buffer-discard sequence
- Python, dependencies, camera drivers, motor firmware, user data, and the virtual environment are unchanged

## [1.4.3] - 2026-07-22

### Fixed
- Focus-stack capture now obtains post-movement frames from the live preview without competing with the preview thread for the camera SDK lock
- If preview delivery stalls, ENIMAS pauses it before a bounded direct-camera fallback, preventing false "camera did not return a fresh frame" failures
- Preview and direct frames are published in acquisition order so an older in-flight preview cannot overwrite a newer stack frame
- Arducam autofocus bypasses preview waiting while its preview is intentionally paused

### Improved
- Fresh-frame failures now distinguish cancellation, lock timeout, capture timeout, driver exception, and no-frame responses in the update log
- The one-time Windows administrator prompt for older installations now clearly states that the update is active and does not reinstall dependencies, camera drivers, or the virtual environment

### Safety
- Preview waiting, camera-lock acquisition, and direct fallback share one total timeout, with cancellation checked during lock contention
- Stack failure and cancellation continue to preserve partial raw frames for diagnosis, suppress incomplete composite images, and restore only the stage distance actually moved
- Python, dependencies, camera drivers, motor firmware, user data, and the virtual environment are unchanged

## [1.4.2] - 2026-07-21

### Fixed
- In-app updates now complete when an ENIMAS background process remains after the window closes, instead of leaving the old version installed and repeatedly offering the same update
- Windows process checks now distinguish a running process from one that has already exited

### Improved
- The bottom-right update banner now uses a larger, bold, high-contrast amber design with explicit UPDATE AVAILABLE text

### Safety
- Shutdown recovery is limited to the exact PID and Windows process-creation marker recorded by the running ENIMAS instance, and only after the user accepts an in-app update
- Manually launched installer updates still ask the user to close ENIMAS and never force-close it
- Python, dependencies, camera drivers, motor firmware, user data, and the virtual environment are unchanged

## [1.4.1] - 2026-07-21

### Highlights
- Focus-stack capture now requests and saves a fresh camera frame after every stage movement, addressing repeated raw frames and blurry composite images
- Final Helicon Focus and built-in stack outputs can be saved as TIFF or JPEG per session while raw stack frames remain TIFF
- New Stacked Image Export plugin collects final stack images for transfer, supports optional site identifiers and JPEG conversion, prevents overwrites, and records a CSV manifest
- Scale bars now use validated camera/lens calibration, adaptive physical lengths, clear millimetre labels, and guided calibration under Help > Scale Bar Setup

### Reliability
- Built-in focus stacking now applies calculated alignment to colour frames before fusion
- Stack cancellation, capture failures, application closing, and output numbering are handled safely without overwriting surviving results
- Missing calibration no longer falls back to an inaccurate physical scale, and unsupported high-bit-depth images are left unchanged

### Licensing
- Software remains MIT-licensed; CAD, STL, BOM, and other hardware design files are explicitly covered by CERN-OHL-P-2.0

### Validation
- Successfully installed as an incremental update from ENIMAS 1.3.1 and validated on Entomoscope hardware
- Python, dependencies, camera drivers, motor firmware, user data, and the virtual environment are unchanged

## [1.4.0] - 2026-07-21

### Added
- Per-session TIFF or JPEG selection for final Helicon and built-in stack outputs while retaining TIFF raw stack frames
- Stacked Image Export plugin for locating final stacks, collecting them into a transfer folder, adding optional site identifiers, converting copies to JPEG, and recording a CSV manifest
- Guided camera/lens scale calibration with a known-distance calculator and Help > Scale Bar Setup instructions

### Changed
- Scale bars now use validated camera/lens calibration, adaptive 1/2/5 physical lengths, resolution-aware styling, and clear millimetre labels
- Built-in focus stacking now applies its calculated alignment to the colour frames before fusion
- Helicon Focus is launched with argument-safe paths and verified TIFF/JPEG output handling

### Fixed
- Stack capture now requests and saves fresh post-movement camera frames instead of reusing cached preview frames
- Cancellation, capture failure, application closing, and stack numbering no longer leave unsafe motor state, stale operations, or overwrite surviving final images
- Invalid or missing calibration no longer falls back to a scientifically incorrect 1 mm/pixel value for measurements or scale bars
- Scale-bar annotation refuses unsupported high-bit-depth images without modifying the original
- Export safely handles duplicates, name collisions, disappearing inputs, cancellation, and Windows path-length limits

### Documentation and licensing
- Clarified that software is MIT-licensed and CAD, STL, BOM, and other hardware design files are covered by CERN-OHL-P-2.0

### Notes
- This is a branch-only validation candidate until manual Entomoscope testing is accepted and the same release commit is explicitly merged to the stable main branch
- Python, dependencies, camera drivers, motor firmware, and the virtual environment are unchanged

## [1.3.1] - 2026-07-21

### Fixed
- Rollback dependency preparation now resolves CPU-only PyTorch builds from the authenticated PyTorch CPU index
- Existing dependencies are recorded as exact installed versions, so VCS-installed packages can reuse verified release wheels without requiring Git
- Wheel validation now reads only the wheel's top-level distribution metadata and safely ignores metadata bundled inside packages such as setuptools

### Notes
- This patch changes only installation and update recovery behavior; camera, motor, firmware, image capture, and stacking behavior are unchanged

## [1.3.0] - 2026-07-21

### Added
- Incremental, tag-pinned updates that download and verify only changed application files
- Non-blocking update notification, changelog dialog, and Help > Check for Updates command
- Durable transaction journal, automatic interruption recovery, rollback, and support rollback command
- Resumable downloads, SHA-256 validation, update locking, disk/write preflight, and hardware-independent health checks
- Hash-locked dependency downloads and a versioned administrator-protected installer control plane
- Test-covered first-migration support for complete ENIMAS 1.0.7 through 1.2.x installations

### Changed
- Running install.bat on a healthy installation now checks for and applies an incremental update without reinstalling drivers or the virtual environment
- Fresh and explicit repair installations remain administrator-level system-maintenance operations
- User configuration, lenses, measurement settings, custom models and plugins, outputs, and the virtual environment are excluded from normal application updates
- The first legacy 1.0.7-1.2.x update uses one UAC approval to create the protected updater and remove broad folder permissions; later application updates, dependency installation, and health checks run without elevation
- Installations older than 1.0.7, incomplete legacy layouts, and manifest-era installations with a missing or mismatched release record are directed to explicit data-preserving Repair
- The obsolete bundled Git installer and pre-transaction `update.bat` are removed; Git is not required for installation or updates

### Fixed
- Interrupted, corrupt, locked, or failed updates no longer leave a mixed application version
- Repair restores root-level configuration, lens, and environment backups left by an interrupted legacy updater
- Starting ENIMAS during an active update no longer starts a competing application instance
- Repair preserves unmanaged custom-model sidecars as well as model weights
- Dependency-changing updates remove obsolete packages that are absent from the authoritative release lock

## [1.2.4] - 2026-05-05

### Improved
- Batch processing performance: segmentation model is now cached instead of reloaded per image
- GUI remains responsive during batch cropping and background removal
- Reduced unnecessary garbage collection overhead in image processing pipeline

### Fixed
- Auto-gain button crash on VAImaging camera
- Segmenter device selection (cpu/cuda) now correctly tracked across calls

## [1.2.3] - 2026-03-26

### Improved
- Autofocus flow tuned for faster real-world use with shorter fine/verify passes and earlier stopping after the focus peak
- Autofocus abort responsiveness improved so stop requests are handled much faster during movement and capture waits
- Fine search is now slightly biased toward the empirically better side of focus on the current hardware, reducing the need for manual correction after autofocus

## [1.2.2] - 2026-03-25

### Improved
- Focus metric switched from full-frame variance to mean of top-15% absolute Laplacian responses
- Removes reliance on spatial crop: metric works regardless of specimen position or size in the frame
- Background regions (near-zero Laplacian) are naturally excluded, sharpening the focus peak
- Docstring and inline comments kept consistent with the 85th-percentile implementation

## [1.2.1] - 2026-03-25

### Improved
- Autofocus algorithm rewritten with two-phase coarse/fine scanning (3 mm → 0.5 mm step)
- Focus zone entry now requires two consecutive large sharpness rises, reducing false triggers
- Convergence gate requires 4 monotonically declining readings AND an 8% drop below peak
- Fine-phase baseline re-measured with multi-sample averaging to eliminate coarse-scan noise
- Refinement pass added: after peak detection, a ±1.5 mm scan at 0.25 mm steps confirms the exact best position
- Camera failure during the refinement pass now escalates to the standard connection-error dialog
- `calc_focus` extracted into a shared `calc_focus_score()` utility (eliminates duplication between Arducam and VAImaging)
- Focus metric improved: center-50% ROI, 5× downscale (was 10×), Gaussian pre-blur before Laplacian

## [1.2.0] - 2026-03-25

### Changed
- Simplified update process: pressing Update now downloads only install.bat and re-runs it (same as fresh install)
- Removed update.bat — no longer needed
- Update is a clean reinstall: no complex backup/restore logic
- Users can also update manually by running install.bat directly

## [1.1.0] - 2026-03-24

### Fixed
- COM port selection: Bluetooth serial ports are now filtered out automatically, preventing the app from connecting to wrong devices
- Arduino detection now prioritizes ports with known Arduino USB Vendor IDs (Arduino, CH340, FTDI, CP210x)
- Update system now points to the correct ENIMAS2 repository
- Version comparison uses semantic versioning (only prompts when a newer version is available, not on any difference)
- Fixed crash caused by duplicate QApplication creation during update prompt

### Added
- Changelog display in update notification — users can see what's new before updating
- User configuration (settings, paths) is now preserved during updates
- Virtual environment is preserved during updates for faster update process
- Application version is now tracked in code (constants.APP_VERSION) for reliable version sync after updates

### Changed
- Update system rewritten to download from the correct enimas2 repository (main branch)
- install.bat updated with consistent download URLs and folder naming

## [1.0.7] - 2025-01-01

### Notes
- Previous version (legacy ENIMAS update system)
