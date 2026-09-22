"""ENIMAS plugin for collecting final stacked images for transfer."""

from __future__ import annotations

from pathlib import Path
from threading import Event

from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from PluginBase import PluginBase
from Tools.stack_export import (
    StackExportError,
    discover_stacked_images,
    export_stacked_images,
)


def scan_exclusion_root(source: str, destination: str) -> str:
    """Exclude a transfer directory only when it is strictly inside source."""
    if not source or not destination:
        return ""
    try:
        source_path = Path(source).resolve()
        destination_path = Path(destination).resolve()
        relative = destination_path.relative_to(source_path)
        return str(destination_path) if relative.parts else ""
    except (OSError, ValueError):
        return ""


class ScanWorker(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, source, recursive, excluded):
        super().__init__()
        self.source = source
        self.recursive = recursive
        self.excluded = excluded

    def run(self):
        try:
            images = discover_stacked_images(
                self.source,
                recursive=self.recursive,
                exclude_root=self.excluded or None,
            )
            self.completed.emit(images)
        except Exception as exc:
            self.failed.emit(str(exc))


class ExportWorker(QThread):
    progress = pyqtSignal(int, int, str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, images, destination, output_format, site_id):
        super().__init__()
        self.images = list(images)
        self.destination = destination
        self.output_format = output_format
        self.site_id = site_id
        self._cancelled = Event()

    def cancel(self):
        self._cancelled.set()

    def run(self):
        try:
            summary = export_stacked_images(
                self.images,
                self.destination,
                output_format=self.output_format,
                site_id=self.site_id,
                progress=lambda current, total, name: self.progress.emit(
                    current, total, name
                ),
                cancelled=self._cancelled.is_set,
            )
            self.completed.emit(summary)
        except Exception as exc:
            self.failed.emit(str(exc))


