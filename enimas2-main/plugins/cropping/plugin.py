from PluginBase import PluginBase
import os
import sys
import PIL.Image
import glob
import traceback
import constants
import numpy as np
import pandas as pd
import onnxruntime as ort
from pathlib import Path
from PyQt5 import QtCore, uic,QtWidgets
from PyQt5.QtCore import QObject
from PyQt5.QtWidgets import  QCheckBox, QComboBox
from ultralytics import YOLO

from Tools.ml_models import Predictor, YOLO_MODELS
from Tools.cropping_boxsegmenter import crop_image_1

def find_onnx_models(directory: Path):

    all_files = directory.glob('*')
    onnx_files = [file for file in all_files if file.suffix.lower() == '.onnx']
    if not onnx_files:
        return None
    return onnx_files

models_base_path = Path('models').resolve()

onnx_models = find_onnx_models(Path(models_base_path, "cropping"))
YOLO_MODELS['crop'] = YOLO(onnx_models[0], task="detect") if onnx_models is not None else None
detect_model: YOLO = YOLO_MODELS["crop"]

class CroppingWorker(QtCore.QThread):
    """Worker thread for batch image cropping."""
    progress = QtCore.pyqtSignal(int, int, str)  # current, total, status
    finished = QtCore.pyqtSignal(list)  # results list

    def __init__(self, images, cropping_method, out_path):
        super().__init__()
        self.images = images
        self.cropping_method = cropping_method
        self.out_path = out_path

    def run(self):
        results = []
        total = len(self.images)

        for i, img_path in enumerate(self.images, start=1):
            img_name = Path(img_path).name
            model = "YOLO-Fast" if self.cropping_method == 0 else "BoxSegmenter-Accurate"
            try:
                if self.cropping_method == 0:
                    cropped_image_pil = self._crop_yolo(img_path)
                else:
                    cropped_image_pil = crop_image_1(img_path, show_images=False)

                if cropped_image_pil:
                    self._save_cropped(cropped_image_pil, img_path)
                    status = "Cropping successful"
                else:
                    status = "Cropping failed"

                results.append({"image": img_name, "model": model, "status": status})

            except Exception as e:
                traceback.print_exc()
                results.append({"image": img_name, "model": model, "status": f"Error: {e}"})
                status = f"Error: {e}"

            self.progress.emit(i, total, status)

        self.finished.emit(results)

    def _crop_yolo(self, img_path):
        stacked_image = PIL.Image.open(img_path)
        detection_result = detect_model.predict(stacked_image, 0.6)
        first_result = next(detection_result)
        boxes = first_result.boxes.xyxy
        stacked_image_np = np.array(stacked_image)
        if len(boxes) > 0:
            x1, y1, x2, y2 = map(int, boxes[0][:4])
            cropped_image_np = stacked_image_np[y1:y2, x1:x2]
            return PIL.Image.fromarray(cropped_image_np)
        return None

    def _save_cropped(self, image, input_path):
        orig = Path(input_path)
        stem = orig.stem
        ext = orig.suffix or ".png"
        out = Path(self.out_path)
        if out.is_dir() or out.suffix == "":
            output_dir = out
            output_dir.mkdir(parents=True, exist_ok=True)
            output_file = output_dir / f"{stem}_cropped{ext}"
        else:
            output_file = out
            output_file.parent.mkdir(parents=True, exist_ok=True)
            if output_file.stem == stem:
                output_file = output_file.with_name(f"{stem}_cropped{output_file.suffix}")
        image.save(output_file)


