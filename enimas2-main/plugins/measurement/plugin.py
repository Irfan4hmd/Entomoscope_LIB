from PluginBase import PluginBase
import os
import sys
import glob
import cv2
import traceback
import numpy as np
import pandas as pd
from math import cos, sin, radians
from pathlib import Path
import json
from json import load, dump
from dataclasses import dataclass
from PyQt5 import QtCore, uic, QtWidgets
from PyQt5.QtWidgets import QLabel, QComboBox, QInputDialog
from PyQt5.QtCore import Qt, QEvent
from datetime import datetime

from OBB.obb_measurement_final import ImageUtils
from OBB.obb_measurement_final import DetectionModel, MeasurementUtils, ImageUtils

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_M_CONFIG_PATH = os.path.join(BASE_DIR, 'plugin_m_config.json')

def load_m_config():
    default_config = {}
    if not os.path.exists(PLUGIN_M_CONFIG_PATH):
        _save_json(PLUGIN_M_CONFIG_PATH, default_config)
        return default_config
    try: 
        return load(open(PLUGIN_M_CONFIG_PATH, encoding='utf-8'))
    except(json.JSONDecodeError, IOError):
        _save_json(PLUGIN_M_CONFIG_PATH, default_config)
        return default_config

def _save_json(path, data):
    dirname = os.path.dirname(path)
    if dirname and not os.path.isdir(dirname):
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding='utf-8') as f:
        dump(data, f, ensure_ascii=False, indent=2)

@dataclass
class Lens:
    name: str
    lenght: int
    settings: list
    base_focus_height: float = None
    base_focus_height_arducam_old: float = None
    base_focus_height_arducam: float = None
    base_focus_height_vaimaging: float = None
    magnification_factor: float = None
    scale: float = None
    scale_vaimaging: float = 1.0
    scale_arducam: float = 1.0


    @property
    def label_str(self):
        return f"{self.name} - {self.lenght}mm"

def load_lens():
    with open("lenses.json", "r") as file:
        json_file = load(file)
        
        all_lenses = []
        for lens in json_file["lenses"]:
            all_lenses.append(Lens(**lens))
        if not "current" in json_file:
            current_lens = all_lenses[0]
        else:
            current_lens = [lens for lens in all_lenses if lens.name == json_file["current"]][0]
        
    return all_lenses

def load_cameras():
    cameras = [{'name':'ArducamCamera'},{'name':'VAImagingCamera'}]
    return cameras

