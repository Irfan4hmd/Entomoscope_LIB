"""PyQt integration for the ENIMAS stable-channel updater."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtCore import QUrl
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

import constants
from Tools.enimas_updater import (
    DEFAULT_MANIFEST_URL,
    CancelledError,
    ManifestError,
    RepairRequiredError,
    UpdatePaths,
    atomic_write_json,
    fetch_manifest,
    is_newer_version,
    parse_version,
    prepare_update,
    read_json,
)


def request_orderly_application_exit(window, exit_code: int = 42) -> None:
    """Run the normal window close lifecycle before the updater waits on us."""
    if not window.close():
        raise RuntimeError("ENIMAS did not accept the update shutdown request")
    QApplication.exit(exit_code)


class CheckWorker(QThread):
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, manifest_url: str, parent=None):
        super().__init__(parent)
        self.manifest_url = manifest_url

    def run(self):
        try:
            self.succeeded.emit(fetch_manifest(self.manifest_url))
        except Exception as exc:
            self.failed.emit(str(exc))


class PrepareWorker(QThread):
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    permission_required = pyqtSignal(str)
    repair_required = pyqtSignal(str)
    progress = pyqtSignal(str, int, int, str)

    def __init__(self, manifest: dict, install_root: Path, parent=None):
        super().__init__(parent)
        self.manifest = manifest
        self.install_root = install_root
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            plan = prepare_update(
                self.manifest,
                self.install_root,
                progress=lambda phase, current, total, message: self.progress.emit(
                    phase, current, total, message
                ),
                cancel_event=self.cancel_event,
            )
            self.succeeded.emit(str(plan))
        except CancelledError:
            self.cancelled.emit()
        except PermissionError as exc:
            self.permission_required.emit(str(exc))
        except RepairRequiredError as exc:
            self.repair_required.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class UpdateDialog(QDialog):
    install_requested = pyqtSignal()

    def __init__(self, parent, manifest: dict):
        super().__init__(parent)
        self.setWindowTitle("ENIMAS Update Available")
        self.setMinimumSize(620, 430)
        layout = QVBoxLayout(self)
        title = QLabel(
            f"<b>ENIMAS {manifest['version']} is available</b><br>"
            f"Installed: {constants.APP_VERSION} &nbsp;&nbsp; Released: {manifest['release_date']}"
        )
        layout.addWidget(title)
        notes = QTextBrowser(self)
        notes.setPlainText(manifest.get("release_notes") or "No release notes were provided.")
        layout.addWidget(notes, 1)
        buttons = QDialogButtonBox(self)
        install = buttons.addButton("Download and install", QDialogButtonBox.AcceptRole)
        later = buttons.addButton("Later", QDialogButtonBox.RejectRole)
        install.clicked.connect(self.install_requested.emit)
        install.clicked.connect(self.accept)
        later.clicked.connect(self.reject)
        layout.addWidget(buttons)


class UpdateController(QObject):
    """Own update checks, user interaction, and handoff to the external updater."""

    def __init__(
        self,
        main_window,
        install_root: Path | None = None,
        manifest_url: str = DEFAULT_MANIFEST_URL,
    ):
        super().__init__(main_window)
        self.window = main_window
        source_root = Path(__file__).resolve().parents[1]
        detected_root = source_root.parent if source_root.name.lower() == "src" else source_root
        self.install_root = (install_root or detected_root).resolve()
        self.paths = UpdatePaths(self.install_root)
        self.manifest_url = manifest_url
        self.manifest = None
        self.check_worker = None
        self.prepare_worker = None
        self.progress_dialog = None
        self._manual_check = False

        self.check_action = QAction("Check for Updates...", self.window)
        self.check_action.triggered.connect(lambda: self.check(manual=True))
        self.window.help_menu.addSeparator()
        self.window.help_menu.addAction(self.check_action)

        self.banner = QPushButton("", self.window)
        self.banner.setStyleSheet(
            "QPushButton { background-color:#ffbf00; border:2px solid #6b3d00; "
            "border-radius:6px; padding:6px 12px; color:#1f1300; "
            "font-weight:700; }"
            "QPushButton:hover { background-color:#ffd65a; }"
            "QPushButton:pressed { background-color:#e6a900; }"
        )
        self.banner.setToolTip(
            "A verified ENIMAS update is ready. Click to review the changes."
        )
        self.banner.setMinimumHeight(30)
        self.banner.clicked.connect(self.show_update_dialog)
        self.banner.hide()
        self.window.statusBar.addPermanentWidget(self.banner)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_workers)

    def _shutdown_workers(self):
        if self.prepare_worker and self.prepare_worker.isRunning():
            self.prepare_worker.request_cancel()
            self.prepare_worker.wait()
        if self.check_worker and self.check_worker.isRunning():
            self.check_worker.wait()

    def start(self):
        self.report_previous_result()
        QTimer.singleShot(1500, lambda: self.check(manual=False))

    def check(self, manual: bool):
        if self.prepare_worker and self.prepare_worker.isRunning():
            if manual:
                self.window.statusBar.showMessage("An ENIMAS update is already being prepared.", 3000)
            return
        if self.check_worker and self.check_worker.isRunning():
            if manual:
                self.window.statusBar.showMessage("Already checking for ENIMAS updates...", 3000)
            return
        self._manual_check = manual
        self.check_action.setEnabled(False)
        if manual:
            self.window.statusBar.showMessage("Checking for ENIMAS updates...")
        self.check_worker = CheckWorker(self.manifest_url, self)
        self.check_worker.succeeded.connect(self._check_succeeded)
        self.check_worker.failed.connect(self._check_failed)
        self.check_worker.start()

    def _check_succeeded(self, manifest: dict):
        self.check_action.setEnabled(True)
        self.window.statusBar.clearMessage()
        self.manifest = manifest
        try:
            available = is_newer_version(manifest["version"], constants.APP_VERSION)
        except ManifestError as exc:
            self._check_failed(str(exc))
            return
        if available:
            self.banner.setText(f"UPDATE AVAILABLE: ENIMAS {manifest['version']}")
            self.banner.show()
            if self._manual_check:
                self.show_update_dialog()
        else:
            self.banner.hide()
            if self._manual_check:
                if parse_version(constants.APP_VERSION) > parse_version(manifest["version"]):
                    message = (
                        f"Installed ENIMAS {constants.APP_VERSION} is newer than stable "
                        f"{manifest['version']}. No files were changed."
                    )
                else:
                    message = f"ENIMAS {constants.APP_VERSION} is up to date."
                QMessageBox.information(
                    self.window,
                    "ENIMAS Update",
                    message,
                )

    def _check_failed(self, message: str):
        self.check_action.setEnabled(True)
        self.window.statusBar.clearMessage()
        if self._manual_check:
            QMessageBox.warning(
                self.window,
                "Could Not Check for Updates",
                f"ENIMAS could not contact the stable update service.\n\n{message}\n\n"
                "Your installed application was not changed.",
            )

    def show_update_dialog(self):
        if not self.manifest:
            self.check(manual=True)
            return
        dialog = UpdateDialog(self.window, self.manifest)
        dialog.install_requested.connect(self.prepare_selected_update)
        dialog.exec_()

    def prepare_selected_update(self):
        if not self.manifest:
            return
        if self.prepare_worker and self.prepare_worker.isRunning():
            return
        if self.manifest.get("system_update_required"):
            QMessageBox.information(
                self.window,
                "Administrator Maintenance Required",
                "This release changes system components or camera drivers. Run install.bat --repair "
                "normally; it will request administrator approval only for protected maintenance. "
                "Your user data will be preserved.",
            )
            return
        self.check_action.setEnabled(False)
        self.banner.setEnabled(False)
        self.progress_dialog = QProgressDialog(
            "Preparing ENIMAS update...", "Cancel", 0, 100, self.window
        )
        self.progress_dialog.setWindowTitle("ENIMAS Update")
        self.progress_dialog.setAutoClose(False)
        self.progress_dialog.setAutoReset(False)
        self.prepare_worker = PrepareWorker(self.manifest, self.install_root, self)
        self.prepare_worker.progress.connect(self._update_progress)
        self.prepare_worker.succeeded.connect(self._prepared)
        self.prepare_worker.failed.connect(self._prepare_failed)
        self.prepare_worker.cancelled.connect(self._prepare_cancelled)
        self.prepare_worker.permission_required.connect(self._permission_required)
        self.prepare_worker.repair_required.connect(self._repair_required)
        self.progress_dialog.canceled.connect(self.prepare_worker.request_cancel)
        self.progress_dialog.show()
        self.prepare_worker.start()

    def _update_progress(self, phase: str, current: int, total: int, message: str):
        if not self.progress_dialog:
            return
        if phase == "dependencies":
            # pip subprocesses cannot be cancelled atomically. Application
            # files are still untouched while dependency wheels are staged.
            self.progress_dialog.setCancelButton(None)
        percent = int(current * 100 / total) if total else 0
        self.progress_dialog.setValue(max(0, min(100, percent)))
        labels = {
            "files": "Preparing changed files",
            "downloading": "Downloading",
            "verifying": "Verifying",
            "dependencies": "Preparing dependencies",
        }
        self.progress_dialog.setLabelText(f"{labels.get(phase, phase.title())}: {message}")

    def _prepared(self, plan_path: str):
        if self.progress_dialog:
            self.progress_dialog.setValue(100)
            self.progress_dialog.close()
        answer = QMessageBox.question(
            self.window,
            "Update Ready",
            "The update is downloaded and verified. ENIMAS will close, apply the update, "
            "validate it, and restart automatically. If anything fails, the previous version "
            "will be restored.\n\nApply the update now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            self.check_action.setEnabled(True)
            self.banner.setEnabled(True)
            self.window.statusBar.showMessage("Update is ready and can be applied later.", 5000)
            return
        plan = read_json(Path(plan_path), {}) or {}
        updater = Path(plan.get("external_updater", ""))
        if not updater.is_file():
            self._prepare_failed("The verified external updater is missing.")
            return
        base_python = Path(getattr(sys, "_base_executable", None) or sys.executable)
        command = [
            str(base_python),
            str(updater),
            "--install-root",
            str(self.install_root),
            "apply",
            "--plan",
            str(plan_path),
            "--wait-pid",
            str(os.getpid()),
            "--restart",
        ]
        try:
            subprocess.Popen(
                command,
                cwd=self.install_root,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                close_fds=True,
            )
        except OSError as exc:
            self._prepare_failed(f"Could not start the external updater: {exc}")
            return
        self.banner.hide()
        request_orderly_application_exit(self.window)

    def _prepare_failed(self, message: str):
        self.check_action.setEnabled(True)
        self.banner.setEnabled(True)
        if self.progress_dialog:
            self.progress_dialog.close()
        QMessageBox.warning(
            self.window,
            "Update Preparation Failed",
            f"{message}\n\nThe installed ENIMAS application was not changed.",
        )

    def _prepare_cancelled(self):
        self.check_action.setEnabled(True)
        self.banner.setEnabled(True)
        if self.progress_dialog:
            self.progress_dialog.close()
        self.window.statusBar.showMessage("ENIMAS update download cancelled; it can be resumed later.", 5000)

    def _permission_required(self, message: str):
        if self.progress_dialog:
            self.progress_dialog.close()
        self.check_action.setEnabled(True)
        self.banner.setEnabled(True)
        answer = QMessageBox.question(
            self.window,
            "Administrator Permission Required",
            f"ENIMAS cannot write to the installation folder.\n\n{message}\n\n"
                "Repair ENIMAS folder access now? No application code, drivers, or virtual "
                "environment will run with administrator rights.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        program_data = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        installer = program_data / "ENIMAS" / "admin" / "install.bat"
        if not installer.is_file():
            self._prepare_failed(
                "The protected update entrypoint is missing. Download the stable installer "
                "and run install.bat --repair."
            )
            return
        try:
            import ctypes

            result = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                str(installer),
                "--admin-grant-access",
                str(self.install_root),
                1,
            )
            if result <= 32:
                raise OSError(f"Windows elevation failed with code {result}")
        except Exception as exc:
            self._prepare_failed(f"Could not start the elevated updater: {exc}")
            return
        self.window.statusBar.showMessage(
            "Windows is repairing ENIMAS folder access. Try the update again after it finishes.",
            10000,
        )

    def _repair_required(self, message: str):
        if self.progress_dialog:
            self.progress_dialog.close()
        self.check_action.setEnabled(True)
        self.banner.setEnabled(True)
        QMessageBox.warning(
            self.window,
            "ENIMAS Repair Required",
            f"{message}\n\nDownload the stable install.bat and run it normally. "
            "Choose Repair only when prompted; configuration, lenses, models, plugins, "
            "outputs, and the virtual environment will be preserved where compatible.",
        )

    def report_previous_result(self):
        result = read_json(self.paths.result)
        if not result or result.get("reported"):
            return
        status = result.get("status")
        if status == "success":
            if result.get("fresh_install"):
                text = f"ENIMAS {result.get('version', constants.APP_VERSION)} was installed successfully."
            else:
                text = f"ENIMAS was updated successfully to {result.get('version', constants.APP_VERSION)}."
            icon = QMessageBox.Information
        elif status == "support_rollback":
            text = f"ENIMAS was restored to known-good version {result.get('version', constants.APP_VERSION)}."
            icon = QMessageBox.Information
        else:
            text = (
                "The previous ENIMAS version was restored after an interrupted or failed update.\n\n"
                f"Details: {result.get('error', 'See the update log.')}"
            )
            icon = QMessageBox.Warning
        message = QMessageBox(icon, "ENIMAS Update", text, QMessageBox.Ok, self.window)
        if result.get("log"):
            message.setDetailedText(f"Update log: {result['log']}")
            open_log = message.addButton("Open update log", QMessageBox.ActionRole)
            open_log.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(result["log"])))
            )
        message.exec_()
        result["reported"] = True
        atomic_write_json(self.paths.result, result)
