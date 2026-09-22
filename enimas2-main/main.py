import sys
import multiprocessing
import atexit
import json
import os
from pathlib import Path
from time import sleep, time


def _early_installation_guard():
    """Fail closed before importing hardware/application modules from a mixed tree."""
    if os.environ.get("ENIMAS_HEALTH_CHECK") == "1":
        return
    source_root = Path(__file__).resolve().parent
    install_root = source_root.parent if source_root.name.lower() == "src" else source_root
    updates = install_root / "updates"
    state_files = (
        (updates / "install_state.json", {"complete", "rolled_back"}),
        (
            updates / "transaction.json",
            {"complete", "rolled_back", "support_rollback_complete"},
        ),
    )
    if (updates / "update.lock").exists():
        print(
            "An ENIMAS update is active. Wait for it to finish or run install.bat "
            "to recover an interrupted update.",
            file=sys.stderr,
        )
        raise SystemExit(42)
    for state_path, terminal_phases in state_files:
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(
                f"ENIMAS installation state is unreadable ({state_path.name}): {exc}. "
                "Run install.bat --repair.",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        if state and state.get("phase") not in terminal_phases:
            print(
                f"ENIMAS installation is not ready ({state_path.name}: "
                f"{state.get('phase', 'unknown')}). Run install.bat to resume recovery.",
                file=sys.stderr,
            )
            raise SystemExit(42)


_early_installation_guard()

from serial import Serial
from serial.tools.list_ports import comports
from Arducam.Arducam import ArducamCamera
from VAImagingcam.VAImagingcam import VAImagingCamera
from Tools.cropping_boxsegmenter import cleanup_resources
from Tools.enimas_updater import (
    UpdateActiveError,
    acquire_app_lock,
    ensure_installation_is_ready,
    recover_interrupted_update,
    release_app_lock,
)
from Tools.update_manager import UpdateController
import ArducamSDK  # For device rescanning

from error_handler import global_except_hook

from logging import basicConfig, getLogger, DEBUG, WARNING
basicConfig(filename="debug.log", filemode="w+", level=DEBUG)

logger = getLogger()
logger.setLevel(DEBUG)
getLogger('matplotlib').setLevel(WARNING)

from PyQt5.QtWidgets import QApplication, QPushButton, QDialog, QMessageBox, QLayout, QHBoxLayout
from PyQt5.QtCore import Qt
from PyQt5 import uic
from PyQt5.QtGui import QFontMetrics

from UserInterface.ui import MainWindow
from MotorController.motorcontroller import Axis
import constants
from Tools.user_preferences import load_output_preferences, store_output_preferences
import faulthandler
faulthandler.enable()


def read_config():
    with open("config.txt", "r") as config_file:
        config = {} 
        for line in config_file.readlines():
            if line.startswith("#") or "=" not in line:
                continue
            
            prop, value = line.split("=", 1)
            prop, value = prop.rstrip().lstrip(), value.rstrip().lstrip()
            if not prop or not value: continue
            
            config[prop] = value
    return config

def write_config(params: dict):
    if not "version" in params:
        params["version"] = "1.0.0"
    
    params["helicon-path"] = constants.HELICON_PATH
    params["base-directory"] = constants.DEFAULT_BASE_DIR
    params["image-extension"] = constants.IMG_EXTENSION
    params["stack-speed"] = constants.STACK_SPEED
    params["number-of-stacks"] = constants.DEFAULT_NUM_OF_STACKS
    params["stack-step-size"] = constants.DEFAULT_STACK_STEP_SIZE
    store_output_preferences(params)
    
    with open("config.txt", "w") as config_file:
        for key, val in params.items() :
            config_file.write(f"{key}={val}\n")

def save_config(cfg: dict):
    """saves config params as python attributes"""
    load_output_preferences(cfg)
    try:
        constants.__setattr__("IMG_EXTENSION", cfg["image-extension"]) if "image-extension" in cfg else None
        helicon_path = cfg["helicon-path"] if "helicon-path" in cfg else "undefined"
        constants.__setattr__("HELICON_PATH", helicon_path)
        constants.__setattr__("STACK_SPEED", int(cfg["stack-speed"])) if "stack-speed" in cfg else None
        constants.__setattr__("DEFAULT_NUM_OF_STACKS", int(cfg["number-of-stacks"])) if "number-of-stacks" in cfg else None
        constants.__setattr__("DEFAULT_STACK_STEP_SIZE", float(cfg["stack-step-size"])) if "stack-step-size" in cfg else None
        constants.__setattr__("DEFAULT_BASE_DIR", cfg["base-directory"]) if "base-directory" in cfg else None
    except:
        # parameter is missing or ValueError, this invalid data can be ignored
        pass

def open_camera_arducam() -> ArducamCamera:
    camera = ArducamCamera()

    # Check if any devices were detected during initialization
    if camera.dev_num == 0:
        logger.warning("No Arducam devices detected during scan")
        # Try rescanning a few times in case device was just plugged in
        max_scans = 5
        for i in range(max_scans):
            logger.info(f"Rescanning for Arducam devices (attempt {i+1}/{max_scans})...")
            sleep(1.0)  # Wait 1 second between scans
            try:
                camera.dev_num = ArducamSDK.Py_ArduCam_scan()
                if camera.dev_num > 0:
                    logger.info(f"Found {camera.dev_num} Arducam device(s) after rescan")
                    break
            except Exception as e:
                logger.error(f"Error during rescan: {e}")

        if camera.dev_num == 0:
            logger.error("Failed to detect Arducam after multiple scans")
            return None

    cfg_file = "Arducam/IMX477_2Lane_4032x3040_RAW8_A.cfg"

    # Try to open the camera with retries
    max_retries = 3
    retry_delay = 1.0

    for attempt in range(max_retries):
        logger.info(f"Opening Arducam camera (attempt {attempt+1}/{max_retries})...")
        opened = camera.openCamera(cfg_file)

        if opened:
            logger.info("Arducam camera opened successfully, starting capture...")
            try:
                camera.start()
                return camera
            except Exception as e:
                logger.error(f"Failed to start Arducam camera: {e}")
                return None
        else:
            if attempt < max_retries - 1:
                logger.warning(f"Failed to open Arducam, retrying in {retry_delay}s...")
                sleep(retry_delay)
            else:
                logger.error("Failed to open Arducam after all retries")
                return None

    return None

def open_camera_vaimagingcam() ->VAImagingCamera:
    camera = VAImagingCamera()

    if camera.dev_num == 0:
        return None
    
    camera.openCamera()
    camera.start()
    return camera

def open_camera():
    cam = open_camera_arducam()
    if cam:
        return cam, "ArducamCamera"
    
    cam = open_camera_vaimagingcam()
    if cam:
        return cam, "VAImagingCamera"
    
    logger.error("Failed to open camera.")
    return None, None

# Known Arduino-compatible USB Vendor IDs
ARDUINO_USB_VIDS = {
    "2341",  # Arduino official
    "2A03",  # Arduino.org (older boards)
    "1A86",  # QinHeng CH340/CH341 (common clones)
    "0403",  # FTDI FT232
    "10C4",  # Silicon Labs CP210x
    "1B4F",  # SparkFun
}


def find_arduino() -> Serial:
    list_of_ports = list(comports())
    logger.debug(f"COM Ports: {list(map(str, list_of_ports))}")

    if not list_of_ports:
        return None

    # Filter out Bluetooth serial ports — they are never Arduino devices
    usb_ports = [(p, d, h) for p, d, h in list_of_ports
                 if not h.upper().startswith("BTHENUM")]
    skipped = len(list_of_ports) - len(usb_ports)
    if skipped:
        logger.debug(f"Filtered out {skipped} Bluetooth port(s)")

    if not usb_ports:
        logger.warning("No USB serial ports found (all ports are Bluetooth)")
        return None

    # Stage 1: Fast path — check for 'Arduino' in port description
    for port, desc, hwid in usb_ports:
        if "Arduino" in desc:
            logger.debug(f"Found Arduino by description: {port}")
            try:
                return Serial(port, baudrate=115200, timeout=.05)
            except Exception as e:
                logger.warning(f"Port {port} matched by description but failed to open: {e}")

    # Stage 2: Prioritize ports with known Arduino USB Vendor IDs
    prioritized = []
    other = []
    for port, desc, hwid in usb_ports:
        hwid_upper = hwid.upper()
        if any(f"VID_{vid}" in hwid_upper for vid in ARDUINO_USB_VIDS):
            prioritized.append((port, desc, hwid))
        else:
            other.append((port, desc, hwid))

    if prioritized:
        logger.debug(f"Found {len(prioritized)} port(s) with known Arduino VIDs")

    # Stage 3: Handshake — try prioritized ports first, then others
    for port, desc, hwid in prioritized + other:
        logger.debug(f"{port=}, {desc=}, {hwid=}")
        try:
            con = Serial(port, baudrate=115200, timeout=.05)
            sleep(2)
            logger.debug(f"written to {port}")
            t0 = time()

            con.write(b"arduino")
            while time() - t0 < .1:
                ans = con.readline()
                logger.debug(f"answer: {ans}")

                if ans.decode("ascii").strip() == "yes":
                    logger.debug(f"{port} won")
                    return con

            con.close()

        except Exception as e:
            logger.debug(f"error while trying to connect to a COM port: {e}")

    logger.error("Failed to connect to the Arduino.")
    return None


class StartWindow(QDialog):
    def __init__(self):
        super().__init__()
        uic.loadUi("UserInterface/logo_screen.ui", self)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)