class ExportDialog(QDialog):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def closeEvent(self, event):
        if self.plugin.worker and self.plugin.worker.isRunning():
            if not hasattr(self.plugin.worker, "cancel"):
                QMessageBox.information(
                    self,
                    "Scan in progress",
                    "Please wait for the folder scan to finish before closing.",
                )
                event.ignore()
                return
            answer = QMessageBox.question(
                self,
                "Export in progress",
                "Cancel the current operation and close this window?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            if hasattr(self.plugin.worker, "cancel"):
                self.plugin.worker.cancel()
            self.plugin.close_when_finished = True
            self.setEnabled(False)
            event.ignore()
            return
        event.accept()


class Plugin(PluginBase):
    def __init__(self):
        super().__init__()
        self._name = "Stacked Image Export"
        self.images = []
        self.worker = None
        self.busy = False
        self.close_when_finished = False

    def run(self, data: dict) -> dict:
        if self.window is not None and self.window.isVisible():
            self.window.raise_()
            self.window.activateWindow()
            return {}

        self.images = []
        self.worker = None
        self.busy = False
        self.close_when_finished = False
        self.window = ExportDialog(self)
        self.window.setWindowTitle("ENIMAS - Stacked Image Export")
        self.window.setMinimumWidth(720)
        self._build_ui()
        self.window.show()
        return {}

    def _build_ui(self):
        layout = QVBoxLayout(self.window)
        explanation = QLabel(
            "Collect final ENIMAS stacked images into one transfer folder. "
            "Raw Stack_Frames folders and post-processing variants are excluded."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        form = QFormLayout()
        self.source_edit = QLineEdit()
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_edit, 1)
        source_browse = QPushButton("Browse...")
        source_row.addWidget(source_browse)
        form.addRow("Source folder:", source_row)

        self.recursive_check = QCheckBox("Include subfolders")
        self.recursive_check.setChecked(True)
        form.addRow("Scan:", self.recursive_check)

        scan_row = QHBoxLayout()
        self.scan_button = QPushButton("Scan final stacks")
        self.found_label = QLabel("Not scanned")
        scan_row.addWidget(self.scan_button)
        scan_row.addWidget(self.found_label, 1)
        form.addRow("Found:", scan_row)

        self.destination_edit = QLineEdit()
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination_edit, 1)
        destination_browse = QPushButton("Browse...")
        destination_row.addWidget(destination_browse)
        form.addRow("Transfer folder:", destination_row)

        self.site_edit = QLineEdit()
        self.site_edit.setPlaceholderText("Optional, e.g. NatureSchool_07")
        self.site_edit.setToolTip(
            "Added to every exported filename to identify the sending centre."
        )
        form.addRow("Centre/site ID:", self.site_edit)

        self.format_combo = QComboBox()
        self.format_combo.addItem("Keep original TIFF/JPEG format", "keep")
        self.format_combo.addItem("Convert exported copies to JPEG (quality 95)", "jpeg")
        form.addRow("Export format:", self.format_combo)
        layout.addLayout(form)

        note = QLabel(
            "Existing files are never overwritten. Identical files are skipped; "
            "different files with the same name receive a numbered suffix. A CSV "
            "manifest records every export."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status = QLabel("Choose a source folder, then scan.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.export_button = QPushButton("Export")
        self.export_button.setEnabled(False)
        close_button = QPushButton("Close")
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.export_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        source_browse.clicked.connect(self._browse_source)
        destination_browse.clicked.connect(self._browse_destination)
        self.scan_button.clicked.connect(self._scan)
        self.export_button.clicked.connect(self._export)
        self.cancel_button.clicked.connect(self._cancel)
        close_button.clicked.connect(self.window.close)
        self.source_edit.textChanged.connect(self._clear_scan)
        self.recursive_check.toggled.connect(self._clear_scan)
        self.destination_edit.textChanged.connect(self._update_export_enabled)

    def _browse_source(self):
        folder = QFileDialog.getExistingDirectory(
            self.window, "Choose ENIMAS image folder", self.source_edit.text()
        )
        if folder:
            self.source_edit.setText(folder)

    def _browse_destination(self):
        folder = QFileDialog.getExistingDirectory(
            self.window, "Choose transfer folder", self.destination_edit.text()
        )
        if folder:
            self.destination_edit.setText(folder)

    def _clear_scan(self):
        if self.busy:
            return
        self.images = []
        self.found_label.setText("Not scanned")
        self.export_button.setEnabled(False)

    def _set_busy(self, busy, scanning=False):
        self.busy = busy
        self.scan_button.setEnabled(not busy)
        self.export_button.setEnabled(not busy and bool(self.images))
        self.cancel_button.setEnabled(busy and not scanning)
        self.source_edit.setEnabled(not busy)
        self.destination_edit.setEnabled(not busy)
        self.recursive_check.setEnabled(not busy)
        self.format_combo.setEnabled(not busy)
        self.site_edit.setEnabled(not busy)
        if busy and scanning:
            self.progress.setRange(0, 0)
        elif not busy:
            self.progress.setRange(0, 100)

    def _scan(self):
        source = self.source_edit.text().strip()
        if not source:
            QMessageBox.warning(self.window, "Source required", "Choose a source folder first.")
            return
        destination = self.destination_edit.text().strip()
        excluded = scan_exclusion_root(source, destination)
        self._set_busy(True, scanning=True)
        self.status.setText("Scanning for final stacked images...")
        self.worker = ScanWorker(source, self.recursive_check.isChecked(), excluded)
        self.worker.completed.connect(self._scan_complete)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _scan_complete(self, images):
        self.images = list(images)
        self.found_label.setText(f"{len(self.images)} final stacked image(s)")
        self.status.setText(
            "Scan complete. Choose a transfer folder and export."
            if self.images
            else "No ENIMAS final stacked images were found."
        )

    def _update_export_enabled(self):
        busy = self.busy
        destination = self.destination_edit.text().strip()
        source = self.source_edit.text().strip()
        same_folder = False
        try:
            same_folder = bool(source and destination) and Path(source).resolve() == Path(destination).resolve()
        except OSError:
            pass
        self.export_button.setEnabled(
            bool(self.images) and bool(destination) and not busy and not same_folder
        )
        if same_folder:
            self.status.setText("The transfer folder must be different from the source folder.")

    def _export(self):
        destination = self.destination_edit.text().strip()
        if not destination:
            QMessageBox.warning(
                self.window, "Transfer folder required", "Choose a transfer folder first."
            )
            return
        try:
            if Path(destination).resolve() == Path(self.source_edit.text()).resolve():
                raise StackExportError(
                    "The transfer folder must be different from the source folder."
                )
        except OSError as exc:
            QMessageBox.warning(self.window, "Invalid folder", str(exc))
            return

        self.progress.setValue(0)
        self._set_busy(True)
        self.status.setText("Preparing export...")
        self.worker = ExportWorker(
            self.images,
            destination,
            self.format_combo.currentData(),
            self.site_edit.text(),
        )
        self.worker.progress.connect(self._progress)
        self.worker.completed.connect(self._export_complete)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _progress(self, current, total, name):
        self.progress.setValue(round(100 * current / max(1, total)))
        self.status.setText(f"Exporting {current} of {total}: {name}")

    def _cancel(self):
        if self.worker and hasattr(self.worker, "cancel"):
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText("Cancellation requested; finishing the current file safely...")

    def _export_complete(self, summary):
        self.progress.setValue(100 if not summary.cancelled else self.progress.value())
        message = (
            f"Exported: {summary.exported}; already present: {summary.skipped}; "
            f"failed: {summary.failed}."
        )
        if summary.cancelled:
            message = "Export cancelled safely. " + message
        if summary.manifest_path:
            message += f" Manifest: {summary.manifest_path}"
        self.status.setText(message)
        if not summary.cancelled:
            QMessageBox.information(self.window, "Export complete", message)

    def _failed(self, message):
        self.status.setText(f"Operation failed: {message}")
        QMessageBox.warning(self.window, "Stacked image export", message)

    def _worker_finished(self):
        completed_worker = self.worker
        self.worker = None
        self._set_busy(False)
        self._update_export_enabled()
        self._finish_close_if_requested()
        if completed_worker is not None:
            completed_worker.deleteLater()

    def _finish_close_if_requested(self):
        if not self.close_when_finished or not self.window:
            return
        if self.worker and self.worker.isRunning():
            return
        self.close_when_finished = False
        self.window.setEnabled(True)
        self.window.close()
