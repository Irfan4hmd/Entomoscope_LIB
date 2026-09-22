
import os
import sys
import constants
from PyQt5.QtWidgets import (
    QApplication, QDialog, QPushButton, QLineEdit, QRadioButton, QButtonGroup,
    QFileDialog, QMessageBox, QComboBox, QLabel, QHBoxLayout,
)
from PyQt5.QtCore import pyqtSignal
from PyQt5 import uic

# "C:\Program Files\Helicon Software\Helicon Focus 8\HeliconFocus.exe"


class MethodDialog(QDialog):
    decision_made = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        uic.loadUi("UserInterface/stacking_method_dialog.ui", self)
        
        self._fuse_method = constants.LAST_FUSE_METHOD
        self._helicon_path = None

        if self._fuse_method == 0:
            self.helicon_radio_bttn.setChecked(True)
        elif self._fuse_method == 1:
            self.selfmade_radio_bttn.setChecked(True)
        else:
            self.no_stack_radio_bttn.setChecked(True)
        
        self.helicon_radio_bttn = self.findChild(QRadioButton, "helicon_radio_bttn")
        self.selfmade_radio_bttn = self.findChild(QRadioButton, "selfmade_radio_bttn")
        self.no_stack_radio_bttn = self.findChild(QRadioButton, "no_stack_radio_bttn")
        self.button_group = QButtonGroup()
        self.button_group.addButton(self.helicon_radio_bttn)
        self.button_group.addButton(self.selfmade_radio_bttn)
        self.button_group.addButton(self.no_stack_radio_bttn)
        self.helicon_radio_bttn.toggled.connect(self.method_toggled)
        self.selfmade_radio_bttn.toggled.connect(self.method_toggled)
        self.no_stack_radio_bttn.toggled.connect(self.method_toggled)
        
        self.ok_bttn = self.findChild(QPushButton, "ok_bttn")
        self.cancel_bttn = self.findChild(QPushButton, "cancel_bttn")
        self.ok_bttn.clicked.connect(self.ok_clicked)
        self.cancel_bttn.clicked.connect(self.close)
        
        self.helicon_path_lineedit = self.findChild(QLineEdit, "helicon_path_lineedit")
        self.browse_bttn = self.findChild(QPushButton, "browse_bttn")
        self.browse_bttn.clicked.connect(self.browse_path)

        # This persisted setting controls every final stack created after the
        # choice, including in the next ENIMAS session.
        output_layout = QHBoxLayout()
        output_label = QLabel("Final stacked image format:")
        self.output_format_combo = QComboBox()
        self.output_format_combo.addItem("TIFF (.tiff) - lossless, recommended", "tiff")
        self.output_format_combo.addItem("JPEG (.jpg) - database-friendly, quality 95", "jpg")
        current = getattr(constants, "STACK_OUTPUT_EXTENSION", "tiff")
        selected = self.output_format_combo.findData(current)
        self.output_format_combo.setCurrentIndex(max(0, selected))
        self.output_format_combo.setToolTip(
            "Only the final stacked image changes format. Raw stack frames remain TIFF."
        )
        output_layout.addWidget(output_label)
        output_layout.addWidget(self.output_format_combo, 1)
        self.verticalLayout_2.insertLayout(1, output_layout)

        self.auto_find_path()

    def auto_find_path(self):
        stdpath = r"C:\Program Files\Helicon Software\Helicon Focus 8\HeliconFocus.exe"
        if os.path.isfile(stdpath):
            self.helicon_path_lineedit.setText(stdpath)

    
    def browse_path(self):
        response = QFileDialog.getOpenFileName(
            parent=self,
            caption="Select the path to the Helicon Focus software executable (.exe)",
            directory=r"C:\Program Files",
            filter="Executable file (*.exe)"
        )
        
        self._helicon_path=response[0] 
        self.helicon_path_lineedit.setText(self._helicon_path)
        
    def method_toggled(self, bttn):
        bttn: QRadioButton = self.sender()
        if bttn.isChecked():
            if bttn == self.helicon_radio_bttn:
                self._fuse_method = 0
            elif bttn == self.selfmade_radio_bttn:
                self._fuse_method = 1
            elif bttn == self.no_stack_radio_bttn:
                self._fuse_method = 2
    
    @staticmethod
    def check_helicon_path(path):
        if os.path.basename(path) != "HeliconFocus.exe" and os.path.isdir(path) and "HeliconFocus.exe" in os.listdir(path):
            # the parent folder was selected
            path = os.path.join(path, "HeliconFocus.exe")
            
        if not path or not os.path.isfile(path) or not "HeliconFocus.exe" in str(path):
            return False
            
        return path
    
    def ok_clicked(self):
        constants.STACK_OUTPUT_EXTENSION = self.output_format_combo.currentData()
        if self._fuse_method == 0:
            path = self.helicon_path_lineedit.text()
            
            path = self.check_helicon_path(path)
            
            if not path:
                QMessageBox.warning(self, "Helicon path is invalid", "The Helicon executable file path is invalid.\nThe program will change to selfmade stacking method.")
                self._fuse_method = 1
                constants.LAST_FUSE_METHOD = self._fuse_method
                self.helicon_radio_bttn.setChecked(False)
                self.selfmade_radio_bttn.setChecked(True)
                self.close()
                self.decision_made.emit("None")
                return 
            
            constants.LAST_FUSE_METHOD = self._fuse_method
            self.close()
            self.decision_made.emit(path)
        
        elif self._fuse_method == 1:
            constants.LAST_FUSE_METHOD = self._fuse_method
            self.close()
            self.decision_made.emit("None")
        
        elif self._fuse_method == 2:
            # Do not stack - just save images without stacking
            constants.LAST_FUSE_METHOD = self._fuse_method
            self.close()
            self.decision_made.emit("NoStack")


def prompt_user_fuse_mode():
    method_selection_interface = MethodDialog()
    method_selection_interface.decision_made.connect(lambda path: constants.__setattr__("HELICON_PATH", path))
    method_selection_interface.exec()