class Plugin(PluginBase):
    def __init__(self):
        self._name = 'Measurement'
        # Load the UI
        self.cfg = load_m_config()
        self.lenses = load_lens()
        self.cameras = load_cameras()
        self.window = None
        self.reset_states()

        current_dir = os.path.dirname(__file__)
        root = os.path.abspath(os.path.join(current_dir, os.pardir, os.pardir))
        model_path = os.path.join(root,'models', 'obb', 'yolov8m_obb3_best.onnx')
        self.detector = DetectionModel(model_path)

    @property
    def name(self):
        return self._name

    # def init(self, app_context: dict):
    #     print(f"Initializing {self.name} plugin with context: {app_context}")
        # load model, config, etc.

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
    
    def bind_ui(self):
        w = self.window
        w.CameraComboBox.clear()
        self.base_cam_names = [c['name'] for c in self.cameras]
        self.all_cam_names = self.base_cam_names + [c for c in self.cfg if c not in self.base_cam_names]
        for cam in self.all_cam_names:
            w.CameraComboBox.addItem(cam)
        w.LensComboBox.clear()
        self.base_lens_names = [l.name for l in self.lenses]
        selected_cam = w.CameraComboBox.currentText() 
        custom_lenses = list(self.cfg.get(selected_cam, {}).keys())
        self.all_lens_names = self.base_lens_names + [
            l for l in custom_lenses
            if l not in self.base_lens_names
        ]
        for lens in self.all_lens_names:
            w.LensComboBox.addItem(lens)

        self.on_cam_changed(w.CameraComboBox.currentText())
        self.on_lens_changed(w.LensComboBox.currentText())

        self.include_subdirs_checkbox = w.findChild(QtWidgets.QCheckBox, "checkBoxIncludeSubdirs")
        self.include_subdirs_checkbox.setChecked(True)
        self.include_subdirs_checkbox.adjustSize()
        self.include_subdirs_checkbox.toggled.connect(self.subfolders_toggled)
       

        w.BrowseInputButton.clicked.connect(self.browse_input)
        w.BrowseOutputButton.clicked.connect(self.browse_output)
        w.InputPathLineEdit.textChanged.connect(self.on_path_changed)
        w.OutputPathLineEdit.textChanged.connect(self.on_path_changed)
        #w.LensComboBox.currentIndexChanged.connect(self.update_lens_scale)
        w.LensComboBox.currentTextChanged.connect(self.on_lens_changed)
        w.CameraComboBox.currentTextChanged.connect(self.on_cam_changed)
        w.MeasureButton.clicked.connect(self.startMeasurement)
        w.ImageClearButton.clicked.connect(self.clear_images)
        w.scaleLineEdit.returnPressed.connect(self.scale_edited)


        w.DragDropArea.setAcceptDrops(True)
        w.DragDropArea.dragEnterEvent = self.drag_enter
        w.DragDropArea.dropEvent = self.drop_event

        w.MeasureButton.setEnabled(False)
        w.ImageClearButton.setEnabled(False)
        w.progressBar.setValue(0)
        w.StatusLabel.setText("Ready")
        self.update_lens_scale()

    def subfolders_toggled(self, checked):
        # print(f"__{self.in_path}__")
        if self.in_path:
            self.images = [] 
            include_subdirs = self.include_subdirs_checkbox.isChecked()
            self.load_images(self.in_path, include_subdirs)

    def browse_input(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self.window, "Select Input Folder")
        if folder:
            self.window.InputPathLineEdit.setText(folder)
            include_subdirs = self.include_subdirs_checkbox.isChecked()
            self.load_images(folder, include_subdirs)


    def browse_output(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self.window, "Select Output Folder")
        if folder:
            self.window.OutputPathLineEdit.setText(folder)

    def on_path_changed(self, _=None):
        self.in_path = self.window.InputPathLineEdit.text().strip()
        self.out_path = self.window.OutputPathLineEdit.text().strip()
        self.update_startButton_state()


    def update_startButton_state(self):
        valid = bool(self.images) and os.path.isdir(self.out_path)
        self.window.MeasureButton.setEnabled(valid)

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
            self.window.MeasureButton.setEnabled(False)

        else:
            self.window.StatusLabel.setText(f"Loaded {len(self.images)} images")
            self.window.ImageClearButton.setEnabled(True)
            self.on_path_changed()

    def _is_valid_image(self, path):
        ext = os.path.splitext(path)[1].lower()
        return ext in {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'}

    def on_cam_changed(self, name: str):
        name = name.strip()
        self.current_camera = {'name': name}
        if name not in self.base_cam_names and name not in self.cfg:
            self.cfg[name]= {}
            _save_json(PLUGIN_M_CONFIG_PATH, self.cfg)
            return
        base_ls   = [l.name for l in self.lenses]
        custom_ls = list(self.cfg.get(name, {}).keys())
        if name in self.base_cam_names and name not in self.cfg:
            lenses = base_ls
        elif name in self.cfg and name not in self.base_cam_names:
            lenses = custom_ls

        cb = self.window.LensComboBox
        cb.blockSignals(True)
        cb.clear()
        cb.addItems(lenses)
        cb.blockSignals(False)
        self.on_lens_changed(lenses[0])
        #print(f"Current camera set to: {self.current_camera['name']}")

    def on_lens_changed(self, name: str):
        name = name.strip()

        cam = self.window.CameraComboBox.currentText().strip()
        if cam and name and name not in [l.name for l in self.lenses] and name not in self.cfg.get(cam, {}):
            # when add specific scale
            self.cfg.setdefault(cam, {})[name] = None
            _save_json(PLUGIN_M_CONFIG_PATH, self.cfg)
            return
        lens_obj = next((l for l in self.lenses if l.name == name), None)
        if lens_obj:
            self.current_lens = lens_obj
        else:       
            self.current_lens = Lens(name=name, lenght=0, settings=[])
        self.update_lens_scale()
        # print(f"Current lens set to: {self.current_lens.label_str}")

    def scale_edited(self):
        s = self.window.scaleLineEdit.text().strip()
        if not s:
            return
        self.scale = s
        cam, ok1 = QInputDialog.getText(self.window, "Add Camera", f"Please add Camera for current scale({self.scale}):")
        if not ok1:
            self.update_lens_scale()
            return
        cam = cam.strip()
        
        ls,ok2 = QInputDialog.getText(self.window, "Add Lens", f"Please add Lens for current scale({self.scale}):")
        if not ok2:
            self.update_lens_scale()
            return
        ls = ls.strip()
        if cam not in self.all_cam_names:
            self.window.CameraComboBox.addItem(cam)
        self.window.CameraComboBox.setCurrentText(cam)
        if ls not in self.all_lens_names:
            self.window.LensComboBox.addItem(ls)
        self.window.LensComboBox.setCurrentText(ls)
        if ls not in ImageUtils.lens_conversion_factors.get(cam,{}):
            if s is not None:
                val = float(s)
                self.cfg[cam][ls] = val
                _save_json(PLUGIN_M_CONFIG_PATH, self.cfg)

    def update_lens_scale(self):
        if self.current_lens is None:
            return
        cam = self.window.CameraComboBox.currentText().strip()
        # print(f"Current camera: {cam}")
        ln = self.window.LensComboBox.currentText().strip()
        if cam in self.cfg and ln in self.cfg[cam] and self.cfg[cam][ln] is not None:
            val = self.cfg[cam][ln]
        elif cam == "VAImagingCamera": 
            val = self.current_lens.scale_vaimaging if self.current_lens.scale_vaimaging else None
        elif cam == "ArducamCamera":
            val = self.current_lens.scale_arducam if self.current_lens.scale_arducam else None
        else:
            val = None
        self.scale = val
        self.window.scaleLineEdit.setText(f" {self.scale}")

    def drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def drop_event(self, event):
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            self.in_path = p  # Update input path to the dropped item
            self.window.InputPathLineEdit.setText(p)
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
            self.window.ImageClearButton.setEnabled(True)
            if self.out_path:
                self.window.StartButton.setEnabled(True)
            else:
                self.window.StatusLabel.setText(f"Loaded {len(self.images)} images, Please set output path")
        else:
            self.window.StatusLabel.setText("No images loaded")

    def clear_images(self):
        #self.images = [] 
        self.reset_states()
        self.window.StatusLabel.setText("All images cleared. Please reset") 
        self.window.InputPathLineEdit.clear()
        self.window.OutputPathLineEdit.clear()
        self.window.MeasureButton.setEnabled(False)  
        self.window.progressBar.setValue(0)  
        self.include_subdirs_checkbox.setEnabled(True)  # Reset to default state


    def startMeasurement(self):
        w = self.window

        # Validate that camera, lens, input list, and output folder are set
        cam_idx = w.CameraComboBox.currentIndex()
        lens_idx = w.LensComboBox.currentIndex()
        if cam_idx < 0 or lens_idx < 0 or not self.images or not os.path.isdir(self.out_path):
            w.StatusLabel.setText("Please set camera, lens, input images and output folder")
            return
        
        total = len(self.images)
        self.results = []
        for i, img_path in enumerate(self.images, start=1):
            try:
                img = cv2.imread(img_path)
                padding_percentage = 0.10
                detected_box = self.detector.perform_detection(img_path, padding_percentage)
                if detected_box:
                    # Create subfolder to save result
                    name = os.path.splitext(os.path.basename(img_path))[0]
                    sub  = os.path.join(self.out_path, name)
                    os.makedirs(sub, exist_ok=True)

                    # Draw box and measurement result on original image
                    img_box, img_annotation, width_mm, height_mm = self.draw_measurement(img, detected_box, name, sub)

                    # Save annotated images
                    box_file = os.path.join(sub, f"{name}_box.png")
                    cv2.imwrite(box_file, img_box)

                    measure_file = os.path.join(sub, f"{name}_annotated.png")
                    cv2.imwrite(measure_file, img_annotation)
                    status = "Measurement done"
                    width = width_mm
                    height = height_mm
                else:
                    status = "Box not detected"
                    width = 0
                    height = 0

            except Exception as e:
                traceback.print_exc()
                self.results.append({
                    "image": os.path.basename(img_path),
                    "status": f"Error: {e}"
                })
                status = f"Error: {e}"
                width = 0
                height = 0
            
            self.results.append({
            "image": os.path.basename(img_path),
            "status": status, 
            "width": width,
            "height": height,
            "camera": self.current_camera,
            "lens":self.current_lens,
            "scale": self.scale if self.scale else None 
            })

            # Progress bar 
            pct = int(i / total * 100)
            w.progressBar.setValue(pct)
            w.StatusLabel.setText(f"Progressing {i}/{total}:{status}")
            QtWidgets.QApplication.processEvents()

        self.export_results()

    def draw_measurement(self, img, detected_box, name, sub):
        self.detected_box = detected_box
        center_padded, size_padded, angle = detected_box

        # Convert box from padded image to original image
        padding_percentage = 0.1
        img_h, img_w, _= img.shape
        pad_h = int(img_h * padding_percentage)
        pad_w = int(img_w * padding_percentage)

        orig_center_x = center_padded[0] - pad_w
        orig_center_y = center_padded[1] - pad_h
        orig_w   = size_padded[0]
        orig_h   = size_padded[1]
        orig_angle = angle

        orig_box = ((orig_center_x, orig_center_y), (orig_w, orig_h), orig_angle)
        self.original_box = orig_box
        pts = cv2.boxPoints(orig_box)
        pts = np.int0(pts)

        img_box = img.copy()
        cv2.drawContours(img_box, [pts], 0,(0,255,0),2)

        img_annotation, width_mm, height_mm = self.draw_text(img, orig_h, orig_w, pts)
        self.save_metafile(name, sub, width_mm, height_mm)
        return img_box, img_annotation, width_mm, height_mm
    
    def draw_text(self, img, orig_h, orig_w, pts):
        print(f"orig_h: {orig_h}, orig_w: {orig_w}")
        width_mm  = ImageUtils.pixels_to_mm(orig_w, self.current_lens.name, self.scale)
        height_mm = ImageUtils.pixels_to_mm(orig_h, self.current_lens.name, self.scale)
        if height_mm < width_mm:
            width_mm, height_mm = height_mm, width_mm
            orig_w, orig_h = orig_h, orig_w

        img_annotation = img.copy()
        # --------- draw arrow and text ---------
        p0, p1, p2 = pts[0], pts[1], pts[2]
        offset = int(min(orig_w, orig_h) * 0.15)

        def perp(u):
            return np.array([-u[1], u[0]])
        # vector of width
        v_w = p1 - p0
        n_w = perp(v_w)
        n_w = n_w / np.linalg.norm(n_w)

        # vector of height 
        v_h = p2 - p1
        n_h = perp(v_h)
        n_h = n_h / np.linalg.norm(n_h)

        color_w = (255, 0, 0)
        color_h = (0, 255, 0)
        thickness = 3
        tip_len = 0.05

        # Draw Arrow
        cv2.arrowedLine(img_annotation, tuple(p0), tuple(p1), color_w, thickness, tipLength=tip_len)
        cv2.arrowedLine(img_annotation, tuple(p1), tuple(p0), color_w, thickness, tipLength=tip_len)

        cv2.arrowedLine(img_annotation, tuple(p1), tuple(p2), color_h, thickness, tipLength=tip_len)
        cv2.arrowedLine(img_annotation, tuple(p2), tuple(p1), color_h, thickness, tipLength=tip_len)

        mid_w = ((p0 + p1) / 2).astype(int)
        mid_h = ((p1 + p2) / 2).astype(int)

        text_w_pos = (mid_w + n_w * offset).astype(int)
        text_h_pos = (mid_h + n_h * offset).astype(int)

        # Draw Text
        cv2.putText(img_annotation, f"{width_mm:.2f}mm", tuple(text_w_pos),
                    cv2.FONT_HERSHEY_SIMPLEX, 4, color_w, 3, cv2.LINE_AA)
        cv2.putText(img_annotation, f"{height_mm:.2f}mm", tuple(text_h_pos),
                    cv2.FONT_HERSHEY_SIMPLEX, 4, color_h, 3, cv2.LINE_AA)
        return img_annotation, width_mm, height_mm

    def save_metafile(self, name, save_path, w, h):
        save_path = Path(save_path)
        metadata_file_path = save_path / f"{name}_metadata.txt"

        if self.detected_box is not None:
            center, size, angle = self.detected_box
            box_coordinate = (center[0], center[1], size[0], size[1], angle)
        else:
            box_coordinate = (0, 0, 0, 0)

        if self.original_box is not None:
            center_original, size_original, angle_original = self.original_box  
            box_coordinate_original = (center_original[0], center_original[1], size_original[0], size_original[1], angle_original)
        else:
            box_coordinate_original = (0, 0, 0, 0)
        
        scale_used = self.scale if self.scale else 1.0
        # information to write inside the metadata txt file
        metadata = {
            "Date": datetime.now().isoformat(timespec='seconds'),
            "Camera": self.current_camera['name'] if hasattr(self, 'current_camera') else '',
            "Lens": self.current_lens.label_str,
            "Scale": f"{scale_used} mm/pixel",
            "Bounding box Coordinates for padded image(x,y,width,height)": box_coordinate,
            "Bounding box Coordinates for original image(x,y,width,height)": box_coordinate_original,
            "Width (mm)": w,
            "Height (mm)": h,
        }
        # save metadata txt file
        with metadata_file_path.open('w') as file:
            for key, value in metadata.items():
                file.write(f"{key}:\n")
                file.write(f"{value}\n\n")

    def export_results(self):
        """ Export result and highlight the failed images """
        df = pd.DataFrame(self.results)
        out_path = os.path.join(self.out_path, "measurement_results.xlsx")

        with pd.ExcelWriter(out_path, engine="xlsxwriter") as writer:
            df.to_excel(writer, sheet_name="Results", index=False)
            workbook  = writer.book
            worksheet = writer.sheets["Results"]

            red_fmt = workbook.add_format({
                "font_color": "red"
            })

            status_col = df.columns.get_loc("status")

            for row_idx, status in enumerate(df["status"], start=1):
                if status != "Measurement done":
                    worksheet.write(row_idx, status_col, status, red_fmt)

        self.window.StatusLabel.setText(f"Results exported to {out_path}")
             

            

                                                                
            


        
