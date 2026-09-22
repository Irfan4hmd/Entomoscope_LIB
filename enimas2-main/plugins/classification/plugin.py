from PluginBase import PluginBase
import os
import glob
import traceback
import pandas as pd
import onnxruntime as ort
from pathlib import Path
from threading import Thread
from PyQt5 import QtCore, uic,QtWidgets
from PyQt5.QtCore import pyqtSignal, Qt, QObject
from PyQt5.QtWidgets import  QFileDialog, QDialog, QComboBox

from Tools.ml_models import load_models, Predictor, ONNX_MODELS, YOLO_MODELS
from UserInterface.checkable_combo_box import CheckableComboBox
from UserInterface.ui import AddModelDialog

class Signals(QObject):
    classification_done = pyqtSignal(list, list)

class Plugin(PluginBase, QObject):
    """
    Base class for all plugins. 
    """
    fuseStatus = pyqtSignal(str)
    #predictionDone = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self._name = 'Classification'
        self.window = None
        self.signals = Signals()
        self.signals.classification_done.connect(self.on_prediction_done)
        self.reset_states()

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
        self.image_categories = {
            "uniform_cropped": [],
            "cropped": [],
            "uniform": [],
            "original": []
        }
        self.selected_image_variant = []
        self.current_models = []
        self.results = []
        self.in_path = None
        self.out_path = None
        self.pred_label = None
        self.prob_val = None
    
    def bind_ui(self):
        w = self.window
       # w.cmbModels.clear()
        #self.load_model()
        self.classification_model_selector = w.findChild(CheckableComboBox, "cmbModels")
        load_models(self.classification_model_selector.getAllOptions())
        w.AddModelButton.clicked.connect(self.add_models)

        self.include_subdirs_checkbox = w.findChild(QtWidgets.QCheckBox, "checkBoxIncludeSubdirs")
        self.include_subdirs_checkbox.setChecked(True)  # Default to True
        self.include_subdirs_checkbox.toggled.connect(self.subfolders_toggled)
        self.include_subdirs_checkbox.adjustSize()

        w.InputBrowseButton.clicked.connect(self.browse_input)
        w.OutputBrowseButton.clicked.connect(self.browse_output)
        w.InputPathLineEdit.textChanged.connect(self.on_path_changed)
        w.OutputPathLineEdit.textChanged.connect(self.on_path_changed)
        w.cmbModels.currentTextChanged.connect(self.on_model_changed)
        w.VariantCombo.setEditable(True)
        w.VariantCombo.setInsertPolicy(QComboBox.NoInsert)
        w.VariantCombo.lineEdit().setPlaceholderText("Select image variants:")
        w.VariantCombo.lineEdit().setReadOnly(True)
        w.VariantCombo.currentTextChanged.connect(self.on_image_variant_changed)
        w.StartButton.clicked.connect(self.startClassification)
        w.ImageClearButton.clicked.connect(self.clear_images)

        w.DragDropArea.setAcceptDrops(True)
        w.DragDropArea.dragEnterEvent = self.drag_enter
        w.DragDropArea.dropEvent = self.drop_event

        w.ImageClearButton.setEnabled(False)
        w.StartButton.setEnabled(False)
        w.progressBar.setValue(0)
        w.StatusLabel.setText("Waiting for input")

    def add_models(self):
        dialog = AddModelDialog(self.window)
        if dialog.exec() == QDialog.Accepted:
            self.classification_model_selector.updateChoices()

    def on_model_changed(self, item):
        checked = []
        mdl = self.classification_model_selector.model()
        for row in range(mdl.rowCount()):
            itm = mdl.item(row)
            if itm.checkState() == Qt.Checked:
                checked.append(itm.text())

        self.current_models = checked
        #print(f"Current model set to: {self.current_models}")

    def subfolders_toggled(self, checked):
        # print(f"__{self.in_path}__")
        if self.in_path:
            self.images = [] 
            self.image_categories = {
                "uniform_cropped": [],
                "cropped": [],
                "uniform": [],
                "original": []
                }
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
        self.window.StartButton.setEnabled(valid)

    def update_image_variant(self):
        """ Update the image variant in the UI """
        self.image_categories = {
            "uniform_cropped": [],
            "cropped": [],
            "uniform": [],
            "original": []
        }
        variant = ["uniform_cropped","cropped", "uniform"]

        for img in self.images:
            stem = Path(img).stem.lower()
            placed = False
            for v in variant:
                if stem.endswith(v):
                    self.image_categories[v].append(img)
                    placed = True
                    break
            if not placed:
                self.image_categories["original"].append(img)

        self.window.VariantCombo.clear()
        for k, lst in self.image_categories.items():
            if lst:
                self.window.VariantCombo.addItem(k)

        #self.window.VariantCombo.setCurrentText("original")  # Default to Raw if no variants found
        #self.on_image_variant_changed(self.window.VariantCombo.currentText())

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
            self.window.ImageClearButton.setEnabled(True)
            self.update_image_variant()
            self.on_path_changed()

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
            self.update_image_variant()

            self.window.StatusLabel.setText(f"Loaded {len(self.images)} images")
            self.window.ImageClearButton.setEnabled(True)
            if self.out_path:
                self.window.StartButton.setEnabled(True)
            else:
                self.window.StatusLabel.setText(f"Loaded {len(self.images)} images, Please set output path")
        else:
            self.window.StatusLabel.setText("No images loaded")
    
    def _is_valid_image(self, path):
        ext = os.path.splitext(path)[1].lower()
        return ext in {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'}
    
    def on_image_variant_changed(self, variant):
        self.selected_image_variant = self.image_categories.get(variant, [])
        self.window.StatusLabel.setText(f"Selected variant: {variant} with {len(self.selected_image_variant)} images")
    
    def clear_images(self):
        #self.images = [] 
        self.reset_states()
        self.window.StatusLabel.setText("All images cleared. Please reset") 
        self.window.StartButton.setEnabled(False)  
        self.window.progressBar.setValue(0)  
        self.window.InputPathLineEdit.clear()
        self.window.OutputPathLineEdit.clear()
        self.window.VariantCombo.clear()
        self.window.VariantCombo.addItem("original")  # Reset to Raw variant
        self.selected_image_variant = []
        self.include_subdirs_checkbox.setEnabled(True)  # Reset to default state


    def on_prediction_done(self, predictions, class_names):
        """ Callback when prediction is done """
        try:
            for idx, block in enumerate(predictions):
                try:
                    if len(block) < 4:
                        raise ValueError(f"Expected at least 4 prediction fields, got {len(block)}")
                    model, img_name, pred_str, prob_str = block[:4]
                    model_name  = model.rstrip(":")
                    img_name = img_name.strip()
                    pred_label = pred_str.split(":", 1)[1].strip()
                    prob_val = float(prob_str.split(":", 1)[1].strip())
                    status_details = []
                    for text in block[4:]:
                        if not text:
                            continue
                        text = text.strip()
                        if text.lower().startswith("status:"):
                            text = text.split(":", 1)[1].strip()
                        status_details.append(text)
                    status = "prediction done"
                    if pred_label.lower() == "error":
                        status = "; ".join(status_details) if status_details else "Error"
                    self.results.append({
                            "image": img_name,
                            "variant": self.window.VariantCombo.currentText(),
                            "model": model_name,
                            "prediction": pred_label,
                            "probability": prob_val,
                            "status": status
                        })
                except Exception as e:
                    error_msg = f"Error processing prediction block {idx}: {str(e)}"
                    print(error_msg)
                    traceback.print_exc()
                    # Add error result
                    self.results.append({
                        "image": "unknown",
                        "variant": self.window.VariantCombo.currentText() if self.window else "unknown",
                        "model": "unknown",
                        "prediction": None,
                        "probability": None,
                        "status": f"Error: {str(e)}"
                    })
        except Exception as e:
            error_msg = f"Error in on_prediction_done: {str(e)}"
            print(error_msg)
            traceback.print_exc()
            self.fuseStatus.emit(error_msg)
        
    def startClassification(self):
        w = self.window
        if not self.images or not os.path.isdir(self.out_path):
            w.StatusLabel.setText("Please set input images and output folder")
            return
        if not self.current_models:
            self.fuseStatus.emit("Please choose at least 1 model")
            return
        
        # Validate selected image variant
        if not self.selected_image_variant:
            w.StatusLabel.setText("No image variant selected. Please select an image variant.")
            self.fuseStatus.emit("No image variant selected")
            return
        
        #total = len(self.images)
        total = len(self.selected_image_variant)
        self.results = []  # Initialize results list
        
        try:
            for i, img_path in enumerate(self.selected_image_variant, start=1):#self.images, start=1):
                img_name = Path(img_path).name
                try:
                    # has to be bound to self.signals as signals will be garbage collected too early otherwise
                    #self.signals = signals = Signals()
                    if selected_ml_models := self.classification_model_selector.currentData():
                        predictor = Predictor(img_path, selected_ml_models, self.fuseStatus, self.signals.classification_done)
                        predictor.start()
                        predictor.join()  # Wait for the thread to finish
                        #signals.classification_done.connect(lambda predictions, class_names: self.on_prediction_done(predictions, class_names))

                    status = "prediction done"
                    

                except Exception as e:
                    traceback.print_exc()
                    self.results.append({
                        "image": os.path.basename(img_path),
                        "variant": self.window.VariantCombo.currentText(),
                        "model": None,
                        "prediction": None,
                        "probability": None,
                        "status": f"Error: {e}"
                    })
                    status = f"Error: {e}"

                # Progress bar 
                pct = int(i / total * 100)
                w.progressBar.setValue(pct)
                w.StatusLabel.setText(f"Progressing {i}/{total}:{status}")
                QtWidgets.QApplication.processEvents()

        except Exception as e:
            error_msg = f"Critical error during classification: {str(e)}"
            w.StatusLabel.setText(error_msg)
            self.fuseStatus.emit(error_msg)
            traceback.print_exc()
        finally:
            # Always try to export results, even if there was an error
            if self.results:
                self.export_results()
            self.include_subdirs_checkbox.setEnabled(True)  # Re-enable the checkbox after processing

    def export_results(self):
        """ Export result and highlight the failed images """
        # Check if results exist
        if not self.results:
            self.window.StatusLabel.setText("No results to export")
            self.fuseStatus.emit("No results to export - please run classification first")
            return
        
        df = pd.DataFrame(self.results)
        
        # Check if DataFrame is empty
        if df.empty:
            self.window.StatusLabel.setText("No results to export")
            self.fuseStatus.emit("No results to export - DataFrame is empty")
            return
        
        out_path = os.path.join(self.out_path, "classification_results.xlsx")

        try:
            with pd.ExcelWriter(out_path, engine="xlsxwriter") as writer:
                df.to_excel(writer, sheet_name="Results", index=False)
                workbook  = writer.book
                worksheet = writer.sheets["Results"]

                red_fmt = workbook.add_format({
                    "font_color": "red"
                })

                # Check if 'status' column exists before trying to access it
                if "status" in df.columns:
                    status_col = df.columns.get_loc("status")

                    for row_idx, status in enumerate(df["status"], start=1):
                        if status != "prediction done":
                            worksheet.write(row_idx, status_col, status, red_fmt)
                else:
                    # If status column doesn't exist, log a warning
                    print("Warning: 'status' column not found in results DataFrame")

            self.window.StatusLabel.setText(f"Results exported to {out_path}")
            self.fuseStatus.emit(f"Results successfully exported to {out_path}")
        except Exception as e:
            error_msg = f"Error exporting results: {str(e)}"
            self.window.StatusLabel.setText(error_msg)
            self.fuseStatus.emit(error_msg)
            traceback.print_exc()
