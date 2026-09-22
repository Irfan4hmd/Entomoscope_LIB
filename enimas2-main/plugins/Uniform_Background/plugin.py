from PluginBase import PluginBase
import os
import glob
import traceback
import pandas as pd

from PyQt5 import QtCore, uic,QtWidgets
from PyQt5.QtGui import QColor
from PyQt5.QtCore import  QObject
from PyQt5.QtWidgets import QComboBox, QCheckBox

from Background_remover.data_util import colors_dict
from Background_remover.background_remover import remove_backgound, remove_background_dataset

color_names = [
    "Default: Gray",
    "Red", "Green", "Blue", "Yellow", "Orange",
    "Purple", "Cyan", "Pink", "Black", "White",
]
devices = [
    "cpu"
]


class BackgroundWorker(QtCore.QThread):
    """Worker thread for batch background removal."""
    progress = QtCore.pyqtSignal(int, int, str)  # current, total, status
    finished = QtCore.pyqtSignal(list)  # results list

    def __init__(self, images, device, rgb, out_path, save_here, color_name):
        super().__init__()
        self.images = images
        self.device = device
        self.rgb = rgb
        self.out_path = out_path
        self.save_here = save_here
        self.color_name = color_name

    def run(self):
        results = []
        total = len(self.images)
        processed = 0

        for img_path in self.images:
            try:
                remove_backgound(img_path, None, self.device, self.rgb, self.out_path, self.save_here, False)
                status = "processed"
                results.append({
                    "image": os.path.basename(img_path),
                    "replace color": self.color_name,
                    "device": self.device,
                    "status": "processed"
                })

            except Exception as e:
                traceback.print_exc()
                results.append({
                    "image": os.path.basename(img_path),
                    "replace color": None,
                    "device": None,
                    "status": f"Error: {e}"
                })
                status = f"Error: {e}"

            processed += 1
            self.progress.emit(processed, total, status)

        self.finished.emit(results)