class NoConnectionWindow(QDialog):
    def __init__(self):
        super().__init__()
        uic.loadUi("UserInterface/connection_error.ui", self)
        self.retry_bttn = self.findChild(QPushButton, "retry_bttn")
        self.close_bttn = self.findChild(QPushButton, "close_bttn")
        self.layout = self.findChild(QHBoxLayout, "horizontalLayout")
        if self.layout:
            self.layout.setSizeConstraint(QLayout.SetMinimumSize)

        self.proceed = False

        if self.close_bttn:
            self.close_bttn.setText("continue without connection")
            fm = QFontMetrics(self.close_bttn.font())
            text_width = fm.width(self.close_bttn.text())
            padding = 150  
            self.close_bttn.setMinimumWidth(text_width + padding)
            self.close_bttn.clicked.connect(self.continue_without_camera)

        if self.retry_bttn:
            self.retry_bttn.clicked.connect(self.close)
        
    def continue_without_camera(self):
        self.proceed = True
        self.accept() #close the dialog

        #self.close_bttn.clicked.connect(exit)
        




if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.excepthook = global_except_hook
    source_root = Path(__file__).resolve().parent
    install_root = source_root.parent if source_root.name.lower() == "src" else source_root

    app = QApplication(sys.argv)
    try:
        ensure_installation_is_ready(install_root)
        recover_interrupted_update(install_root)
    except UpdateActiveError:
        QMessageBox.information(
            None,
            "ENIMAS Update",
            "An ENIMAS update is currently in progress. Please wait for it to finish.",
        )
        sys.exit(42)
    except Exception as exc:
        logger.exception("Could not recover an interrupted update")
        QMessageBox.critical(
            None,
            "ENIMAS Recovery Required",
            "ENIMAS could not safely recover an interrupted update.\n\n"
            f"No new session was started. Details: {exc}\n\n"
            "Run install.bat --repair or contact support.",
        )
        sys.exit(1)

    if not acquire_app_lock(install_root):
        QMessageBox.information(
            None,
            "ENIMAS Already Running",
            "Another ENIMAS session is already running, or an update is being applied.",
        )
        sys.exit(42)
    atexit.register(release_app_lock, install_root)

    cfg = read_config()

    # Sync version from code (ensures correct version after updates)
    cfg["version"] = constants.APP_VERSION

    if "base-directory" in cfg:
        if not os.path.isdir(cfg["base-directory"]):
            del cfg["base-directory"]
    save_config(cfg)

    start_window = StartWindow()
    start_window.show()
    
    arduino = find_arduino()
    camera, camera_type = open_camera()
    connection_window = None
    
    while arduino is None or camera is None:
    # while arduino is None and camera is None:
        connection_window = NoConnectionWindow()
        connection_window.exec()

        if connection_window.proceed:
            break

        arduino = find_arduino() if arduino is None else arduino
        # camera = open_camera() if camera is None else camera
        if camera is None:
            camera, camera_type = open_camera()
            if camera is None:
                print("Camera not opened")
                logger.error("camera not opened")
                sys.exit(1)
    
    if connection_window:
        connection_window.close()
    start_window.close()
    
    axis = Axis(arduino)
    # print(arduino)
    window = MainWindow(axis, camera, cam_type = camera_type)
    window.show()
    # Persist the code version only after the main window has started successfully.
    write_config(cfg)
    update_controller = UpdateController(window, install_root=install_root)
    update_controller.start()
    rtn = app.exec()
    logger.info("app executed")
    write_config(cfg)

    if rtn == 42:
        # The window close lifecycle has already stopped the camera, motor, and
        # active workers. Do not repeat global model cleanup or wait on stale
        # non-daemon resources while the verified external updater is waiting.
        logger.info("Completing ENIMAS update handoff with exit code 42")
        release_app_lock(install_root)
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        for handler in logger.handlers:
            try:
                handler.flush()
            except Exception:
                pass
        os._exit(42)

    # Clean up resources to prevent handle leaks
    try:
        cleanup_resources()
        logger.info("Resources cleaned up successfully")
    except Exception as e:
        logger.error(f"Error during resource cleanup: {e}")
    
    release_app_lock(install_root)
    sys.exit(rtn)
