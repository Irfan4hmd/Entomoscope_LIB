import PIL.Image
import numpy as np

from PyQt5.QtWidgets import QDialog, QCheckBox, QPushButton, QLabel, QComboBox, QDialogButtonBox, QMessageBox
from PyQt5.QtGui import QColor
from PyQt5.QtCore import QSettings
from PyQt5 import uic
from Background_remover.background_remover import remove_backgound
from Background_remover.data_util import colors_dict
from ultralytics import YOLO
import onnxruntime as ort
from pathlib import Path

from Tools.ml_models import Predictor, YOLO_MODELS
from Tools.image_output import is_valid_scale
import constants

color_names = [
    "Default: Gray",
    "Red", "Green", "Blue", "Yellow", "Orange",
    "Purple", "Cyan", "Pink", "Black", "White",
]

def save_cropped_images(image, input_path):
    orig = Path(input_path)
    stem = orig.stem
    ext = orig.suffix or ".png"

    output_dir = orig.parent
    output_file = output_dir / f"{stem}_cropped{ext}"
    image.save(output_file)
    return output_file

def find_onnx_models(directory: Path):
    all_files = directory.glob('*')
    onnx_files = [file for file in all_files if file.suffix.lower() == '.onnx']
    if not onnx_files:
        return None
    return onnx_files

def cropping(img_path, detect_model: YOLO = None):
    stacked_image = PIL.Image.open(img_path)
    if detect_model is None:
        raise RuntimeError("No ONNX model found for cropping.")
    detection_result = detect_model.predict(stacked_image, 0.6)
    if not detection_result:
        return None
    first_result = next(detection_result)
    boxes = first_result.boxes.xyxy
    stacked_image_np = np.array(stacked_image)

    if len(boxes) > 0:
        # Crop the image based on detection box
        x1, y1, x2, y2 = map(int, boxes[0][:4])
        cropped_image_np = stacked_image_np[y1:y2, x1:x2]
        cropped_image_pil = PIL.Image.fromarray(cropped_image_np)
        cropped_img_file = save_cropped_images(cropped_image_pil, img_path)
        return cropped_img_file
    else:
        print("No detection boxes found, returning None.")
        return None