class Plugin(PluginBase, QObject):
    """
    Base class for all plugins. 
    """

    def __init__(self):
        super().__init__()
        self._name = 'Uniform Background'
        self.window = None
        self.reset_states()
        self._worker = None

    @property
    def name(self):
        return self._name
    
    # def init(self, app_context: dict):
    #     print(f"Initializing {self.name} plugin with context: {app_context}")
    
    def run(self, data: dict) -> dict:
        ui_path = self.get_ui()
        self.window = uic.loadUi(ui_path)
        self.window.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowCloseButtonHint
            | QtCore.Qt.WindowMinimizeButtonHint
        )
        self.window.setFixedSize(self.window.size())
        self.bind_ui()
        self.reset_states()
        self.window.show()
        return {}

    def get_ui(self) -> str:
        return os.path.join(os.path.dirname(__file__), 'plugin.ui')
    
    def reset_states(self):
        self.images = []
        self.results = []
        self.in_path = None
        self.out_path = None
        self.rgb = None
        self.device = "cpu"
        self.save_here = False
    
    def bind_ui(self):
        w = self.window

        w.InputBrowseButton.clicked.connect(self.browse_input)
        w.OutputBrowseButton.clicked.connect(self.browse_output)
        w.InputLineEdit.textChanged.connect(self.on_path_changed)
        w.OutputLineEdit.textChanged.connect(self.on_path_changed)

        self.include_subdirs_checkbox = w.findChild(QtWidgets.QCheckBox, "checkBoxIncludeSubdirs")
        self.include_subdirs_checkbox.setChecked(True)
        self.include_subdirs_checkbox.adjustSize()
        self.include_subdirs_checkbox.toggled.connect(self.subfolders_toggled)

        self.colorbox = w.findChild(QComboBox, "ColorComboBox")
        self.devicebox = w.findChild(QComboBox, "DeviceComboBox")
        self.savebox = w.findChild(QCheckBox, "SaveCheckBox")

        for name in color_names:
            self.colorbox.addItem(name)
        for device in devices:
            self.devicebox.addItem(device)

        self.savebox.setEnabled(False)
        self.savebox.toggled.connect(self.savehere_checked)
        self.colorbox.setCurrentText("None")
        self.colorbox.currentTextChanged.connect(self.on_color_changed)
        self.devicebox.currentTextChanged.connect(self.on_device_changed)
        
        w.StartButton.clicked.connect(self.startProcessing)
        w.ClearButton.clicked.connect(self.clear_images)

        w.DragDropArea.setAcceptDrops(True)
        w.DragDropArea.dragEnterEvent = self.drag_enter
        w.DragDropArea.dropEvent = self.drop_event

        w.ClearButton.setEnabled(False)
        w.StartButton.setEnabled(False)
        w.progressBar.setValue(0)
        w.StatusLabel.setText("Waiting for input")

    def subfolders_toggled(self, checked):
        # print(f"__{self.in_path}__")
        if self.in_path:
            self.images = [] 
            include_subdirs = self.include_subdirs_checkbox.isChecked()
            self.load_images(self.in_path, include_subdirs)

    def browse_input(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self.window, "Select Input Folder")
        if folder:
            self.window.InputLineEdit.setText(folder)
            include_subdirs = self.include_subdirs_checkbox.isChecked()
            self.load_images(folder, include_subdirs)
            self.savebox.setEnabled(True)

    def browse_output(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self.window, "Select Output Folder")
        if folder:
            self.window.OutputLineEdit.setText(folder)
    
    def on_path_changed(self, _=None):
        self.in_path = self.window.InputLineEdit.text().strip()
        self.out_path = self.window.OutputLineEdit.text().strip()
        self.update_startButton_state()

    def update_startButton_state(self):
        valid = bool(self.images) and os.path.isdir(self.out_path)
        self.window.StartButton.setEnabled(valid)

    def load_images(self, folder:str, include_subdirs: bool=True):
        exts = ('*.png','*.jpg','*.jpeg','*.bmp','*.tif','*.tiff')
        self.images = []
        for ext in exts:    
            if include_subdirs:
                # Find images recursively in subdirectories
                self.images += glob.glob(os.path.join(folder, '**', ext), recursive=True)
            else:
                # Find images only in the specified folder
                self.images += glob.glob(os.path.join(folder, ext))
        self.images.sort()

        if not self.images:
            self.window.StatusLabel.setText("No images found in input folder.")
            print("no images")
            self.window.StartButton.setEnabled(False)

        else:
            self.window.StatusLabel.setText(f"Loaded {len(self.images)} images")
            self.window.ClearButton.setEnabled(True)
            self.on_path_changed()

    def drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def drop_event(self, event):
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            self.in_path = p  # Update input path to the dropped item
            self.window.InputLineEdit.setText(p)
            if os.path.isdir(p):
                if self.include_subdirs_checkbox.isChecked():
                    # If include subdirs is checked, walk through all subdirectories
                    for root, dirs, files in os.walk(p):
                        for fname in files:
                            full = os.path.join(root, fname)
                            if self._is_valid_image(full):
                                self.images.append(full)
                else:
                    # If not, just list files in the directory
                    for fname in os.listdir(p):
                        full = os.path.join(p, fname)
                        if self._is_valid_image(full):
                            self.images.append(full)
            elif os.path.isfile(p) and self._is_valid_image(p):
                self.images.append(p)

        if self.images:
            self.include_subdirs_checkbox.setEnabled(False)
            self.window.StatusLabel.setText(f"Loaded {len(self.images)} images")
            self.window.ClearButton.setEnabled(True)
            self.savebox.setEnabled(True)
            if self.out_path or self.save_here:
                self.window.StartButton.setEnabled(True)
            else:
                self.window.StatusLabel.setText(f"Loaded {len(self.images)} images, Please set output path")
        else:
            self.window.StatusLabel.setText("No images loaded")
    
    def _is_valid_image(self, path):
        ext = os.path.splitext(path)[1].lower()
        return ext in {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'}
    
    def clear_images(self):
        #self.images = [] 
        self.reset_states()
        self.window.StatusLabel.setText("All images cleared. Please reset") 
        self.window.InputLineEdit.clear()
        self.window.OutputLineEdit.clear()
        self.savebox.setEnabled(False)
        self.window.StartButton.setEnabled(False)  
        self.window.progressBar.setValue(0)  
        self.include_subdirs_checkbox.setEnabled(True)

    def savehere_checked(self):
        self.save_here = self.savebox.isChecked()
        if self.save_here:
            self.out_path = self.in_path
            self.window.OutputBrowseButton.setEnabled(False)
            self.window.OutputLineEdit.setText(self.out_path)
        else:
            self.window.OutputBrowseButton.setEnabled(True)
        self.update_startButton_state()

    def on_color_changed(self):
        idx = self.colorbox.currentIndex()
        self.rgb = colors_dict.get(idx)
        print("Selected RGB:", self.rgb)
        # Change the background color of the combobox
        if self.rgb is not None:
            qcolor = QColor(*self.rgb)
        else: 
            qcolor = QColor(*(255,255,255))
        self.colorbox.setStyleSheet(f"background-color: {qcolor.name()};")

    def on_device_changed(self):
        self.device = self.devicebox.currentText()
        print(self.device)

    def startProcessing(self):
        w = self.window
        if not self.images or not os.path.isdir(self.out_path):
            w.StatusLabel.setText("Please set input images and output folder")
            return

        # Get color name before starting worker (UI access must be on main thread)
        color_name = self.colorbox.currentText()
        if color_name == "None":
            color_name = "Gray"

        # Disable controls during processing
        w.StartButton.setEnabled(False)
        w.ClearButton.setEnabled(False)
        w.progressBar.setValue(0)

        self._worker = BackgroundWorker(
            self.images, self.device, self.rgb,
            self.out_path, self.save_here, color_name
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_progress(self, current, total, status):
        if self.window is None:
            return
        w = self.window
        pct = int(current / total * 100)
        w.progressBar.setValue(pct)
        w.StatusLabel.setText(f"Progressing {current}/{total}:{status}")

    def _on_finished(self, results):
        self.results = results
        self._worker = None
        if self.window is None:
            return
        self.window.StartButton.setEnabled(True)
        self.window.ClearButton.setEnabled(True)
        self.export_results()

    def export_results(self):
        """ Export result and highlight the failed images """
        df = pd.DataFrame(self.results)
        out_path = os.path.join(self.out_path, "remove_background_results.xlsx")


        with pd.ExcelWriter(out_path, engine="xlsxwriter") as writer:
            df.to_excel(writer, sheet_name="Results", index=False)
            workbook  = writer.book
            worksheet = writer.sheets["Results"]

            red_fmt = workbook.add_format({
                "font_color": "red"
            })

            status_col = df.columns.get_loc("status")

            for row_idx, status in enumerate(df["status"], start=1):
                if status != "processed":
                    worksheet.write(row_idx, status_col, status, red_fmt)

        self.window.StatusLabel.setText(f"Results exported to {out_path}")