class Plugin(PluginBase, QObject):
    """
    Base class for all plugins. 
    """

    def __init__(self):
        super().__init__()
        self._name = 'Cropping'
        self.window = None
        self.reset_states()
        self.cropping_method = 0
        self._worker = None

    @property
    def name(self):
        return self._name
    
    # def init(self, app_context: dict):
    #     print(f"Initializing {self.name} plugin with context: {app_context}")
    
    def run(self, data: dict) -> dict:
        #if self.window is None:
        ui_path = self.get_ui()
        self.window = uic.loadUi(ui_path)
        self.window.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowCloseButtonHint
            | QtCore.Qt.WindowMinimizeButtonHint
        )
        self.window.setFixedSize(self.window.size())
        self.reset_states()
        self.bind_ui()
        self.window.show()
        return {}

    def get_ui(self) -> str:
        return os.path.join(os.path.dirname(__file__), 'plugin.ui')
    
    def reset_states(self):
        self.images = []
        self.results = []
        self.in_path = None
        self.out_path = None
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
       

        self.savebox = w.findChild(QCheckBox, "SaveCheckBox")
        self.savebox.setEnabled(False)
        self.savebox.toggled.connect(self.savehere_checked)
        self.savebox.adjustSize()

        self.cropping_model_box = w.findChild(QComboBox, "comboBox_croppingmodel")
        self.cropping_model_box.addItem("YOLO-Fast")
        self.cropping_model_box.addItem("BoxSegmenter-Accurate")
        self.cropping_model_box.setCurrentText("Yolo-Fast")
        self.cropping_model_box.currentIndexChanged.connect(self.on_model_changed)
        
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
    
    def on_model_changed(self):
        self.cropping_method = self.cropping_model_box.currentIndex()
        model = self.cropping_model_box.currentText()
        self.window.progressBar.setValue(0) 
        self.window.StatusLabel.setText(f"Model set to {model}")

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

    def load_images(self, folder:str, include_subdirs:bool=True):
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
            self.window.StatusLabel.setText("No images found in folder.")
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
            if self.out_path:
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
        self.include_subdirs_checkbox.setEnabled(True)  # Reset to default state

    def savehere_checked(self):
        self.save_here = self.savebox.isChecked()
        if self.save_here:
            self.out_path = self.in_path
            self.window.OutputBrowseButton.setEnabled(False)
            self.window.OutputLineEdit.setText(self.out_path)
        else:
            self.window.OutputBrowseButton.setEnabled(True)
        self.update_startButton_state()

    def save_cropped_images(self, image, input_path):
        orig = Path(input_path)
        stem = orig.stem
        ext = orig.suffix or ".png"

        out = Path(self.out_path)
        if out.is_dir() or out.suffix == "":
            output_dir = out
            output_dir.mkdir(parents=True, exist_ok=True)

            output_file = output_dir / f"{stem}_cropped{ext}"
        else:
            output_file = out
            output_file.parent.mkdir(parents=True, exist_ok=True)
            if output_file.stem == stem:
                output_file = output_file.with_name(f"{stem}_cropped{output_file.suffix}")
        
        image.save(output_file)
    
    def cropping_0(self, img_path):
        stacked_image = PIL.Image.open(img_path)
                
        detection_result = detect_model.predict(stacked_image, 0.6)
        first_result = next(detection_result)
        boxes = first_result.boxes.xyxy
        stacked_image_np = np.array(stacked_image)

        if len(boxes) > 0:
            # Crop the image based on detection box
            x1, y1, x2, y2 = map(int, boxes[0][:4])
            cropped_image_np = stacked_image_np[y1:y2, x1:x2]
            cropped_image_pil = PIL.Image.fromarray(cropped_image_np)
            return cropped_image_pil


    def startProcessing(self):
        print(self.cropping_method)
        w = self.window
        if not self.images:
            w.StatusLabel.setText("Please set input images")
            return
        elif not os.path.isdir(self.out_path):
            w.StatusLabel.setText("Please set output folder")
            return
        
        # Disable controls during processing
        w.StartButton.setEnabled(False)
        w.ClearButton.setEnabled(False)
        w.progressBar.setValue(0)

        self._worker = CroppingWorker(self.images, self.cropping_method, self.out_path)
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
        out_path = os.path.join(self.out_path, "cropping_results.xlsx")


        with pd.ExcelWriter(out_path, engine="xlsxwriter") as writer:
            df.to_excel(writer, sheet_name="Results", index=False)
            workbook  = writer.book
            worksheet = writer.sheets["Results"]

            red_fmt = workbook.add_format({
                "font_color": "red"
            })

            status_col = df.columns.get_loc("status")

            for row_idx, status in enumerate(df["status"], start=1):
                if status != "Cropping successful":
                    worksheet.write(row_idx, status_col, status, red_fmt)

        self.window.StatusLabel.setText(f"Results exported to {out_path}")