class ImageSettingsDialog(QDialog):
    def __init__(self, mainWindow):
        super().__init__()

        uic.loadUi("UserInterface/ImageSettingsDialog.ui", self)

        self.mainwindow = mainWindow
        self.scalebar_checkbox = self.findChild(QCheckBox, "ScaleBarCheckBox")
        self.scalebar_checkbox.setChecked(self.mainwindow.show_scalebar)
        self.scalebar_checkbox.setToolTip(
            "Requires a valid Scale (mm/pixel) for the active camera and lens."
        )
        self.uniformBG_checkbox = self.findChild(QCheckBox, "UniformBGCheckBox")
        self.replace_color_box = self.findChild(QComboBox, "comboBox")
        self.cropping_checkbox = self.findChild(QCheckBox, "checkBox_2")
        self.cropping_model_box = self.findChild(QComboBox, "comboBox_cropping")
        self.cropping_model_box.addItem("YOLO-Fast")
        self.cropping_model_box.addItem("BoxSegmenter-Accurate")
        self.cropping_model_box.setEnabled(False)
        self.cropping_model_box.currentIndexChanged.connect(self.on_model_changed)
        self.cropping_method = None
        # self.cropping_model_box.setCurrentIndex(0)
        self.statusLabel = self.findChild(QLabel, "StatusLable")
        self.ok_bttn = self.findChild(QPushButton, "okButton")
        self.cancel_bttn = self.findChild(QPushButton, "cancelButton")
        self.ok_bttn.clicked.connect(self.on_ok)
        self.cancel_bttn.clicked.connect(self.reject)

        self.scalebar_checkbox.toggled.connect(self.scalebar)
        self.uniformBG_checkbox.toggled.connect(self.remove_background)
        self.cropping_checkbox.toggled.connect(self.cropping)

        for name in color_names:
            self.replace_color_box.addItem(name)
        self.replace_color_box.setEnabled(False)
        self.replace_color_box.setCurrentText("Default: Gray")
        self.replace_color_box.currentIndexChanged.connect(self.on_color_changed)
        self.rgb = None

        self._orig = {}
        self.states = {
            "Scalebar"   : self.scalebar_checkbox.isChecked(),
            "Cropping"   : self.cropping_checkbox.isChecked(),
            "Uniform BG" : self.uniformBG_checkbox.isChecked()
        }

    def on_model_changed(self, index):
        self.cropping_method = self.cropping_model_box.currentIndex()

    def on_color_changed(self, index):
        idx = self.replace_color_box.currentIndex()
        self.rgb = colors_dict.get(idx)
        print("Selected RGB:", self.rgb)
        # Change the background color of the combobox
        if self.rgb is not None:
            qcolor = QColor(*self.rgb)
        else: 
            #self.rgb = None
            qcolor = QColor(*(255,255,255))
        self.replace_color_box.setStyleSheet(f"background-color: {qcolor.name()};")

    def scalebar(self):
        self.update_statuslabel()

    def remove_background(self):
        self.update_statuslabel()
        if self.uniformBG_checkbox.isChecked():
            self.replace_color_box.setEnabled(True)
        else: 
            self.replace_color_box.setEnabled(False)

    def cropping(self):
        self.update_statuslabel()
        if self.cropping_checkbox.isChecked():
            self.cropping_model_box.setEnabled(True)
        else: 
            self.cropping_model_box.setEnabled(False)

    def update_statuslabel(self):
        self.states = {
            "Scalebar"   : self.scalebar_checkbox.isChecked(),
            "Cropping"   : self.cropping_checkbox.isChecked(),
            "Uniform BG" : self.uniformBG_checkbox.isChecked()       
        }
        parts = [f"{name}: {'On' if on else 'Off'}" for name, on in self.states.items()]
        self.statusLabel.setText("   ".join(parts))

    def showEvent(self, event):
        
        self._orig['crop']      = self.cropping_checkbox.isChecked()
        self._orig['uniform']   = self.uniformBG_checkbox.isChecked()
        self._orig['scalebar']  = self.scalebar_checkbox.isChecked()
        self._orig['cropping method'] = self.cropping_model_box.currentIndex()
        self._orig['replace color'] = self.replace_color_box.currentIndex()
        super().showEvent(event)

    def on_ok(self):
        if self.scalebar_checkbox.isChecked() and not is_valid_scale(
            getattr(self.mainwindow, "current_scale", None)
        ):
            QMessageBox.warning(
                self,
                "Lens calibration required",
                "Scale bar cannot be enabled until the active camera and lens have "
                "a valid Scale (mm/pixel). Use Help > Scale Bar Setup for instructions.",
            )
            return
        self.mainwindow.show_scalebar = self.scalebar_checkbox.isChecked()
        constants.SHOW_SCALEBAR = self.mainwindow.show_scalebar
        self.mainwindow.save_uniform_bg = self.uniformBG_checkbox.isChecked()
        self.mainwindow.cropping = self.cropping_checkbox.isChecked()
        self.mainwindow.replace_color = self.rgb
        self.mainwindow.cropping_method = self.cropping_method
        on_names = [name for name, on in self.states.items() if on]
        if on_names:
            self.mainwindow.proc_var_progress_label.setText(f"{', ' .join(on_names)} selected")
        else:
            self.mainwindow.proc_var_progress_label.setText("No options selected")
        self.accept()
        self.hide()

    def reject(self):
        self.cropping_checkbox.setChecked(self._orig.get('crop', False))
        self.uniformBG_checkbox.setChecked(self._orig.get('uniform', False))
        self.scalebar_checkbox.setChecked(self._orig.get('scalebar', False))
        self.cropping_model_box.setCurrentIndex(self._orig.get('cropping method', 0))
        self.replace_color_box.setCurrentIndex(self._orig.get('replace color', 0))
        super().reject()
