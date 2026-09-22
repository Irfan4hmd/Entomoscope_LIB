import os
os.environ['PYTHONUNBUFFERED'] = '1'   # disable assertion failed window
import psutil
import cv2
import numpy as np
from re import escape, search
import shutil
import requests
import gxipy as gx
from sip import isdeleted # type: ignore
from serial import Serial
from serial.tools.list_ports import comports
from PIL import Image
from pathlib import Path
from typing import Tuple, List
from numpy import average
from json import load, dump
from threading import Thread
from time import monotonic, time, sleep
from datetime import datetime
import threading
from dataclasses import dataclass
from copy import deepcopy
from ultralytics import YOLO
from refiners.solutions import BoxSegmenter
from logging.config import dictConfig
from logging import getLogger, DEBUG, INFO
import logging
from multiprocessing import Queue
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from queue import Empty
from process_cleanup import safe_shutdown_pool

from PyQt5.QtWidgets import (QApplication, QMainWindow, QLineEdit, QPushButton, QLabel, QStatusBar, QAction,QGraphicsDropShadowEffect,
                            QDialog, QTreeView, QFileSystemModel, QFileDialog, QMessageBox, QTabWidget, QActionGroup,
                            QSlider, QListWidgetItem, QListWidget, QSpinBox, QDoubleSpinBox, QMenu, QCheckBox,
                            QVBoxLayout, QHBoxLayout, QDialogButtonBox, QWidget, QSizePolicy, QComboBox, QWidgetAction, QScrollArea, QStyle, QLayout, QInputDialog)
from PyQt5.QtGui import QIcon, QImage, QPixmap, QFont, QColor, QMovie
from PyQt5.QtCore import Qt, QThread, QObject, pyqtSignal, pyqtBoundSignal, pyqtSlot, QEvent, QPointF, QSize, QTimer, QMetaObject, Q_ARG, QSignalBlocker
from PyQt5 import uic



# disable all PyQt loggers
dictConfig({"version": 1, "disable_existing_loggers": True})

from Arducam.Arducam import ArducamCamera
from Arducam.ImageConvert import convert_image_arducam

import constants
import Tools.stacker as stacker
from Tools.stack_capture import (
    StackCaptureCancelled,
    StackCaptureError,
    capture_direct_with_paused_preview,
    capture_stack_sequence,
    remaining_direct_discard_frames,
    wait_for_new_preview_frame,
)
from Tools.image_output import (
    SUPPORTED_IMAGE_EXTENSIONS,
    ScaleBarError,
    add_scale_bar,
    calculate_mm_per_pixel,
    convert_image_file,
    is_valid_scale,
    normalize_image_extension,
)
from Tools.ml_models import Predictor, load_models, YOLO_MODELS
from Tools.zenodo_uploader import Uploader
from MotorController.motorcontroller import Axis
from . import method_selection
from UserInterface.InteractiveStaticLabel import InteractiveStaticLabel 
from UserInterface.checkable_combo_box import CheckableComboBox
from OBB.obb_measurement_final import DetectionModel, MeasurementUtils, ImageUtils
from PluginBase import PluginBase
from UserInterface.plugin_manager import PluginManager
from UserInterface.Image_save_settings import ImageSettingsDialog, cropping, find_onnx_models
from Background_remover.background_remover import remove_backgound
from Tools.cropping_boxsegmenter import crop_image


from VAImagingcam.VAImagingcam import VAImagingCamera
from VAImagingcam.ImageConvert import convert_image

# import faulthandler


def calc_focus_score(image):
    """Compute a robust focus score for specimens of varying size and position.

    The score uses a tile-based gradient metric instead of a single full-frame
    Laplacian variance. This makes autofocus less sensitive to background,
    bubbles, container reflections, and specimens that only occupy a small part
    of the image.
    """
    if image is None or image.size == 0:
        return 0.0

    height, width = image.shape[:2]
    max_dim = 1400
    scale = min(1.0, max_dim / max(height, width))
    if scale < 1.0:
        resized = cv2.resize(
            image,
            (max(64, int(width * scale)), max(64, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        resized = image

    if len(resized.shape) == 3:
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    else:
        gray = resized

    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    sobel_x = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    grad_energy = sobel_x * sobel_x + sobel_y * sobel_y
    laplacian = cv2.Laplacian(blurred, cv2.CV_32F, ksize=3)

    tile_h = max(48, blurred.shape[0] // 6)
    tile_w = max(48, blurred.shape[1] // 6)
    tile_scores = []
    all_tile_scores = []

    for y in range(0, blurred.shape[0], tile_h):
        for x in range(0, blurred.shape[1], tile_w):
            y_end = min(y + tile_h, blurred.shape[0])
            x_end = min(x + tile_w, blurred.shape[1])
            if (y_end - y) < 24 or (x_end - x) < 24:
                continue

            tile_gray = blurred[y:y_end, x:x_end]
            tile_grad = grad_energy[y:y_end, x:x_end]
            tile_lap = laplacian[y:y_end, x:x_end]

            tenengrad = float(tile_grad.mean())
            lap_var = float(tile_lap.var())
            tile_score = tenengrad + 0.35 * lap_var
            all_tile_scores.append(tile_score)

            contrast = float(tile_gray.std())
            saturation_ratio = float(((tile_gray <= 4) | (tile_gray >= 251)).mean())
            if contrast < 3.0 or saturation_ratio > 0.25:
                continue

            tile_scores.append(tile_score)

    active_scores = tile_scores if tile_scores else all_tile_scores
    if active_scores:
        active_scores.sort(reverse=True)
        top_n = max(1, min(8, int(np.ceil(len(active_scores) * 0.2))))
        return float(np.mean(active_scores[:top_n]))

    return float(grad_energy.mean() + 0.35 * laplacian.var())


logger = getLogger("UserInterface")
logger.setLevel(DEBUG)
logging.getLogger("PIL").setLevel(logging.WARNING)

total_cores = psutil.cpu_count(logical=True)
ui_reserved = max(1, round(total_cores * 0.3))
usable = list(range(total_cores - ui_reserved))

detect_n = max(1, round(total_cores * 0.3))
variant_n = max(1, round(total_cores * 0.3))

max_usable = len(usable)
if variant_n + detect_n > max_usable:
    overflow = variant_n + detect_n - max_usable
    detect_n = max(1, detect_n - overflow)
    if variant_n + detect_n > max_usable:
        variant_n = max_usable - detect_n

detect_cores = usable[:detect_n]
variant_cores = usable[detect_n:detect_n + variant_n]
# print(f"Usable cores: {usable}, Variant cores: {variant_cores}, Detect cores: {detect_cores}")

current_dir = os.path.dirname(__file__)
root = os.path.abspath(os.path.join(current_dir, os.pardir))
measurement_model_path = os.path.join(root,'models','obb','yolov8m_obb3_best.onnx')


cropping_dir = os.path.join(root,'models','cropping')
onnx_models = find_onnx_models(Path(cropping_dir))
YOLO_MODELS['crop'] = YOLO(onnx_models[0], task="detect") if onnx_models is not None else None
cropping_model: YOLO = YOLO_MODELS["crop"]
DETECT_POOL = None

def _init_detect_worker(cpu_cores, nice_level, model_path):
    """
    Initialize the detection worker with the specified CPU cores and nice level.
    """
    p = psutil.Process(os.getpid())
    p.cpu_affinity(cpu_cores)
    p.nice(nice_level)
    global detector
    detector = DetectionModel(model_path)

def detect_task(img_path):
    try:
        if detector is None:
            print("Warning: Detector not initialized")
            return None
        return detector.perform_detection(img_path, padding_percentage=0.1)
    except Exception as e:
        print(f"Error in detection task: {e}")
        import traceback
        traceback.print_exc()
        return None

def make_detect_pool(_detect_cores, model_path):
    print("Loading detection model...")
    try:
        return ProcessPoolExecutor( 
            max_workers=1,
            initializer=_init_detect_worker,
            initargs=(_detect_cores, psutil.BELOW_NORMAL_PRIORITY_CLASS, model_path))
    except Exception as e:
        print(f"Error creating detection pool: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_detect_pool():
    global DETECT_POOL
    if DETECT_POOL is None:
        DETECT_POOL = make_detect_pool(detect_cores, measurement_model_path)
    return DETECT_POOL

class DetectionWorker(QObject):
    started = pyqtSignal()
    finished = pyqtSignal(object)
    aborted = pyqtSignal(str)

    def __init__(self, img_path, parent=None):
        super().__init__(parent)
        self.img_path = img_path
        self.future = None
        self._abort = False
        self._done = False
        
    def start(self):
        self._abort = False
        self._done = False
        self.started.emit()
        print(f"Starting detection worker with image: {self.img_path}")

        try:
            pool = get_detect_pool()
            future = pool.submit(detect_task, self.img_path)
            self.future = future
            future.add_done_callback(self._on_finished)
        except Exception as e:
            logger.error(f"Error starting detection worker: {e}")
            self.aborted.emit(f"Detection error: {str(e)}")
            self._cleanup()

    def abort(self):
        if self._done:
            return
            
        self._abort = True
        try:
            if self.future and not self.future.done():
                cancelled = self.future.cancel()
                logger.info(f"Detection cancelled, result: {cancelled}")
        except Exception as e:
            logger.error(f"Error cancelling detection: {e}")
        
        self.aborted.emit("Detection aborted.")
        self._cleanup()

    def _on_finished(self, future):
        if self._abort or self._done:
            return
            
        try:
            result = future.result()
            self.finished.emit(result)
        except Exception as e:
            logger.error(f"Error in detection: {e}")
            self.aborted.emit(f"Detection error: {str(e)}")
        finally:
            self._cleanup()
    
    def _cleanup(self):
        """Clean up resources to prevent handle leaks"""
        if self._done:
            return
            
        self._done = True
        self.future = None
        
        # Force garbage collection to help release resources
        import gc
        gc.collect()

new_button_style = """
QPushButton{
	background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                stop: 0 #F0F0F0, stop: 0.8 #DCDCDC,
                                stop: 0.9 #DCDCDC, stop: 1.0 #C9C9C9);
    font-size: 13px; font-weight: bold; border-width: 1;border-radius: 10px;
	border-style: solid; border-color: rgb(201, 201, 201); padding: 4px;
}

QPushButton:hover{
	border-color: rgb(128, 128, 128); /*rgb(0, 145, 117)*/
	background-color: rgb(128, 128, 128); color: white;
}
"""

def isfloat(string: str) -> bool:
    try:
        float(string)
        return True
    except ValueError:
        return False


def lens_scale_for_camera(lens, camera_type):
    """Return a validated mm/pixel scale, or None when not calibrated."""
    attribute = {
        "VAImagingCamera": "scale_vaimaging",
        "ArducamCamera": "scale_arducam",
    }.get(camera_type)
    value = getattr(lens, attribute, None) if attribute else None
    return float(value) if is_valid_scale(value) else None


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
    scale_vaimaging: float = None
    scale_arducam: float = None

    @property
    def label_str(self):
        return f"{self.name} - {self.lenght}mm"
    
DEVICE_CAMERAS = {
    "Entomoscope PI": ArducamCamera,
    "Entomoscope PIs": ArducamCamera,
    "Entomoscope PI2AI": VAImagingCamera,
    }

def process_variant_task(img_path: str,
                         save_uniform_bg: bool,
                         replace_color,
                         do_cropping: bool,
                         cropping_method,
                         progress_queue: Queue) -> dict:
    results = {'Original': img_path}
    if save_uniform_bg:
        progress_queue.put("Processing uniform background...")
        try: 
            uniformBG = remove_backgound(img_path, None, "cpu", replace_color, None, True, False)
            if uniformBG and os.path.exists(uniformBG):
                results['Uniform'] = uniformBG
            else:
                logger.error(f"Uniform background image failed")
        except Exception as e:
            logger.exception(f"Error processing uniform background: {e}")
        progress_queue.put("Uniform background processed.")
    
    if do_cropping:
        progress_queue.put("Processing cropping...")
        if cropping_method is None:
            cropping_method = 0 
        try:
            # Safeguard against Boost thread assertion errors
            try:
                # Import necessary modules here to avoid circular imports
                import gc
                
                # First try running cropping
                if cropping_method == 0:
                    logger.debug("Using YOLO cropping")
                    cropped = cropping(img_path, cropping_model)
                elif cropping_method == 1:
                    logger.debug("Using BoxSegmenter cropping")
                    # Add extra error handling for the problematic BoxSegmenter
                    try:
                        cropped = crop_image(img_path, show_images=False)
                    except AssertionError as ae:
                        logger.error(f"BoxSegmenter assertion error: {ae}")
                        # Force cleanup and try again
                        from Tools.cropping_boxsegmenter import cleanup_resources
                        cleanup_resources()
                        gc.collect()
                        # Fall back to YOLO cropping
                        logger.info("Falling back to YOLO cropping after BoxSegmenter error")
                        cropped = cropping(img_path, cropping_model)
                
                # Check the result
                if cropped and os.path.exists(cropped):
                    results['Cropped'] = cropped
                else:
                    logger.error(f"Cropped image failed")
            except Exception as inner_e:
                logger.exception(f"Inner error during cropping: {inner_e}")
                # Force garbage collection
                gc.collect()
        except Exception as e:
            logger.exception(f"Error processing cropping: {e}")
            # Force garbage collection
            gc.collect()
        
        # Process uniform background cropping if available
        if save_uniform_bg and 'Uniform' in results:
            try:
                # Use the more reliable YOLO cropping for the uniform background
                cropped_uniform = cropping(results['Uniform'], cropping_model)
                if cropped_uniform:
                    results['Cropped_Uniform'] = cropped_uniform
                else:
                    logger.error(f"Cropping for uniform background image failed")
            except Exception as e:
                logger.exception(f"Error cropping uniform background image: {e}")
        
        progress_queue.put("Cropping processed.")
    return results

class VariantWorker(QObject):
    started = pyqtSignal()
    finished = pyqtSignal(dict)
    aborted = pyqtSignal()
    progress = pyqtSignal(str)

    def __init__(self, img_path, save_uniform_bg, replace_color, do_cropping, cropping_model, cpu_cores, nice_level, parent=None):  
        super().__init__()
        self.img_path = img_path
        self.save_uniform_bg = save_uniform_bg
        self.replace_color = replace_color
        self.cropping = do_cropping
        self.cropping_model = cropping_model
        self.cpu_cores = cpu_cores
        self.nice_level = nice_level
        self._progress_queue = None
        self._result_queue = None

        self._done = False

        self._proc = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_queues)

    def start(self):
        if self._proc and self._proc.is_alive():
            return
        self.started.emit()
        self._progress_queue = Queue()
        self._result_queue = Queue()

        self._done = False

        self._proc = multiprocessing.Process(
            target = self._run_in_subprocess,
            args=(self.img_path,
                  self.save_uniform_bg,
                  self.replace_color,
                  self.cropping,
                  self.cropping_model,
                  self._progress_queue,
                  self._result_queue,
                  self.cpu_cores,
                  self.nice_level))
        self._proc.start()
        self.timer.start(200)  # Poll every 100 ms
        self.progress.emit("Processing variant image...")
    
    def cleanup(self):
        """Cleanup resources after process completion or abortion."""
        if self._done:
            return
        
        self._done = True
        self.timer.stop()
        
        # Terminate the process if it's still running
        if self._proc and self._proc.is_alive():
            try:
                self._proc.terminate()
                self._proc.join(timeout=1)
                if self._proc.is_alive():
                    # Force kill if still running
                    self._proc.kill()
                    self._proc.join(timeout=0.5)
            except Exception as e:
                logger.error(f"Error terminating process: {e}")

        # Clean up the queues
        try:
            if self._progress_queue:
                # Drain the queue first to prevent deadlocks
                while True:
                    try:
                        self._progress_queue.get_nowait()
                    except:
                        break
                self._progress_queue.close()
                try:
                    self._progress_queue.join_thread()
                except Exception as e:
                    logger.error(f"Error joining progress queue thread: {e}")
        except Exception as e:
            logger.error(f"Error cleaning up progress queue: {e}")
        finally:
            self._progress_queue = None

        try:
            if self._result_queue:
                # Drain the queue first to prevent deadlocks
                while True:
                    try:
                        self._result_queue.get_nowait()
                    except:
                        break
                self._result_queue.close()
                try:
                    self._result_queue.join_thread()
                except Exception as e:
                    logger.error(f"Error joining result queue thread: {e}")
        except Exception as e:
            logger.error(f"Error cleaning up result queue: {e}")
        finally:
            self._result_queue = None

        self._proc = None

    def abort(self):
        if self._proc and self._proc.is_alive():
            self.aborted.emit()
            self.cleanup()


    def _poll_queues(self):
        """Poll the progress and result queues for updates."""
        if self._done:
            return
        
        # Check if process is still alive before accessing queues
        if self._proc is None or not self._proc.is_alive():
            if self._result_queue:
                try:
                    if not self._result_queue.empty():
                        results = self._result_queue.get_nowait()
                        self.finished.emit(results)
                    else:
                        self.finished.emit({'error': 'Process terminated unexpectedly'})
                except (Empty, OSError, IOError) as e:
                    logger.error(f"Error getting results before cleanup: {e}")
                    self.finished.emit({'error': f'Error retrieving results: {e}'})
            else:
                self.finished.emit({'error': 'Process terminated unexpectedly'})
            
            self.cleanup()
            return
        
        # Process progress queue
        if self._progress_queue:
            while True:
                try:
                    message = self._progress_queue.get_nowait()
                    self.progress.emit(message)
                except Empty:
                    break
                except (OSError, IOError) as e:
                    logger.error(f"Error while polling progress queue: {e}")
                    self.aborted.emit()
                    self.cleanup()
                    return

        # Process result queue
        if self._result_queue:
            try:
                if not self._result_queue.empty():
                    results = self._result_queue.get_nowait()
                    self.finished.emit(results)
                    self.cleanup()
                    return
            except (Empty, OSError, IOError) as e:
                logger.error(f"Error while polling result queue: {e}")
                self.cleanup()
                return
            # self._proc.join()  # Ensure the process has finished
            # self._proc = None
            

    @staticmethod
    def _run_in_subprocess(img_path, save_uniform_bg, replace_color, do_cropping, cropping_model, progress_queue, result_queue, cpu_cores, nice_level):
        """
        This method runs in a separate process to handle the image processing.
        """
        try:
            p = psutil.Process(os.getpid())
            p.cpu_affinity(cpu_cores)
            p.nice(nice_level)

            try:
                results = process_variant_task(img_path, save_uniform_bg, replace_color, do_cropping, cropping_model, progress_queue)
                result_queue.put(results)
            except Exception as e:
                logger.exception(f"Error in variant processing: {e}")
                result_queue.put({'error': str(e)})
        except Exception as e:
            logger.exception(f"Fatal error in subprocess: {e}")
            try:
                result_queue.put({'error': f'Fatal error: {str(e)}'})
            except:
                pass
        # Don't close queues here - they should be closed in the parent process

class MessageBox():
    message_box: QMessageBox = None

    @staticmethod
    def information(text: str, abort=False, callback=None):
        MessageBox.message_box = QMessageBox()
        MessageBox.message_box.setWindowTitle("Information")
        MessageBox.message_box.setWindowIcon(QIcon(r"UserInterface\imgs\enimas_icon.png"))
        MessageBox.message_box.setIcon(QMessageBox.Icon.Information)
        MessageBox.message_box.setText(text)
        if abort:
            MessageBox.message_box.setStandardButtons(QMessageBox.StandardButton.Abort)
            MessageBox.message_box.buttonClicked.connect(callback)
        MessageBox.message_box.show()
        QApplication.processEvents()

    @staticmethod
    def hide_message():
        if MessageBox.message_box:
            MessageBox.message_box.done(1)
            MessageBox.message_box = None
            QApplication.processEvents()

class MainWindow(QMainWindow):
    fuse_status = pyqtSignal(str)
    hide_message_signal = pyqtSignal()
    show_connection_error = pyqtSignal()
    autofocus_error_signal = pyqtSignal(str)
    autofocus_finished_signal = pyqtSignal()
    lens_reposition_error_signal = pyqtSignal(str)
    lens_reposition_finished_signal = pyqtSignal()
    single_capture_ready_signal = pyqtSignal(object)
    single_capture_error_signal = pyqtSignal(str)
    manual_move_error_signal = pyqtSignal(str)
    manual_move_finished_signal = pyqtSignal()
    _last_image_path=''
    
    def __init__(self, axis: Axis, camera, cam_type):
        super().__init__()
        logger.debug("Starting interface")
        uic.loadUi("UserInterface/main.ui", self)
        self._preferred_size = QSize(1430, 1100)  # desired working size
        self._minimum_hint = self._preferred_size
        self._initial_size_set = False
        self._min_size_set = False
        self.setMinimumSize(self._minimum_hint)
        if self.centralWidget():
            self.centralWidget().setMinimumSize(0, 0)

        # Allow shrinking below the layout's computed minimums so we can stay inside small screens
        central_layout = self.centralWidget().layout()
        if central_layout:
            central_layout.setSizeConstraint(QLayout.SetNoConstraint)

        self.axis: Axis = axis
        self.camera = camera 
        self.camera_type = cam_type  

        self.axis_length = 0
        self.sensor_distance = 0

        # Track window sizing so we can keep it inside the usable desktop area
        self._screen_signal_connected = False
        self._adjusting_size = False

        self.hide_message_signal.connect(self._hide_operation_message)
        self.autofocus_error_signal.connect(self._show_autofocus_error)
        self.autofocus_finished_signal.connect(self._finish_autofocus_ui)
        self.lens_reposition_error_signal.connect(self._show_lens_reposition_error)
        self.lens_reposition_finished_signal.connect(self._finish_lens_reposition_ui)
        self.single_capture_ready_signal.connect(self._handle_single_capture_ready)
        self.single_capture_error_signal.connect(self._handle_single_capture_error)
        self.manual_move_error_signal.connect(self._show_manual_move_error)
        self.manual_move_finished_signal.connect(self._finish_manual_move_ui)

        self.detected_box = None   
        self.original_box = None 
        self.drawn_box = None

        self.show_scalebar = constants.SHOW_SCALEBAR
        self.save_uniform_bg = False
        self.cropping = False
        self.cropping_method = None
        self.replace_color = None
        self.results = None

        # self.cropping_model: YOLO = YOLO_MODELS["crop"]

        self._number_stacks = constants.DEFAULT_NUM_OF_STACKS
        self._stack_step_size = constants.DEFAULT_STACK_STEP_SIZE
        if self.camera_type == "VAImagingCamera":
            self.gamma_checked = self.camera.gamma_enabled_check()

        self.setWindowFlags(Qt.WindowType.Window)
        self.setWindowIcon(QIcon(r"UserInterface\imgs\enimas_icon.png"))
        self.setWindowTitle("ENIMAS - Entomoscope Imaging Software")
        self.statusBar: QStatusBar
        self.proc_var_progress_label = QLabel("Optional: click 'Post-Proc' to select image variants")
        spacer = QLabel()
        spacer.setFixedWidth(15)
        self.connection_status_label = QLabel("Entomoscope: Checking...")
        self.statusBar.addPermanentWidget(self.proc_var_progress_label)
        self.statusBar.addPermanentWidget(spacer)
        self.statusBar.addPermanentWidget(self.connection_status_label)

        #Start menu
        menubar = self.menuBar()
        start_menu = menubar.addMenu("❗Start")

        # Device Menu
        self.device_menu = QMenu("❗Select Device", self)
        start_menu.addMenu(self.device_menu)

        self.device_group = QActionGroup(self)
        self.device_group.setExclusive(False)
        devices = ["Entomoscope PI", "Entomoscope PIs", "Entomoscope PI2AI"]
        self.current_device_name = None

        self.device_selected = False

        for name in devices:
            act = QAction(name, self, checkable = True)
            act.triggered.connect(lambda ckecked, n = name: self.on_device_changed(n))
            self.device_group.addAction(act)
            self.device_menu.addAction(act)

        self.device_group.setExclusive(True)

        # Plugin Menu
        self.pluginMenu = menubar.addMenu("&Plugins")
        
        # Set up plugin system
        main_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        self.plugin_folder = os.path.join(main_dir, 'plugins')
        self.plugins = {}
        self.plugin_manager = PluginManager(self, self.plugin_folder, self.pluginMenu, PluginBase)
        self.plugin_manager.load_plugins()
        self.plugin_manager.populate_plugins_menu()

        self.help_menu = menubar.addMenu("Help")
        user_guide_action = QAction("Open User Guide", self)
        user_guide_action.setIcon(QIcon(self.style().standardIcon(QStyle.SP_MessageBoxQuestion)))
        user_guide_action.triggered.connect(self.open_user_guide)
        self.help_menu.addAction(user_guide_action)

        scale_bar_help_action = QAction("Scale Bar Setup...", self)
        scale_bar_help_action.setIcon(
            QIcon(self.style().standardIcon(QStyle.SP_MessageBoxInformation))
        )
        scale_bar_help_action.triggered.connect(self.show_scale_bar_setup)
        self.help_menu.addAction(scale_bar_help_action)

        classification_guide_action = QAction("Open Classification Model Guide", self)
        classification_guide_action.setIcon(QIcon(self.style().standardIcon(QStyle.SP_MessageBoxQuestion)))
        classification_guide_action.triggered.connect(self.open_classification_guide)
        self.help_menu.addAction(classification_guide_action)


        qss = """

        QMenuBar {
            background-color: #F5F5F5;      
            border: none;                   
            border-bottom: 1px solid #D0D0D0; 
            padding: 0px;
            font-family: "Segoe UI", "Roboto", sans-serif;
        }
        QMenuBar::item {
            background: transparent;
            color: #212121;                 
            padding: 6px 12px;              
            margin: 0px 2px;               
            border-radius: 4px;             
        }
        QMenuBar::item:hover {background-color: #E0E0E0;}
        QMenuBar::item:pressed {background-color: #D0D0D0; }

        QMenu {
            background-color: #FFFFFF;      
            border: 1px solid #D0D0D0;      
            border-radius: 6px;             
            padding: 4px 0px;
            font-size: 11pt;              
        }
        QMenu::item {color: #212121;padding: 6px 20px; }
        QMenu::item:selected {background-color: #E0E0E0; border-radius: 4px;}
        QMenu::item:pressed {background-color: #D0D0D0; }
       
        QMenu::separator {height: 1px; background: #D0D0D0; margin: 4px 0px;}

        QMenu::indicator {width: 14px; height: 14px;}
        QMenu::indicator:unchecked {border: 1px solid #B0B0B0; background: transparent; border-radius: 2px;}

        """
        self.setStyleSheet(qss)

        # Find Widgets from .ui file
        self.zenodo_upload_button: QPushButton = self.findChild(QPushButton, "zenodo_upload_button")
        #self.zenodo_upload_button.setStyleSheet("border: 1px solid gray; border-radius: 5px;")
        # TODO: Temporarily disabled until upload logic is updated
        self.zenodo_upload_button.setEnabled(False)  # Disabled for safety - enable after logic update

        # Measurement button
        self.measurement_button: QPushButton = self.findChild(QPushButton, "MeasurementButton")
        self.measurement_button.setStatusTip("Measure the specimen in the image.")
        self.measurement_button.clicked.connect(self.measurement_clicked)
        self.measurement_button.setEnabled(False)
        self.measurement_button.setIcon(QIcon())

        self.progress_label = self.findChild(QLabel, "ProgressLabel")
        # self.progress_label.setFixedHeight(80)
        self.progress_label.setMinimumHeight(60)
        self.progress_label.setMaximumHeight(90)
        self.progress_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        
        self.spinner = QMovie(r"UserInterface\imgs\Spinner.gif")
        self.spinner.setScaledSize(QSize(50, 50))
        
        # Fix layout alignment and sizing for Imaging Controls
        vl_left = self.findChild(QVBoxLayout, "verticalLayout_7")
        vl_right = self.findChild(QVBoxLayout, "verticalLayout_3")
        
        if vl_left and vl_right:
            # Left: Up(2), Auto(2), Down(2), Spacer(1), Ref(2), Stop(2) -> Total 11
            vl_left.setStretch(0, 2) # Up
            vl_left.setStretch(1, 2) # Auto
            vl_left.setStretch(2, 2) # Down
            vl_left.setStretch(3, 1) # Spacer
            vl_left.setStretch(4, 2) # Ref
            vl_left.setStretch(5, 2) # Stop
            
            # Right: Single(2), Label(2), Stack(2), Spacer(1), Settings(4) -> Total 11
            vl_right.setStretch(0, 2) # Single
            vl_right.setStretch(1, 2) # Label
            vl_right.setStretch(2, 2) # Stack
            vl_right.setStretch(3, 1) # Spacer
            vl_right.setStretch(4, 4) # Settings
        self.spinner.setSpeed(150)
        self.spinner.loopCount = 0

        self.movie = QMovie("UserInterface\imgs\Spinner.gif")
        self.movie.setScaledSize(QSize(24, 24))
        self.movie.setSpeed(150)
        self.movie.loopCount = 0
        self.movie.frameChanged.connect(self.update_icon)

        # Orientation menu
        self.orientation_button: QPushButton = self.findChild(QPushButton, "OrientationButton")
        self.orientation_button.setStatusTip("Choose the orientation of the Object.")
        self.orientation_button.clicked.connect(self.show_orientation_menu)

        self.current_orientation_primary   = "Undefined"
        self.current_orientation_secondary = "Undefined"

        self.stack_speed_spinbox: QSpinBox = self.findChild(QSpinBox, "stack_speed_spinbox")
        self.number_stacks_spinbox: QSpinBox = self.findChild(QSpinBox, "number_stacks_spinbox")
        self.step_size_spinbox: QDoubleSpinBox = self.findChild(QDoubleSpinBox, "step_size_spinbox")
        
        self.take_single_picture_bttn: QPushButton = self.findChild(QPushButton, "take_single_picture_bttn")
        self.take_stack_picture_bttn: QPushButton = self.findChild(QPushButton, "take_stack_picture_bttn")

        self.motor_up_button = self.findChild(QPushButton, "motor_up_button")
        self.motor_down_button = self.findChild(QPushButton, "motor_down_button")
        self.autofocus_button = self.findChild(QPushButton, "autofocus_button")
        self.motor_stop_button = self.findChild(QPushButton, "motor_stop_button")
        self.motor_reference_button = self.findChild(QPushButton, "motor_reference_button")
        self.motor_reference_button.setEnabled(False)
        self.referenced = False
        self._stack_operation_active = False
        self._autofocus_active = False
        self._lens_reposition_active = False
        self._single_capture_active = False
        self._manual_move_active = False
        self._stack_capture_thread = None
        self._autofocus_thread = None
        self._lens_reposition_thread = None
        self._single_capture_thread = None
        self._manual_move_thread = None
        self._single_capture_control_states = []
        self._lens_reposition_control_states = []
        self._manual_move_control_states = []
        self._active_stacker = None
        self._stack_cancel_event = None
        self._close_pending = False
        self.stop = False
        
        self.cam_setting_bttn = self.findChild(QPushButton, "cam_settings_bttn")
                
        self.post_proc_bttn: QPushButton = self.findChild(QPushButton, "PostProcButton")
        self.post_proc_bttn.setStatusTip("Select image variants that should be processed.")
        self.image_settings_dialog = ImageSettingsDialog(self)

        self.classification_model_selector = self.findChild(CheckableComboBox, "classification_model_selector")
        self.classification_model_selector.currentIndexChanged.connect(self.update_classify_enable)
        self.add_model_button = self.findChild(QPushButton, "add_model_button")
        self.classify_button = self.findChild(QPushButton, "ClassifyButton")
        self.classify_button.setEnabled(False)
        self.classify_button.clicked.connect(self.on_classify)
        self.variant_combo = self.findChild(QComboBox, "VariantCombo")
        self.variant_combo.currentIndexChanged.connect(self.update_classify_enable) 
        self.variant_combo.setEditable(True)
        self.variant_combo.setInsertPolicy(QComboBox.NoInsert)
        self.variant_combo.lineEdit().setPlaceholderText("Select image variants:")
        self.variant_combo.lineEdit().setReadOnly(True)
        classification_results_widget = self.findChild(QWidget, "classification_results_widget")
        self.classification_results_viewer = ClassificationResultsViewer(classification_results_widget)  

        self.classification_model_selector.currentIndexChanged.connect(self.update_classify_enable)
        self.variant_combo.currentIndexChanged.connect(self.update_classify_enable)
             
        # Directory
        dir_widgets = [
            self.findChild(QTreeView, "dir_view"),
            self.findChild(QPushButton, "browse_dir_bttn"),
            self.findChild(QPushButton, "add_specimen_bttn"),
            self.findChild(QLineEdit, "entry_1"),
            self.findChild(QLineEdit, "entry_2"),
            self.findChild(QSpinBox, "spinBox"),
            self.findChild(QTabWidget, "tabWidget"),
            self.findChild(QLineEdit, "single_specimen_entry"),
            self.findChild(QLineEdit, "single_folder_entry"),
            self.findChild(QPushButton, "browse_single_dir_bttn"),
            self.findChild(QLineEdit, "entry_3"),
        ]
        print(len(dir_widgets))
        self.directory_manager = DirectoryManager(dir_widgets, self.statusBar)

        # Video label
        self.video_label = self.findChild(QLabel, "video_label")
        self.video_label.setStyleSheet(""" QLabel{border: 1px solid #CCCCCC; border-radius: 8px;}""")

        shadow = QGraphicsDropShadowEffect(self.video_label)
        shadow.setBlurRadius(20)            
        shadow.setOffset(0, 4)               
        shadow.setColor(QColor(0, 0, 0, 160)) 
        self.video_label.setGraphicsEffect(shadow)
        #self.video_label.setStyleSheet("border: 1px solid black;")

        # Static label with scroll area for zoom functionality
        self.scroll_area = QScrollArea(self.video_label.parent())
        self.scroll_area.setGeometry(self.video_label.geometry())
        self.scroll_area.setWidgetResizable(False)  # Important for zoom to work
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setAlignment(Qt.AlignCenter)
        self.scroll_area.setStyleSheet("""
            QScrollArea {
                border: 1px solid #CCCCCC; 
                border-radius: 8px;
                background-color: #f0f0f0;
            }
        """)
        
        self.static_label = InteractiveStaticLabel()
        self.static_label.setAlignment(Qt.AlignCenter)
        self.static_label.setScaledContents(False)  # Important: Don't auto-scale contents
        self.static_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)  # Fixed size policy
        self.static_label.zoom_changed.connect(self.update_zoom_label)  # Update label when zoom changes
        self.scroll_area.setWidget(self.static_label)
        self.scroll_area.hide()
        
        self.scroll_area.raise_()
        self.video_label.installEventFilter(self)
        self.showing_static_image = False
        self.static_label.drawing_enabled = False
        self.static_label.show_rectangle = False

        parent_layout = self.findChild(QVBoxLayout, "verticalLayout_8")

        self.livefeed_bttn = QPushButton("Live Feed")
        self.livefeed_bttn.setStyleSheet(new_button_style)
        self.delete_bttn = QPushButton("Delete last image")
        self.delete_bttn.setStyleSheet(new_button_style)
        self.confirm_bttn = QPushButton("Confirm")
        self.confirm_bttn.setStyleSheet(new_button_style)
        self.clear_rectangle_bttn = QPushButton("Clear Rectangle")
        self.clear_rectangle_bttn.setStyleSheet(new_button_style)
        self.save_annotated_bttn = QPushButton("Save Annotated Image")
        self.save_annotated_bttn.setStyleSheet(new_button_style)
        self.livefeed_bttn.clicked.connect(self.return_to_livefeed)
        self.delete_bttn.clicked.connect(self.delete_last_image)
        self.confirm_bttn.clicked.connect(self.confirm_annotation)
        self.clear_rectangle_bttn.clicked.connect(self.clear_annotation)
        self.save_annotated_bttn.clicked.connect(self.save_annotated_image) 
        self.confirm_bttn.setEnabled(False)
        self.clear_rectangle_bttn.setEnabled(False)
        self._deleting = False

        self.livefeed_bttn.hide()
        self.delete_bttn.hide()
        self.confirm_bttn.hide()
        self.save_annotated_bttn.hide()
        self.clear_rectangle_bttn.hide()

        # Zoom controls
        self.zoom_in_bttn = QPushButton("Zoom In (+)")
        self.zoom_in_bttn.setStyleSheet(new_button_style)
        self.zoom_out_bttn = QPushButton("Zoom Out (-)")
        self.zoom_out_bttn.setStyleSheet(new_button_style)
        self.zoom_fit_bttn = QPushButton("Fit to Window")
        self.zoom_fit_bttn.setStyleSheet(new_button_style)
        self.zoom_level_label = QLabel("Zoom: 100%")
        self.zoom_level_label.setAlignment(Qt.AlignCenter)
        
        self.zoom_in_bttn.clicked.connect(self.zoom_in_image)
        self.zoom_out_bttn.clicked.connect(self.zoom_out_image)
        self.zoom_fit_bttn.clicked.connect(self.fit_to_window_image)
        
        self.zoom_in_bttn.setToolTip("Zoom in\nShortcut: Ctrl + Mouse Wheel Up")
        self.zoom_out_bttn.setToolTip("Zoom out\nShortcut: Ctrl + Mouse Wheel Down")
        self.zoom_fit_bttn.setToolTip("Fit entire image to window\nDouble-click image also fits to window")
        
        self.zoom_in_bttn.hide()
        self.zoom_out_bttn.hide()
        self.zoom_fit_bttn.hide()
        self.zoom_level_label.hide()

        button_row = QHBoxLayout()
        button_row.addWidget(self.livefeed_bttn)
        button_row.addWidget(self.delete_bttn)
        button_row.addWidget(self.confirm_bttn)
        button_row.addWidget(self.clear_rectangle_bttn)
        button_row.addWidget(self.save_annotated_bttn)
        button_row.addStretch()
        button_row.addWidget(self.zoom_out_bttn)
        button_row.addWidget(self.zoom_level_label)
        button_row.addWidget(self.zoom_in_bttn)
        button_row.addWidget(self.zoom_fit_bttn)
        parent_layout.insertLayout(parent_layout.indexOf(self.video_label) + 1, button_row)
        
        self.show_connection_error.connect(self.camera_connection_error)
        if self.camera_type == "ArducamCamera":
            self.arducam = Arducam(self.camera, self.video_label, self.show_connection_error)
        elif self.camera_type == "VAImagingCamera":
            self.arducam = VAImaging(self.camera, self.video_label, self.show_connection_error)

        # Lens Manager
        self.change_lens_bttn = self.findChild(QPushButton, "change_lens_bttn")
        self.lens_info_label = self.findChild(QLabel, "lens_info_label")
        self.lens_info_label.setWordWrap(True)
    
        self.all_lenses = []
        self.lens_approved = False
        self.current_lens: Lens = None
        if self.camera is not None:
            self.default_cam_values = self.arducam.get_reg_values()
        self.load_lenses_from_file()
        # self.lens_info_label.setText(self.current_lens.label_str)          
        # self.lens_info_label.setText(f"{self.current_lens.label_str}\nScale: {self.current_scale}")

        # Set default stack values
        self.stack_speed_spinbox.setValue(constants.STACK_SPEED)
        self.number_stacks_spinbox.setValue(self._number_stacks)
        self.step_size_spinbox.setValue(self._stack_step_size)
        
        # Connect callback methods
        self.motor_up_button.clicked.connect(self.motor_up)
        self.motor_down_button.clicked.connect(self.motor_down)
        self.autofocus_button.clicked.connect(self.autofocus_clicked)
        self.motor_reference_button.clicked.connect(self.motor_ref)
        self.motor_stop_button.clicked.connect(self.motor_stop)
        self.take_single_picture_bttn.clicked.connect(self.take_single_picture)
        self.take_stack_picture_bttn.clicked.connect(self.stack_pictures_clicked)
        self.change_lens_bttn.clicked.connect(self.open_lens_dialog)
        self.cam_setting_bttn.clicked.connect(self.open_settings_dialog)
        self.zenodo_upload_button.clicked.connect(self.upload_to_zenodo)
        self.post_proc_bttn.clicked.connect(self.open_image_settings_dialog)
        
        self.number_stacks_spinbox.valueChanged.connect(self.number_stacks_changed)
        self.step_size_spinbox.valueChanged.connect(self.step_size_changed)
        
        # change status tips when values for folder names changed
        dir_widgets[0].clicked.connect(self.set_status_tip) # explorer
        dir_widgets[3].textChanged.connect(self.set_status_tip) # code prefix
        dir_widgets[4].textChanged.connect(self.set_status_tip) # plate id
        dir_widgets[5].valueChanged.connect(self.set_status_tip) # code number
        dir_widgets[6].currentChanged.connect(self.set_status_tip) # tab
        dir_widgets[7].textChanged.connect(self.set_status_tip) # single name
        dir_widgets[10].textChanged.connect(self.set_status_tip) # Sample ID
        dir_widgets[10].textChanged.connect(self.update_zenodo_button_state) # Update Zenodo button when Sample ID changes

        self.sample_ID = dir_widgets[10]
        
        # StatusBar tips
        self.set_status_tip()
        
        # Stack fuse status in statusbar
        self.fuse_status.connect(self.statusBar.showMessage)
        
        # little menu when right clicking stack img button
        self.take_stack_picture_bttn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.take_stack_picture_bttn.customContextMenuRequested.connect(self.change_stack_mode_menu)
        
        self.add_model_button.setContextMenuPolicy(Qt.CustomContextMenu)
        self.add_model_button.clicked.connect(self.addModel)
        
        # Update ui according to the connection of entomoscope 
        self.update_ui_state()

        # Disable motor movement until referenced
        self.set_motor_control_state(False)
        # self.motor_reference_button.setEnabled(True)
        
        # Load the YOLO models
        load_models(self.classification_model_selector.getAllOptions())

    def showEvent(self, event):
        """Clamp the window to the available screen so the taskbar never overlaps it."""
        super().showEvent(event)
        # Don't apply size adjustments to maximized windows
        if not self.isMaximized() and not self._initial_size_set:
            # Force preferred size on first show
            self.setMinimumSize(self._preferred_size)
            self.resize(self._preferred_size)
            # Center within available geometry (no extra margins)
            screen = self.screen() or QApplication.primaryScreen()
            if screen:
                available = screen.availableGeometry()
                frame_geo = self.frameGeometry()
                new_x = available.x() + (available.width() - frame_geo.width()) // 2
                new_y = available.y() + (available.height() - frame_geo.height()) // 2
                self.move(new_x, new_y)
            self._initial_size_set = True

        # Run once after window is shown (initial pass without extra margin to honor preferred size)
        QTimer.singleShot(50, lambda: self._fit_window_to_screen(use_margin=False))
        if not self._screen_signal_connected and self.windowHandle():
            self.windowHandle().screenChanged.connect(lambda _: self._fit_window_to_screen())
            self._screen_signal_connected = True

    def sizeHint(self):
        return self._preferred_size

    def minimumSizeHint(self):
        return self._minimum_hint

    def _update_minimum_size_for_screen(self):
        """Ensure the minimum size never exceeds the available screen (prevents maximize warnings)."""
        safe_rect = self._safe_available_geometry(use_margin=False)
        if not safe_rect:
            return

        frame_geo = self.frameGeometry()
        geo = self.geometry()
        frame_extra_w = max(0, frame_geo.width() - geo.width())
        frame_extra_h = max(0, frame_geo.height() - geo.height())

        max_min_w = max(600, safe_rect.width() - frame_extra_w)
        max_min_h = max(600, safe_rect.height() - frame_extra_h)

        min_w = min(self._minimum_hint.width(), max_min_w)
        min_h = min(self._minimum_hint.height(), max_min_h)

        self.setMinimumSize(min_w, min_h)

    def _safe_available_geometry(self, use_margin: bool=True):
        """Return a conservative available geometry, optionally padding for taskbar overlap."""
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return None

        available = screen.availableGeometry()
        screen_geo = screen.geometry()

        # Calculate how much space Qt already excluded (likely the taskbar)
        bottom_gap = screen_geo.height() - available.height()
        
        # Debug logging to see what's happening
        logger.debug(f"Screen geometry: {screen_geo.width()}x{screen_geo.height()}")
        logger.debug(f"Available geometry: {available.width()}x{available.height()}")
        logger.debug(f"Detected bottom gap: {bottom_gap}px")
        
        # Always add additional safety margin on top of Qt's detection (skip extra margin when maximized)
        additional_margin = 0 if (self.isMaximized() or not use_margin) else 100  # extra safety margin for taskbar and system UI
        total_bottom_gap = bottom_gap + additional_margin
        
        logger.debug(f"Applying total bottom gap: {total_bottom_gap}px")

        safe_rect = available.adjusted(0, 0, 0, -additional_margin)
        return safe_rect if safe_rect.width() > 0 and safe_rect.height() > 0 else available

    def _apply_preferred_size_once(self):
        """Set a reasonable starting size (non-maximized) while keeping within the screen."""
        if self._initial_size_set:
            return

        safe_rect = self._safe_available_geometry(use_margin=False)
        if not safe_rect:
            return

        frame_geo = self.frameGeometry()
        geo = self.geometry()

        frame_extra_w = frame_geo.width() - geo.width()
        frame_extra_h = frame_geo.height() - geo.height()
        max_width = max(200, safe_rect.width() - frame_extra_w)
        max_height = max(200, safe_rect.height() - frame_extra_h)

        # Use the preferred size (1430x906) but ensure it fits within screen bounds
        target_w = min(self._preferred_size.width(), max_width)
        target_h = min(self._preferred_size.height(), max_height)

        if target_w != self.width() or target_h != self.height():
            logger.debug(f"Applying preferred size: {self.width()}x{self.height()} -> {target_w}x{target_h}")
            self.resize(target_w, target_h)

        self._initial_size_set = True

    def _fit_window_to_screen(self, use_margin: bool=True):
        """Fit window within screen bounds, accounting for taskbar and window decorations."""
        if self._adjusting_size:
            return  # Prevent recursive calls
        
        # Don't interfere with maximized windows - let Qt handle them
        if self.isMaximized():
            logger.debug("Window is maximized, skipping size adjustment")
            return
            
        self._adjusting_size = True
        try:
            self._update_minimum_size_for_screen()
            safe_rect = self._safe_available_geometry(use_margin=use_margin)
            if not safe_rect:
                return

            frame_geo = self.frameGeometry()
            geo = self.geometry()

            # Account for the window frame so the outer size stays within the work area
            frame_extra_w = frame_geo.width() - geo.width()
            frame_extra_h = frame_geo.height() - geo.height()
            max_width = max(200, safe_rect.width() - frame_extra_w)
            max_height = max(200, safe_rect.height() - frame_extra_h)

            # Calculate the target size
            new_w = min(self.width(), max_width)
            new_h = min(self.height(), max_height)
            
            logger.debug(f"Screen fit check: current={self.width()}x{self.height()}, max={max_width}x{max_height}, target={new_w}x{new_h}")
            
            # Only resize if we actually exceed the bounds
            if self.width() <= max_width and self.height() <= max_height:
                logger.debug("Window already fits within screen bounds")
                return
            
            # Resize the window
            logger.debug(f"Resizing window: {self.width()}x{self.height()} -> {new_w}x{new_h}")
            self.resize(new_w, new_h)

            # Clamp the window position so it stays inside the safe area
            QTimer.singleShot(50, self._adjust_position)
            
        finally:
            self._adjusting_size = False
    
    def _adjust_position(self):
        """Adjust window position to ensure it's within screen bounds."""
        if self.isMaximized():
            return
        safe_rect = self._safe_available_geometry()
        if not safe_rect:
            return
            
        frame_geo = self.frameGeometry()
        
        # Calculate target position
        target_x = max(safe_rect.left(), min(frame_geo.left(), safe_rect.right() - frame_geo.width()))
        target_y = max(safe_rect.top(), min(frame_geo.top(), safe_rect.bottom() - frame_geo.height()))
        
        # Apply offset for window decorations
        offset_x = self.x() - frame_geo.x()
        offset_y = self.y() - frame_geo.y()
        
        new_x = target_x + offset_x
        new_y = target_y + offset_y
        
        if new_x != self.x() or new_y != self.y():
            logger.debug(f"Adjusting position: ({self.x()}, {self.y()}) -> ({new_x}, {new_y})")
            self.move(new_x, new_y)

    def on_device_changed(self, device_name):
        """Verifies whether the connected device matches the chosen type and applies device_specific settings """
        action = self.sender()
        assert isinstance(action, QAction)
        camera_class = DEVICE_CAMERAS.get(device_name)
        if self.camera_type == camera_class.__name__:
            self.connection_status_label.setText(f"{device_name} connected: {self.camera_type}")
            self.classification_results_viewer.set_text("Please reference axis")
            self.current_device_name = device_name
            if self.current_device_name == "Entomoscope PI":
                self.axis_length = constants.AXIS_LENGHT
                self.sensor_distance = 0
                self.set_new_limit()
                self.current_lens.base_focus_height = self.current_lens.base_focus_height_arducam_old
            elif self.current_device_name == "Entomoscope PIs":
                self.axis_length = constants.TRAVEL_LENGHT_S
                self.sensor_distance = constants.SENSOR_TO_BASE_PLATE_S
                self.set_new_limit()
                self.current_lens.base_focus_height = self.current_lens.base_focus_height_arducam
            elif self.current_device_name == "Entomoscope PI2AI":
                
                self.axis.DIST_STEPS = 360 * constants.MICROSTEPS / 1.8 / constants.ROTATION_HEIGHT_N
                self.axis.STEPS_DIST = 1 / self.axis.DIST_STEPS
                self.axis_length = constants.TRAVEL_LENGHT_N
                self.sensor_distance = constants.SENSOR_TO_BASE_PLATE_N
                self.set_new_limit()
                self.current_lens.base_focus_height = self.current_lens.base_focus_height_vaimaging
            self.device_selected = True
            self.referenced = False
            self.update_ui_state()
            self.menuBar().actions()[0].setText("Start") 
            self.device_menu.menuAction().setText(f"Select Device")

        else:
            self.device_selected = False
            with QSignalBlocker(action):
                action.setChecked(False)
            self.update_ui_state()
            self.menuBar().actions()[0].setText("❗Start") 
            self.device_menu.menuAction().setText(f"❗Select Device")
            print(self.camera_type, " != ", camera_class.__name__)
            QMessageBox.critical(self,"Wrong connection", f"current camera does not match the chosen device")

    def is_entomoscope_connected(self):
        return self.camera is not None 

    def update_ui_state(self):
        """Disable buttons when continue without camera or with wrong chosen device"""
        connected = self.is_entomoscope_connected()
        enabled = self.device_selected and connected
        #print(f"update ui state: {self.device_selected}, {enabled}")

        self.motor_reference_button.setEnabled(enabled)
        if not enabled:
            self.set_motor_control_state(enabled)
        self.set_stack_setting_state(enabled)
        self._update_single_capture_button()
        self.motor_stop_button.setEnabled(enabled)
        self.set_camera_lens_state(enabled)
        self.post_proc_bttn.setEnabled(enabled)
        self.orientation_button.setEnabled(enabled)
        self.device_menu.setEnabled(connected)

        if not connected:
            self.connection_status_label.setText("Entomoscope Disconnected")
            self.proc_var_progress_label.setText(" ")

            self.lens_info_label.setText("No camera")
            
        else:
            if not enabled:
                self.connection_status_label.setText("❗Select a device from Start menu")
            self.proc_var_progress_label.setText("Optional: click 'Post-Proc' to select image variants")
            # The camera preview worker is created exactly once during window
            # initialization. Recreating it here would leave the previous
            # QThread reading the same physical camera with a different lock.
            if self.camera_type in ("ArducamCamera", "VAImagingCamera"):
                self.current_scale = lens_scale_for_camera(self.current_lens, self.camera_type)
                self.current_lens.scale = self.current_scale
            scale_text = f"{self.current_scale:g} mm/pixel" if self.current_scale else "not calibrated"
            self.lens_info_label.setText(f"{self.current_lens.label_str}\nScale: {scale_text}")
            
    def open_user_guide(self):
        path = os.path.abspath("User_Guide.pdf")
        if os.path.exists(path):
            os.startfile(path)
        else: 
            print("User Guide not found.")

    def show_scale_bar_setup(self):
        message = QMessageBox(self)
        message.setWindowTitle("Scale Bar Setup")
        message.setIcon(QMessageBox.Information)
        message.setText("Calibrate each camera and lens combination before adding a scale bar.")
        message.setInformativeText(
            "1. Photograph a stage micrometer or ruler at the normal imaging setup.\n"
            "2. Measure a known physical span in pixels without resizing the image.\n"
            "3. Open Lens settings, select the lens, and click Calculate next to "
            "Scale (mm/pixel).\n"
            "4. Enable Scale bar under Post-Proc before taking images.\n\n"
            "ENIMAS will not create a scale bar or physical measurement from an "
            "uncalibrated lens. Recalibrate if the camera, lens, resolution, or "
            "optical setup changes."
        )
        open_button = message.addButton("Open Lens Settings", QMessageBox.AcceptRole)
        message.addButton(QMessageBox.Close)
        message.exec()
        if message.clickedButton() == open_button:
            self.open_lens_dialog()

    def open_classification_guide(self):
        path = os.path.abspath("CLASSIFICATION_MODEL_GUIDE.pdf")
        if os.path.exists(path):
            os.startfile(path)
        else: 
            print("Classification Model Guide not found.")

    def set_camera_lens_state(self, state):
        self.cam_setting_bttn.setEnabled(state)
        self.change_lens_bttn.setEnabled(state)   

    def set_stack_setting_state(self,state):
        self.stack_speed_spinbox.setEnabled(state)
        self.number_stacks_spinbox.setEnabled(state)
        self.step_size_spinbox.setEnabled(state)            
    
    def addModel(self):
        dialog = AddModelDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self.classification_model_selector.updateChoices()
    
    def motor_up(self, *, steps: float=None, wait=False):
        if steps is None:
            steps = self._stack_step_size
        logger.debug(f"moving up for {steps}")
        if wait:
            return self.axis.move_for(-steps, wait=True)
        return self._request_manual_move(-steps)

    def motor_down(self, *, steps: float=None, wait=False):
        if steps is None:
            steps = self._stack_step_size
        logger.debug(f"moving down for {steps}")
        if wait:
            return self.axis.move_for(steps, wait=True)
        return self._request_manual_move(steps)

    def _request_manual_move(self, distance):
        """Coordinate arrow movement with camera and motor workers."""
        if getattr(self, "_close_pending", False):
            return

        if (
            getattr(self, "_stack_operation_active", False)
            or getattr(self, "_autofocus_active", False)
            or getattr(self, "_lens_reposition_active", False)
            or getattr(self, "_single_capture_active", False)
            or getattr(self, "_manual_move_active", False)
        ):
            self.statusBar.showMessage(
                "Wait for the current image or motor operation to finish.",
                10000,
            )
            return

        self._start_manual_move(distance)

    def _start_manual_move(self, distance):
        if abs(float(distance)) < 1e-9 or getattr(self, "_close_pending", False):
            return

        self.stop = False
        self._manual_move_active = True
        self._manual_move_control_states = []
        for name in (
            "take_single_picture_bttn",
            "take_stack_picture_bttn",
            "motor_up_button",
            "motor_down_button",
            "autofocus_button",
            "motor_reference_button",
            "cam_setting_bttn",
            "change_lens_bttn",
        ):
            widget = getattr(self, name, None)
            if widget is None:
                continue
            is_enabled = getattr(widget, "isEnabled", None)
            previous_state = bool(is_enabled()) if callable(is_enabled) else True
            self._manual_move_control_states.append((widget, previous_state))
            widget.setEnabled(False)

        self.statusBar.showMessage("Moving the focus stage...", 10000)
        move_thread = Thread(target=self._manual_move_worker, args=(distance,))
        self._manual_move_thread = move_thread
        try:
            move_thread.start()
        except Exception as exc:
            logger.exception("Could not start manual stage movement worker")
            self._finish_manual_move_ui()
            if not getattr(self, "_close_pending", False):
                QMessageBox.warning(
                    self,
                    "Manual movement could not start",
                    f"ENIMAS could not start the focus movement: {exc}",
                )

    def _manual_move_worker(self, distance):
        try:
            result = self.axis.move_for(distance, wait=True)
            if result is False:
                self.manual_move_error_signal.emit(
                    "The focus stage did not complete the requested movement."
                )
        except Exception as exc:
            logger.exception("Manual stage movement failed")
            self.manual_move_error_signal.emit(
                f"The focus stage movement failed: {exc}"
            )
        finally:
            self.manual_move_finished_signal.emit()
    
    def move_motor_to(self, pos: float, wait=False):
        logger.debug(f"moving to {pos}") 
        self.axis.set_speed(constants.DEFAULT_SPEED)
        self.axis.move_to(pos, wait=wait)
        self.axis.set_speed(constants.LOW_SPEED)

    def motor_ref(self):
        logger.debug("referencing")
        self.set_motor_control_state(False)
        MessageBox.information("Referencing Axis ...   ")
        success = self.axis.axis_reference()
        MessageBox.hide_message()
        if not success:
            QMessageBox.critical(
                self, "Referencing Failed",
                "The axis could not be referenced.\n\n"
                "Please check that the motor and endstop switch are "
                "properly connected, then try again.")
            self.set_motor_control_state(True)
            return
        self.set_motor_control_state(True)
        self.referenced = True
        self.classification_results_viewer.clear()
        method_selection.prompt_user_fuse_mode()
    
    def motor_stop(self):
        logger.debug("stopping motor")
        self.axis.stop()
        self.stop = True # to stop the stack loop or autofocus loop if it's running
        cancel_event = getattr(self, "_stack_cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()

    def invalidate_axis_reference(self):
        """Require a new reference after an interrupted stage movement."""
        self.referenced = False
        self.axis._referenced = False
        self.motor_up_button.setEnabled(False)
        self.motor_down_button.setEnabled(False)
        self.autofocus_button.setEnabled(False)
        self.take_stack_picture_bttn.setEnabled(False)
        self.motor_reference_button.setEnabled(True)

    def upload_to_zenodo(self):
        sample_ID_entry = self.sample_ID
        sample_ID = sample_ID_entry.text()
        print(f"Debug - upload_to_zenodo called with Sample ID: {sample_ID}")
        
        if not hasattr(self, '_last_image_path') or not self._last_image_path:
            print("Debug - No _last_image_path available!")
            QMessageBox.warning(self, "No Image Available", "No image is available for upload.")
            return
            
        if not os.path.exists(self._last_image_path):
            print(f"Debug - Image path does not exist: {self._last_image_path}")
            QMessageBox.warning(self, "Image Not Found", f"The image file does not exist: {self._last_image_path}")
            return
            
        if not sample_ID:
            print("Debug - No sample ID provided!")
            QMessageBox.warning(self, "No Sample ID", "Please enter a Sample ID before uploading.")
            return
        
        try:
            image_path = self._last_image_path
            print(f"Debug - Starting Zenodo upload for: {image_path}")
            zenodo_uploader = Uploader(image_path, sample_ID)
            zenodo_uploader.start()
            QMessageBox.information(self, "Upload Started", "Zenodo upload started. Check the console for progress.")
        except Exception as e:
            print(f"Debug - Error starting Zenodo upload: {str(e)}")
            QMessageBox.critical(self, "Upload Error", f"Error starting Zenodo upload: {str(e)}")

    def update_zenodo_button_state(self):
        """Update Zenodo button state based on sample ID and image availability."""
        # TODO: Temporarily disabled - uncomment when ready to enable
        return  # Early return to keep button disabled
        
        sample_ID = self.sample_ID.text().strip()
        has_sample_id = bool(sample_ID)
        has_image_path_attr = hasattr(self, '_last_image_path')
        has_image_path = bool(self._last_image_path) if has_image_path_attr else False
        image_exists = os.path.exists(self._last_image_path) if has_image_path else False
        
        print(f"Debug - Sample ID: '{sample_ID}' ({has_sample_id})")
        print(f"Debug - Has _last_image_path attribute: {has_image_path_attr}")
        print(f"Debug - _last_image_path: '{self._last_image_path if has_image_path_attr else 'N/A'}' ({has_image_path})")
        print(f"Debug - Image exists: {image_exists}")
        
        if has_sample_id and has_image_path and image_exists:
            print("Debug - Enabling Zenodo button")
            self.zenodo_upload_button.setEnabled(True)
        else:
            print("Debug - Disabling Zenodo button")
            self.zenodo_upload_button.setEnabled(False)
            
    def set_motor_control_state(self, state):
        self.motor_up_button.setEnabled(state)
        self.motor_down_button.setEnabled(state)
        self.autofocus_button.setEnabled(state)
        self.motor_reference_button.setEnabled(state)
        self.take_stack_picture_bttn.setEnabled(state)

    def _update_single_capture_button(self):
        enabled = bool(
            getattr(self, "device_selected", False)
            and getattr(self, "camera", None) is not None
            and not getattr(self, "showing_static_image", False)
            and not getattr(self, "_stack_operation_active", False)
            and not getattr(self, "_autofocus_active", False)
            and not getattr(self, "_lens_reposition_active", False)
            and not getattr(self, "_single_capture_active", False)
            and not getattr(self, "_manual_move_active", False)
        )
        self.take_single_picture_bttn.setEnabled(enabled)

    @pyqtSlot()
    def _hide_operation_message(self):
        """Ignore queued worker UI cleanup after final shutdown begins."""
        if not getattr(self, "_close_pending", False):
            MessageBox.hide_message()

    def approve_lens(self, lens: Lens, text) -> bool:
        # answer = QMessageBox.question(self, "Confirm the current Lens", f"Are you sure that the current lens is: {lens.name}, with a lenght of {lens.lenght}mm ? \n{text}")
        answer = QMessageBox.warning(self, "Confirm the current Lens", f"Are you sure that the current lens is: {lens.name}, with a lenght of {lens.lenght}mm ? \n{text}", buttons= QMessageBox.Yes | QMessageBox.No)
        return answer == QMessageBox.Yes
    
    def autofocus_clicked(self):
        if (
            getattr(self, "_stack_operation_active", False)
            or getattr(self, "_autofocus_active", False)
            or getattr(self, "_lens_reposition_active", False)
            or getattr(self, "_single_capture_active", False)
            or getattr(self, "_manual_move_active", False)
        ):
            self.statusBar.showMessage(
                "Wait for the current image or motor operation to finish.",
                10000,
            )
            return
        if not self.lens_approved:
            if not self.approve_lens(self.current_lens, "The camera is going to move up and then search downwards for the focus point."):
                return 
            self.lens_approved = True
        
        self.stop = False
        self._autofocus_active = True
        self.take_single_picture_bttn.setEnabled(False)
        MessageBox.information("Autofocusing ...", abort=True, callback=self.abort_autofocus)
        self.set_motor_control_state(False)
        self.set_camera_lens_state(False)
        QApplication.processEvents()
        autofocus_thread = Thread(target=self.do_autofocus_robust)
        self._autofocus_thread = autofocus_thread
        try:
            autofocus_thread.start()
        except Exception as exc:
            logger.exception("Could not start autofocus worker")
            self._finish_autofocus_ui()
            QMessageBox.warning(
                self,
                "Autofocus could not start",
                f"ENIMAS could not start autofocus: {exc}",
            )
        
    def abort_autofocus(self, *_):
        if self.stop:
            return
        logger.debug("autofocus abort requested")
        self.stop = True
        self.axis.stop()

    def _autofocus_handle_camera_failure(self):
        logger.error("Failed to capture a fresh image during autofocus")
        self.hide_message_signal.emit()
        QMetaObject.invokeMethod(
            self,
            "camera_connection_error_during_autofocus",
            Qt.ConnectionType.QueuedConnection,
        )

    def _autofocus_prepare_capture(self):
        if hasattr(self.arducam, "begin_autofocus_capture"):
            self.arducam.begin_autofocus_capture()
        warmup_discard_frames = getattr(self.arducam, "autofocus_warmup_discard_frames", 0)
        if warmup_discard_frames > 0 and hasattr(self.arducam, "get_fresh_image"):
            try:
                self.arducam.get_fresh_image(discard_frames=warmup_discard_frames, max_attempts=1)
            except Exception as e:
                logger.debug(f"Autofocus warmup frame discard skipped: {e}")

    def _autofocus_finish_capture(self):
        if hasattr(self.arducam, "end_autofocus_capture"):
            self.arducam.end_autofocus_capture()

    def _autofocus_should_stop(self):
        return bool(self.stop)

    def _autofocus_wait(self, duration, interval=0.01):
        deadline = time() + max(0.0, float(duration))
        while time() < deadline:
            if self._autofocus_should_stop():
                return False
            remaining = max(0.0, deadline - time())
            if remaining <= 0:
                break
            sleep(min(interval, remaining))
        return not self._autofocus_should_stop()

    def _autofocus_max_z(self):
        """Deepest z autofocus may drive to, honouring every known limit.

        ``axis.lower_limit`` is derived from the configured lens length and can
        land beyond the physical travel when the lens is short, so the travel is
        applied as a second, independent guard.
        """
        limit = float(self.axis.lower_limit) - constants.AXIS_SAFETY_MARGIN
        axis_length = float(getattr(self, "axis_length", 0) or 0)
        if axis_length > 0:
            limit = min(limit, axis_length - constants.AXIS_SAFETY_MARGIN)
        return max(0.0, limit)

    def _autofocus_clamp_z(self, z, max_valid_z=None):
        if max_valid_z is None:
            max_valid_z = self._autofocus_max_z()
        return max(0.0, min(float(z), float(max_valid_z)))

    def _autofocus_move_to(self, pos, wait=True, speed=None):
        pos = self._autofocus_clamp_z(pos)
        logger.debug(f"autofocus move to {pos:.3f}")
        if speed is None:
            speed = constants.LOW_SPEED
        self.axis.set_speed(speed)
        self.axis.move_to(pos, wait=wait)

    def _autofocus_positions(self, start, end, step):
        step = abs(float(step))
        if step <= 0:
            return [self._autofocus_clamp_z(start)]

        start = float(start)
        end = float(end)
        direction = -1.0 if start > end else 1.0
        epsilon = step * 0.2
        positions = []
        current = start

        while (current >= end - epsilon) if direction < 0 else (current <= end + epsilon):
            clamped = self._autofocus_clamp_z(current)
            if not positions or abs(clamped - positions[-1]) > 1e-4:
                positions.append(clamped)
            current += direction * step

        final = self._autofocus_clamp_z(end)
        if not positions or abs(final - positions[-1]) > 1e-4:
            positions.append(final)

        return positions

    def _autofocus_get_image(self, max_retries=3, discard_frames=2):
        for retry in range(max_retries):
            if self._autofocus_should_stop():
                return None
            try:
                if hasattr(self.arducam, "get_fresh_image"):
                    img = self.arducam.get_fresh_image(
                        discard_frames=discard_frames,
                        max_attempts=1,
                        timeout_s=constants.AUTOFOCUS_CAPTURE_TIMEOUT,
                        stop_check=self._autofocus_should_stop,
                    )
                else:
                    img = self.arducam.get_image()

                if img is not None:
                    return img

                if not self._autofocus_wait(0.03):
                    return None
            except Exception as e:
                logger.error(f"Error getting image during autofocus (attempt {retry + 1}/{max_retries}): {e}")
                if not self._autofocus_wait(0.05):
                    return None
        return None

    def _autofocus_measure_sharpness(self, n_samples=3, discard_frames=2):
        scores = []

        for sample_index in range(n_samples):
            if self._autofocus_should_stop():
                return 0.0, True
            img = self._autofocus_get_image(
                max_retries=3 if sample_index == 0 else 1,
                discard_frames=discard_frames if sample_index == 0 else 0,
            )
            if img is None:
                break

            scores.append(float(self.arducam.calc_focus(img)))
            if sample_index < n_samples - 1:
                if not self._autofocus_wait(0.01):
                    break

        if not scores:
            return 0.0, False

        return float(np.median(scores)), True

    def _autofocus_scan_positions(
        self,
        positions,
        *,
        settle_time,
        n_samples,
        discard_frames,
        label,
        move_speed,
        stop_after_peak=False,
        peak_drop_ratio=0.5,
        peak_decline_count=3,
        min_samples_before_stop=8,
    ):
        samples = []
        best_score = -1.0
        best_z = None
        post_peak_scores = []

        for pos in positions:
            if self.stop:
                break

            self._autofocus_move_to(pos, wait=True, speed=move_speed)
            if self.stop:
                break
            if not self._autofocus_wait(settle_time):
                break

            sharpness, ok = self._autofocus_measure_sharpness(
                n_samples=n_samples,
                discard_frames=discard_frames,
            )
            if self.stop:
                break
            if not ok:
                return samples, False

            sample = {"z": float(self.axis.z), "score": float(sharpness)}
            samples.append(sample)
            logger.debug(f"{label}: sharpness={sample['score']:.4f}, z={sample['z']:.3f}, samples={n_samples}")

            if sample["score"] > best_score:
                best_score = sample["score"]
                best_z = sample["z"]
                post_peak_scores.clear()
            elif stop_after_peak and best_score > 0:
                post_peak_scores.append(sample["score"])
                if (
                    len(samples) >= min_samples_before_stop
                    and len(post_peak_scores) >= peak_decline_count
                ):
                    recent = post_peak_scores[-peak_decline_count:]
                    monotonic_decline = all(recent[i] >= recent[i + 1] for i in range(len(recent) - 1))
                    strong_drop = recent[-1] < best_score * peak_drop_ratio
                    if monotonic_decline and strong_drop:
                        logger.debug(
                            f"{label}: early stop after peak at z={best_z:.3f}, "
                            f"best_sharpness={best_score:.4f}"
                        )
                        break

        return samples, True

    def _autofocus_scan_window(
        self,
        center_z,
        half_range,
        step,
        *,
        settle_time,
        n_samples,
        discard_frames,
        label,
        move_speed,
        stop_after_peak=False,
        peak_drop_ratio=0.5,
        peak_decline_count=3,
        min_samples_before_stop=8,
    ):
        max_valid_z = self._autofocus_max_z()
        start = self._autofocus_clamp_z(center_z - half_range, max_valid_z)
        end = self._autofocus_clamp_z(center_z + half_range, max_valid_z)
        if start > end:
            start, end = end, start

        # Enter the window from the retracted side and work downwards, so the
        # stage stops as soon as the peak is passed instead of first dropping
        # below the focus plane.
        positions = self._autofocus_positions(start, end, step)
        return self._autofocus_scan_positions(
            positions,
            settle_time=settle_time,
            n_samples=n_samples,
            discard_frames=discard_frames,
            label=label,
            move_speed=move_speed,
            stop_after_peak=stop_after_peak,
            peak_drop_ratio=peak_drop_ratio,
            peak_decline_count=peak_decline_count,
            min_samples_before_stop=min_samples_before_stop,
        )

    def _autofocus_get_best_sample(self, samples):
        if not samples:
            return None
        return max(samples, key=lambda sample: sample["score"])

    def _autofocus_move_to_with_backlash(self, target_z, backlash=0.4, settle_time=0.2):
        target_z = self._autofocus_clamp_z(target_z)
        approach_z = self._autofocus_clamp_z(target_z + max(0.0, backlash))

        if approach_z > target_z + 1e-3:
            self._autofocus_move_to(approach_z, wait=True)
            if self.stop:
                return
            if not self._autofocus_wait(settle_time):
                return

        self._autofocus_move_to(target_z, wait=True)
        if self.stop:
            return
        self._autofocus_wait(settle_time)

    def do_autofocus_robust(self):
        logger.debug("autofocusing...")

        # The start handler clears this flag before launching the thread. A
        # close request can set it again before the worker gets CPU time, so
        # never clear it here or shutdown cancellation could be lost.
        if self.stop:
            self.autofocus_finished_signal.emit()
            return
        coarse_step = 3.0
        fine_step = 0.4
        verify_step = 0.15
        rescue_step = 0.1
        fine_half_range = 1.2
        # Shifts the fine window back against the coarse travel direction to
        # compensate the small reading lag of the sharpness measurement. The
        # coarse scan descends, so the correction moves the window upwards.
        fine_center_bias = -0.6
        verify_half_range = 0.15
        rescue_half_range = 0.1
        backlash = 0.4
        backlash_settle_time = 0.08
        coarse_settle_time = 0.06
        coarse_n_samples = 1
        coarse_discard_frames = 1
        fine_settle_time = 0.06
        fine_n_samples = 1
        fine_discard_frames = 1
        verify_settle_time = 0.08
        verify_n_samples = 1
        verify_discard_frames = 1
        rescue_settle_time = 0.08
        rescue_n_samples = 1
        rescue_discard_frames = 1
        final_n_samples = 1
        final_discard_frames = 1
        verify_trigger_ratio = 0.85
        rescue_trigger_ratio = 0.82

        descent_started = False
        focus_locked = False
        retract_z = 0.0
        try:
            self._autofocus_prepare_capture()

            max_valid_z = self._autofocus_max_z()

            # Search downwards from the retracted position instead of dropping
            # to the lowest point first. The stage then never travels deeper
            # than the focus plane, so a lens can no longer be driven onto the
            # specimen while hunting for focus.
            retract_z = self._autofocus_clamp_z(0.0, max_valid_z)
            logger.debug(
                f"autofocus retract to z={retract_z:.3f}, then descend to at most z={max_valid_z:.3f}"
            )
            self._autofocus_move_to(retract_z, wait=True, speed=constants.DEFAULT_SPEED)
            if self.stop:
                return
            # Past this point the stage can be left part-way down the search.
            descent_started = True

            coarse_positions = self._autofocus_positions(retract_z, max_valid_z, coarse_step)
            coarse_samples, ok = self._autofocus_scan_positions(
                coarse_positions,
                settle_time=coarse_settle_time,
                n_samples=coarse_n_samples,
                discard_frames=coarse_discard_frames,
                label="autofocus-coarse",
                move_speed=constants.DEFAULT_SPEED,
                stop_after_peak=True,
                peak_drop_ratio=0.7,
                peak_decline_count=3,
                min_samples_before_stop=5,
            )
            if not ok:
                self._autofocus_handle_camera_failure()
                return

            if self.stop:
                return

            best_coarse = self._autofocus_get_best_sample(coarse_samples)
            if best_coarse is None:
                logger.warning("Autofocus found no valid focus samples")
                return

            logger.debug(
                f"autofocus coarse best: z={best_coarse['z']:.3f}, sharpness={best_coarse['score']:.4f}"
            )

            fine_center_z = self._autofocus_clamp_z(best_coarse["z"] + fine_center_bias, max_valid_z)
            fine_samples, ok = self._autofocus_scan_window(
                fine_center_z,
                fine_half_range,
                fine_step,
                settle_time=fine_settle_time,
                n_samples=fine_n_samples,
                discard_frames=fine_discard_frames,
                label="autofocus-fine",
                move_speed=constants.LOW_SPEED,
                stop_after_peak=True,
                peak_drop_ratio=0.88,
                peak_decline_count=2,
                min_samples_before_stop=4,
            )
            if not ok:
                self._autofocus_handle_camera_failure()
                return
            if self.stop:
                return

            best_fine = self._autofocus_get_best_sample(fine_samples) or best_coarse
            logger.debug(
                f"autofocus fine best: z={best_fine['z']:.3f}, sharpness={best_fine['score']:.4f}"
            )

            best_final = best_fine
            self._autofocus_move_to_with_backlash(best_final["z"], backlash=backlash, settle_time=backlash_settle_time)
            focus_locked = True
            if self.stop:
                return

            final_sharpness, ok = self._autofocus_measure_sharpness(
                n_samples=final_n_samples,
                discard_frames=final_discard_frames,
            )
            if not ok:
                self._autofocus_handle_camera_failure()
                return
            final_z = float(self.axis.z)

            if final_sharpness < best_final["score"] * verify_trigger_ratio and not self.stop:
                logger.debug(
                    f"autofocus final score below expected after coarse/fine search: measured={final_sharpness:.4f}, "
                    f"expected={best_final['score']:.4f}"
                )
                verify_samples, ok = self._autofocus_scan_window(
                    best_final["z"],
                    verify_half_range,
                    verify_step,
                    settle_time=verify_settle_time,
                    n_samples=verify_n_samples,
                    discard_frames=verify_discard_frames,
                    label="autofocus-verify",
                    move_speed=constants.LOW_SPEED,
                    stop_after_peak=True,
                    peak_drop_ratio=0.96,
                    peak_decline_count=2,
                    min_samples_before_stop=3,
                )
                if not ok:
                    self._autofocus_handle_camera_failure()
                    return
                if self.stop:
                    return

                best_final = self._autofocus_get_best_sample(verify_samples) or best_final
                logger.debug(
                    f"autofocus verified best: z={best_final['z']:.3f}, sharpness={best_final['score']:.4f}"
                )

                self._autofocus_move_to_with_backlash(best_final["z"], backlash=backlash, settle_time=backlash_settle_time)
                if self.stop:
                    return

                final_sharpness, ok = self._autofocus_measure_sharpness(
                    n_samples=final_n_samples,
                    discard_frames=final_discard_frames,
                )
                if not ok:
                    self._autofocus_handle_camera_failure()
                    return
                final_z = float(self.axis.z)

            if final_sharpness < best_final["score"] * rescue_trigger_ratio and not self.stop:
                logger.debug(
                    f"autofocus final verification below rescue threshold: measured={final_sharpness:.4f}, "
                    f"expected={best_final['score']:.4f}"
                )
                rescue_samples, ok = self._autofocus_scan_window(
                    best_final["z"],
                    rescue_half_range,
                    rescue_step,
                    settle_time=rescue_settle_time,
                    n_samples=rescue_n_samples,
                    discard_frames=rescue_discard_frames,
                    label="autofocus-rescue",
                    move_speed=constants.LOW_SPEED,
                    stop_after_peak=True,
                    peak_drop_ratio=0.97,
                    peak_decline_count=2,
                    min_samples_before_stop=3,
                )
                if not ok:
                    self._autofocus_handle_camera_failure()
                    return
                if self.stop:
                    return

                measured_sample = {"z": final_z, "score": float(final_sharpness)}
                rescue_best = self._autofocus_get_best_sample(rescue_samples + [measured_sample])
                if rescue_best is not None and rescue_best["score"] > final_sharpness:
                    best_final = rescue_best
                    logger.debug(
                        f"autofocus rescue best: z={best_final['z']:.3f}, sharpness={best_final['score']:.4f}"
                    )
                    self._autofocus_move_to_with_backlash(best_final["z"], backlash=backlash, settle_time=backlash_settle_time)

            if self.current_lens:
                if self.current_device_name == "Entomoscope PI":
                    self.current_lens.base_focus_height_arducam_old = self.axis_length - (self.axis.z + self.current_lens.lenght)
                elif self.current_device_name == "Entomoscope PIs":
                    self.current_lens.base_focus_height_arducam = self.axis_length - (self.axis.z + self.current_lens.lenght)
                elif self.current_device_name == "Entomoscope PI2AI":
                    self.current_lens.base_focus_height_vaimaging = self.axis_length - (self.axis.z + self.current_lens.lenght)
                self.dump_lenses_to_file()

        except Exception as e:
            logger.exception("Error during autofocus")
            self.autofocus_error_signal.emit(f"Autofocus stopped: {e}")
        finally:
            if descent_started and not focus_locked and not self.stop:
                # No focus was reached, so the stage is parked somewhere along
                # the descent. Retract it rather than leaving the lens low over
                # the specimen. A user abort is left alone on purpose.
                try:
                    self._autofocus_move_to(retract_z, wait=True, speed=constants.DEFAULT_SPEED)
                except Exception:
                    logger.exception("Could not retract the stage after a failed autofocus")
            try:
                self._autofocus_finish_capture()
            except Exception as e:
                logger.exception("Could not restore the camera after autofocus")
                self.autofocus_error_signal.emit(
                    f"Autofocus stopped while restoring the camera: {e}"
                )
            finally:
                self.stop = False
                self.autofocus_finished_signal.emit()

    def do_autofocus(self):
        return self.do_autofocus_robust()

    def number_stacks_changed(self):
        self._number_stacks = self.number_stacks_spinbox.value()
    
    def step_size_changed(self):
        self._stack_step_size = self.step_size_spinbox.value()

    def set_status_tip(self):
        # StatusTip for single picture button
        if path := self.directory_manager.add_image(create_dir=False):
            self.take_single_picture_bttn.setStatusTip(f"Save a single picture in {os.path.abspath(path)}")
        else:
            self.take_single_picture_bttn.setStatusTip("Save a single picture")
        
        # StatusTip for stack picture button
        if img_params := self.directory_manager.add_stack_image(5, create_dir=False):
            self.take_stack_picture_bttn.setStatusTip(f"Save a stack picture in {img_params[2]}")
        else:
            self.take_stack_picture_bttn.setStatusTip("Save a stack picture")

    def show_spinner(self):
        self.progress_label.setMovie(self.spinner)
        self.spinner.start()

    def hide_spinner(self):
        self.spinner.stop()
        self.progress_label.clear()

    def take_single_picture(self):
        #self.zenodo_upload_button.setEnabled(False)
        #self.classification_results_viewer.set_text("Waiting for predictions...")
        if (
            getattr(self, "_stack_operation_active", False)
            or getattr(self, "_autofocus_active", False)
            or getattr(self, "_lens_reposition_active", False)
            or getattr(self, "_single_capture_active", False)
            or getattr(self, "_manual_move_active", False)
        ):
            self.statusBar.showMessage(
                "Wait for the current image or motor operation to finish.",
                10000,
            )
            return

        self.stop = False
        self._single_capture_active = True
        self._single_capture_control_states = []
        for name in (
            "take_single_picture_bttn",
            "take_stack_picture_bttn",
            "motor_up_button",
            "motor_down_button",
            "autofocus_button",
            "motor_reference_button",
            "cam_setting_bttn",
            "change_lens_bttn",
        ):
            widget = getattr(self, name, None)
            if widget is None:
                continue
            is_enabled = getattr(widget, "isEnabled", None)
            previous_state = bool(is_enabled()) if callable(is_enabled) else True
            self._single_capture_control_states.append((widget, previous_state))
            widget.setEnabled(False)

        self.statusBar.showMessage("Capturing a fresh image...", 10000)
        capture_thread = Thread(target=self._capture_single_image_worker)
        self._single_capture_thread = capture_thread
        try:
            capture_thread.start()
        except Exception as exc:
            logger.exception("Could not start single-image capture worker")
            self._finish_single_capture_ui()
            QMessageBox.warning(
                self,
                "Single capture could not start",
                f"ENIMAS could not start the camera capture worker: {exc}",
            )

    def _capture_single_image_worker(self):
        begin_single_capture = getattr(self.arducam, "begin_single_capture", None)
        end_single_capture = getattr(self.arducam, "end_single_capture", None)
        capture_mode_started = False
        img = None
        capture_exception = None
        try:
            if callable(begin_single_capture):
                begin_single_capture()
                capture_mode_started = True
            get_frame_counter = getattr(self.arducam, "get_frame_counter", None)
            after_counter = get_frame_counter() if callable(get_frame_counter) else None
            get_fresh_image = getattr(self.arducam, "get_fresh_image", None)
            if not callable(get_fresh_image):
                raise RuntimeError("The active camera does not support fresh-frame capture")
            img = get_fresh_image(
                discard_frames=0,
                max_attempts=constants.SINGLE_CAPTURE_MAX_ATTEMPTS,
                timeout_s=constants.SINGLE_CAPTURE_TIMEOUT,
                after_counter=after_counter,
                stop_check=lambda: bool(
                    getattr(self, "stop", False)
                    or getattr(self, "_close_pending", False)
                ),
            )
        except Exception as exc:
            capture_exception = exc
            logger.exception("Single-image fresh capture failed")
        finally:
            if capture_mode_started and callable(end_single_capture):
                try:
                    end_single_capture()
                except Exception:
                    logger.exception("Could not restore the camera after single capture")

        if img is None or getattr(self, "stop", False) or getattr(
            self, "_close_pending", False
        ):
            diagnostic = str(
                getattr(self.arducam, "last_fresh_capture_error", "") or ""
            ).strip()
            if capture_exception is not None:
                diagnostic = diagnostic or str(capture_exception)
            detail = f"\n\nCamera detail: {diagnostic}" if diagnostic else ""
            message = (
                "The camera did not return a fresh image. No file was created. "
                "You may try again."
                f"{detail}\n\nIf this happens again, send enimas-output.log "
                "and debug.log to support."
            )
            logger.error("Single capture stopped: %s", message)
            self.single_capture_error_signal.emit(message)
            return

        try:
            if self.camera_type == "VAImagingCamera":
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        except Exception as exc:
            logger.exception("Could not convert single-image camera frame")
            self.single_capture_error_signal.emit(
                f"The camera returned an unreadable image: {exc}\n\n"
                "No file was created. If this happens again, send "
                "enimas-output.log and debug.log to support."
            )
            return

        self.single_capture_ready_signal.emit(img)

    @pyqtSlot(object)
    def _handle_single_capture_ready(self, img):
        try:
            if not getattr(self, "_close_pending", False):
                self._save_single_picture(img)
        except Exception as exc:
            logger.exception("Single-image saving failed")
            if not getattr(self, "_close_pending", False):
                QMessageBox.critical(
                    self,
                    "Saving the picture failed",
                    f"ENIMAS could not save the captured image: {exc}",
                )
        finally:
            self._finish_single_capture_ui()

    @pyqtSlot(str)
    def _handle_single_capture_error(self, message):
        if not getattr(self, "_close_pending", False):
            self.statusBar.showMessage(
                "Single capture stopped: no fresh frame.",
                10000,
            )
            QMessageBox.warning(self, "Single capture stopped", message)
        self._finish_single_capture_ui()

    def _finish_single_capture_ui(self):
        self._single_capture_active = False
        if not getattr(self, "_close_pending", False):
            for widget, enabled in getattr(
                self, "_single_capture_control_states", []
            ):
                try:
                    widget.setEnabled(enabled)
                except RuntimeError:
                    # The window may already be tearing down.
                    pass
            self._update_single_capture_button()
        self._single_capture_control_states = []

    def _save_single_picture(self, img):
        img_file_path, img_dir = self.directory_manager.add_image(get_dir=True)
        
        if img_file_path is None:
            if self.directory_manager.current_dir_mode == 0:
                QMessageBox.critical(self, "Couldn't take a picture.", "No folder names are specified to save a picture. \nPlease enter a Specimen prefix, Plate ID and Specimen code number.")
            else:
                QMessageBox.critical(self, "Couldn't take a picture.", "No specimen name. \nPlease enter a folder path and a specimen name to save a picture.")
            return
            
        if not os.path.isdir(img_dir): os.makedirs(img_dir)
            
        response = cv2.imwrite(img_file_path, img)
        if not response:
            QMessageBox.critical(self, "Saving the picture failed", f"Something went wrong while saving the picture in:\n{img_file_path}")
        else:
            self.set_status_tip()
            self.save_metadata_file(img_file_path)
            if self.show_scalebar:
                self.draw_scale_bar(img_file_path)
            self.show_static_image(img_file_path)
            self.measurement_button.setEnabled(True)
            QApplication.processEvents()

            self.worker = VariantWorker(img_file_path, self.save_uniform_bg, self.replace_color, self.cropping, self.cropping_method, cpu_cores=variant_cores, nice_level=psutil.BELOW_NORMAL_PRIORITY_CLASS)
            self.worker.started.connect(lambda: self.show_spinner())
            self.worker.progress.connect(self.proc_var_progress_label.setText)
            self.worker.aborted.connect(lambda: self.proc_var_progress_label.setText("Variant processing aborted."))
            self.worker.aborted.connect(lambda: self.hide_spinner())
            self.worker.finished.connect(lambda results: self.on_variant_done(results))

            self.worker.start()
            self.variant_worker = self.worker

        self._last_image_path = img_file_path
        print(f"Debug - Setting _last_image_path (single image): {img_file_path}")
        
        #self.zenodo_upload_button.setEnabled(True)

    def stack_pictures_clicked(self):
        if self._stack_operation_active:
            self.statusBar.showMessage(
                "A stack capture or fusion operation is already running.",
                10000,
            )
            return
        if (
            getattr(self, "_autofocus_active", False)
            or getattr(self, "_lens_reposition_active", False)
            or getattr(self, "_single_capture_active", False)
            or getattr(self, "_manual_move_active", False)
        ):
            self.statusBar.showMessage(
                "Wait for the current image or motor operation to finish.",
                10000,
            )
            return

        if not self.referenced or not getattr(self.axis, "_referenced", False):
            message = "Reference the axis before taking another stack."
            self.statusBar.showMessage(message, 10000)
            QMessageBox.warning(self, "Axis reference required", message)
            return

        self.zenodo_upload_button.setEnabled(False)
        # Resolve collision-safe paths first, but do not create an empty raw
        # folder until travel preflight has passed.
        if (img_params := self.directory_manager.add_stack_image(
            self._number_stacks, create_dir=False
        )) is None:
            if self.directory_manager.current_dir_mode == 0:
                QMessageBox.critical(self, "Couldn't take a stack picture.", "No folder names are specified to save a stack picture. \nPlease enter a Specimen prefix, Plate ID and Specimen code number.")
            else:
                QMessageBox.critical(self, "Couldn't take a stack picture.", "No specimen name. \nPlease enter a specimen name to save a stack picture.")
            return
        
        img_paths, new_dir_path, stacked_path = img_params
            
        if not self.axis.check_limit(-1 * self._number_stacks * self._stack_step_size):
            self.statusBar.showMessage("Stacking not possible. The highest position is out of limit")
            return
        try:
            os.makedirs(new_dir_path)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Could not create stack folder",
                f"ENIMAS could not create the raw-frame folder:\n{new_dir_path}\n\n{exc}",
            )
            return

        self.stop = False
        self._stack_cancel_event = threading.Event()
        self._stack_operation_active = True
        self.take_single_picture_bttn.setEnabled(False)
        MessageBox.information("Taking stacking pictures. Please don't move.", abort=True, callback=self.motor_stop)
        self.set_motor_control_state(False)
        self.set_camera_lens_state(False)
        QApplication.processEvents()
        
        class Signals(QObject):
            imaging_done = pyqtSignal()
            imaging_failed = pyqtSignal(str, bool)
            stacking_done = pyqtSignal()
            stacking_failed = pyqtSignal(str)
            classification_done = pyqtSignal(list, list)
            
        # has to be bound to self.signals as signals will be garbage collected too early otherwise
        self.signals = signals = Signals()

        def finish_stack_operation():
            self._stack_operation_active = False
            self._stack_capture_thread = None
            self._active_stacker = None
            self._stack_cancel_event = None
            if getattr(self, "_close_pending", False):
                return
            self.stop = False
            self.set_motor_control_state(True)
            self.set_camera_lens_state(True)
            self._update_single_capture_button()

        # add debug information    
        def on_imaging_done():
            print("Imaging done, starting stacking...")
            self._stack_capture_thread = None
            if getattr(self, "_close_pending", False):
                finish_stack_operation()
                return
            try:
                self._active_stacker = stacker.fuse_stacks(
                    new_dir_path,
                    stacked_path,
                    self.fuse_status,
                    signals.stacking_done,
                    signals.stacking_failed,
                )
            except Exception as exc:
                logger.exception("Could not start stack fusion")
                on_stacking_failed(f"Could not start stack fusion: {exc}")

        def on_imaging_failed(message, reference_invalid=False):
            logger.error(message)
            self._last_image_path = None
            finish_stack_operation()
            if getattr(self, "_close_pending", False):
                return
            if reference_invalid:
                self.invalidate_axis_reference()
            MessageBox.hide_message()
            self.statusBar.showMessage(message, 15000)
            QMessageBox.warning(
                self,
                "Stack capture stopped",
                f"{message}\n\nIf this happens again, send enimas-output.log "
                "and debug.log to support.",
            )

        def on_stacking_failed(message):
            logger.error(message)
            self._last_image_path = None
            finish_stack_operation()
            if getattr(self, "_close_pending", False):
                return
            MessageBox.hide_message()
            self.statusBar.showMessage(message, 15000)
            QMessageBox.warning(self, "Stacking stopped", message)
        
        def on_stacking_done():
            print("Stacking done, showing image...")
            finish_stack_operation()
            if getattr(self, "_close_pending", False):
                return
            # Check if stacking was skipped (user selected "Do not stack")
            if constants.HELICON_PATH == "NoStack":
                # Stacking was skipped - copy first image outside folder and process it
                print("Stacking skipped by user choice - processing first image as single image")
                
                if img_paths and os.path.isfile(img_paths[0]):
                    first_img_in_folder = img_paths[0]
                    
                    # Create a path for the processed image outside the Stack_Frames folder
                    # Similar to stacked_path but with different naming
                    parent_dir = os.path.dirname(new_dir_path)
                    folder_name = os.path.basename(new_dir_path)
                    
                    # Extract specimen name and number from folder name
                    # Expected format: {specimen_name}_Stack_Frames_{XX}
                    if "_Stack_Frames_" in folder_name:
                        parts = folder_name.split("_Stack_Frames_")
                        specimen_name = parts[0]
                        number = parts[1] if len(parts) > 1 else "01"
                        
                        # Create output path outside the folder (same level as stacked images would be)
                        final_extension = normalize_image_extension(
                            constants.STACK_OUTPUT_EXTENSION
                        )
                        output_img_path = os.path.join(
                            parent_dir,
                            f"{specimen_name}_nostack_{number}.{final_extension}",
                        )

                        # Preserve a lossless raw frame and create the selected
                        # per-session final format outside the source folder.
                        convert_image_file(
                            first_img_in_folder,
                            output_img_path,
                            jpeg_quality=constants.STACK_JPEG_QUALITY,
                        )
                        print(f"Created final image from {first_img_in_folder} at {output_img_path}")
                        
                        # Now process the copied image (not the one in the folder)
                        # Save metadata for the copied image
                        self.save_metadata_file(output_img_path)
                        
                        # Add scale bar if enabled
                        if self.show_scalebar:
                            self.draw_scale_bar(output_img_path)
                        
                        # Show the copied image in UI
                        self.show_static_image(output_img_path)
                        
                        # Enable measurement button
                        self.measurement_button.setEnabled(True)
                        
                        # Set last image path to the copied image (outside folder)
                        self._last_image_path = output_img_path
                        print(f"Debug - Setting _last_image_path (copied image): {output_img_path}")
                        
                        # Process variants (uniform background, cropping, etc.) on copied image
                        self.worker = VariantWorker(output_img_path, self.save_uniform_bg, self.replace_color, self.cropping, self.cropping_method, cpu_cores=variant_cores, nice_level=psutil.BELOW_NORMAL_PRIORITY_CLASS)
                        self.worker.started.connect(lambda: self.show_spinner())
                        self.worker.progress.connect(self.proc_var_progress_label.setText)
                        self.worker.aborted.connect(lambda: self.proc_var_progress_label.setText("Variant processing aborted."))
                        self.worker.aborted.connect(lambda: self.hide_spinner())
                        self.worker.finished.connect(lambda results: self.on_variant_done(results))

                        self.worker.start()
                        self.variant_worker = self.worker
                else:
                    # If no images captured, just return to normal state
                    MessageBox.hide_message()
                return
            
            # Normal stacking flow - stacked image was created
            self.save_metadata_file(stacked_path)
            if self.show_scalebar:
                self.draw_scale_bar(stacked_path)
            self.show_static_image(stacked_path)
            self.measurement_button.setEnabled(True)
           
            self.worker = VariantWorker(stacked_path, self.save_uniform_bg, self.replace_color, self.cropping, self.cropping_method, cpu_cores=variant_cores, nice_level=psutil.BELOW_NORMAL_PRIORITY_CLASS)
            self.worker.started.connect(lambda: self.show_spinner())
            self.worker.progress.connect(self.proc_var_progress_label.setText)
            self.worker.aborted.connect(lambda: self.proc_var_progress_label.setText("Variant processing aborted."))
            self.worker.aborted.connect(lambda: self.hide_spinner())
            self.worker.finished.connect(lambda results: self.on_variant_done(results))

            self.worker.start()
            self.variant_worker = self.worker
  
        signals.imaging_done.connect(on_imaging_done)
        signals.imaging_failed.connect(on_imaging_failed)
        signals.stacking_done.connect(on_stacking_done)
        signals.stacking_failed.connect(on_stacking_failed)
        
        self._last_image_path = stacked_path
        print(f"Debug - Setting _last_image_path (stacked): {stacked_path}")
        
        stacking_thread = Thread(
            target=self.capture_source_images_for_stacking,
            args=(
                img_paths,
                signals.imaging_done,
                signals.imaging_failed,
                self._stack_cancel_event,
            ),
        )
        self._stack_capture_thread = stacking_thread
        # zenodo_btn_activator_thread = Thread(target=self.activate_zenodo_btn_after_stacking, args=(stacked_path, ))
        try:
            stacking_thread.start()
        except Exception as exc:
            logger.exception("Could not start stack capture thread")
            on_imaging_failed(f"Could not start stack capture: {exc}", False)
        # zenodo_btn_activator_thread.start()

    def activate_zenodo_btn_after_stacking(self, path):
        while True:
            if os.path.isfile(path):
                self.zenodo_upload_button.setEnabled(True)
                break
        
    def save_metadata_file(self, img_path: Path | str):
        """
        Save metadata information to a text file associated with an image.

        This method creates a metadata text file in the same directory as the specified image file
        with the image file's name appended with "_metadata.txt".

        Args:
            img_path (Path | str): The path to the image file for which the metadata is to be saved. 
                                This can be either a Path object or a string representing the path.

        Example:
            If the image path is "images/photo.jpg", the metadata file will be saved as "images/photo_metadata.txt"
            and will contain information like:
            
            Date:
            2024-07-08T14:22:05

            Lens:
            Canon EF 50mm f/1.8

            Step size:
            5.0 mm

            Number of steps:
            20
        """
        img_path = Path(img_path).resolve()
        metadata_file_path = img_path.parent / f"{img_path.stem}_metadata.txt"

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

        if self.drawn_box is not None:
            center_drawn, size_drawn, angle_drawn = self.drawn_box  
            box_coordinate_drawn = (center_drawn[0], center_drawn[1], size_drawn[0], size_drawn[1], angle_drawn)
        else:
            box_coordinate_drawn = (0, 0, 0, 0)
        
        scale_used = self.current_scale if is_valid_scale(self.current_scale) else None
        #orientation = 
        width_mm = f"{self.static_label.width_mm} mm" if self.static_label.width_mm is not None else "N/A"
        height_mm = f"{self.static_label.height_mm} mm" if self.static_label.height_mm is not None else "N/A"
        primary = getattr(self, "current_orientation_primary", "Undefined")
        secondary = getattr(self, "current_orientation_secondary", "Undefined")
        # information to write inside the metadata txt file
        metadata = {
            "Date": datetime.now().isoformat(timespec='seconds'),
            "Camera": self.camera_type,
            "Step size": f"{self._stack_step_size} mm",
            "Number of steps": self._number_stacks,
            "Lens": self.current_lens.label_str
        }
        additional_metadata = {
            "Scale": f"{scale_used:g} mm/pixel" if scale_used else "Not calibrated",
            "Bounding box Coordinates for padded image(x,y,height,width)": box_coordinate,
            "Bounding box Coordinates for original image(x,y,height,width)": box_coordinate_original,
            "Bounding box Coordinates drawn manually (x,y,width,height)": box_coordinate_drawn,
            "Width (mm)": width_mm,
            "Height (mm)": height_mm,
            "Primary orientation": primary,
            "Secondary orientation": secondary,
        }
        # save metadata txt file
        with metadata_file_path.open('w') as file:
            for key, value in metadata.items():
                file.write(f"{key}:\n")
                file.write(f"{value}\n\n")
            for key, value in additional_metadata.items():
                file.write(f"{key}:\n")
                file.write(f"{value}\n\n")

    def capture_source_images_for_stacking(
        self,
        img_paths: List[str],
        done_signal: pyqtBoundSignal,
        failed_signal: pyqtBoundSignal,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Take fresh stack frames while honoring one immutable cancellation token."""
        logger.info("Stack thread running")
        cancel_event = cancel_event or threading.Event()
        is_cancelled = lambda: cancel_event.is_set() or self.stop
        stage_start_position = self.axis.z
        stage_motion_started = False

        failure_message = None
        reference_invalid = False
        camera = getattr(self, "arducam", None)
        begin_stack_capture = getattr(camera, "begin_stack_capture", None)
        end_stack_capture = getattr(camera, "end_stack_capture", None)
        capture_mode_started = False
        try:
            if callable(begin_stack_capture):
                begin_stack_capture()
                capture_mode_started = True
            if is_cancelled():
                raise StackCaptureCancelled("Stack capture was cancelled")
            speed = self.stack_speed_spinbox.value() * constants.LOW_SPEED / 100
            self.axis.set_speed(speed)

            # Exercise the stage before capture to settle backlash. Keep this
            # inside the guarded operation so motor failures cannot leave the UI
            # permanently in its busy state.
            if constants.PRE_STACK_MOVEMENT_STEPS > 0:
                logger.info(
                    "Pre-stacking movement: moving down %s steps",
                    constants.PRE_STACK_MOVEMENT_STEPS,
                )
                stage_motion_started = True
                self.motor_down(
                    steps=constants.PRE_STACK_MOVEMENT_STEPS * self._stack_step_size,
                    wait=True,
                )

                logger.info(
                    "Pre-stacking movement: moving up %s steps",
                    constants.PRE_STACK_MOVEMENT_STEPS,
                )
                for _step in range(constants.PRE_STACK_MOVEMENT_STEPS):
                    if is_cancelled():
                        raise StackCaptureCancelled("Stack capture was cancelled")
                    self.motor_up(wait=True)
                    sleep(constants.STACK_SETTLING_DELAY)

                logger.info(
                    "Pre-stacking: waiting %ss for final settling",
                    constants.STACK_SETTLING_DELAY,
                )
                sleep(constants.STACK_SETTLING_DELAY)

            destinations = img_paths[: self._number_stacks]
            if len(destinations) != self._number_stacks:
                raise StackCaptureError(
                    f"Expected {self._number_stacks} stack destinations, got {len(destinations)}"
                )
            def move_next():
                nonlocal stage_motion_started
                stage_motion_started = True
                self.motor_up(wait=True)

            captured = capture_stack_sequence(
                self.arducam,
                self.camera_type,
                destinations,
                move_next=move_next,
                restore_distance=lambda distance: self.motor_down(
                    steps=distance, wait=True
                ),
                step_distance=self._stack_step_size,
                settle_delay=constants.STACK_SETTLING_DELAY,
                discard_frames=constants.STACK_CAPTURE_DISCARD_FRAMES,
                max_attempts=constants.STACK_CAPTURE_MAX_ATTEMPTS,
                timeout_s=constants.STACK_CAPTURE_TIMEOUT,
                stop_check=is_cancelled,
            )
            if captured != self._number_stacks:
                raise StackCaptureError(
                    f"Captured {captured} of {self._number_stacks} required stack frames"
                )
        except StackCaptureCancelled:
            reference_invalid = stage_motion_started
            failure_message = (
                "Stack capture was cancelled. Any partial source frames were kept "
                "for diagnosis; no stacked image was created."
            )
            if reference_invalid:
                failure_message += (
                    " The stage position is no longer trusted; reference the axis "
                    "before taking another stack."
                )
                logger.warning(
                    "Stack cancelled after stage movement (start=%s, tracked=%s); "
                    "axis reference invalidated",
                    stage_start_position,
                    self.axis.z,
                )
        except StackCaptureError as exc:
            reference_invalid = stage_motion_started and self.stop
            failure_message = (
                f"Stack capture stopped: {exc}. Any partial source frames were kept "
                "for diagnosis; no stacked image was created."
            )
            if reference_invalid:
                failure_message += (
                    " The return movement was interrupted; reference the axis "
                    "before taking another stack."
                )
        except Exception as exc:
            logger.exception("Unexpected stack capture failure")
            reference_invalid = stage_motion_started
            failure_message = (
                f"Stack capture stopped unexpectedly: {exc}. Any partial source "
                "frames were kept for diagnosis; no stacked image was created."
            )
            if reference_invalid:
                failure_message += (
                    " The stage position may be uncertain; reference the axis "
                    "before taking another stack."
                )
        finally:
            try:
                self.axis.set_speed(constants.LOW_SPEED)
            except Exception as exc:
                logger.exception("Could not restore the default stage speed")
                reference_invalid = stage_motion_started
                if failure_message is None:
                    failure_message = (
                        f"Stack capture stopped: could not restore the stage speed "
                        f"({exc})."
                    )
            if capture_mode_started and callable(end_stack_capture):
                try:
                    end_stack_capture()
                except Exception as exc:
                    logger.exception("Could not restore normal camera error handling")
                    if failure_message is None:
                        failure_message = (
                            "Stack capture stopped: could not restore normal camera "
                            f"error handling ({exc})."
                        )
            self.hide_message_signal.emit()

        logger.info("Stack thread ending")
        if failure_message is not None:
            failed_signal.emit(failure_message, reference_invalid)
            return
        done_signal.emit()

    def on_variant_done(self, results):
        """Callback method when the variant processing is done."""
        self.hide_spinner()
        self.results = results
        variants = [
            ("Original",                "Original"),
            ("Cropped",        "Cropped"),
            ("Uniform",        "Uniform"),
            ("Cropped_Uniform","Cropped_Uniform"),
        ]
        self.variant_combo.clear()
        for key, label in variants:
            p = self.results.get(key)
            if p and os.path.exists(p):
                if key == "Original":
                    name = os.path.basename(p)
                    label = f"{label} ({name})"
                self.variant_combo.addItem(label, p)
        if self.variant_combo.count():
            self.variant_combo.setCurrentIndex(0)
            self.update_classify_enable(0)
            
        # Update Zenodo button state after processing is complete
        self.update_zenodo_button_state()
        
        self.proc_var_progress_label.setText("Variant processing done.")
    
    def update_classify_enable(self, index):
        model = self.classification_model_selector.currentData() 
        path = self.variant_combo.currentData() 
        enable = (model is not None) and (path is not None)
        self.classify_button.setEnabled(enable)

    def on_classify(self):
        """Classify the selected image variant using the selected ML models."""
        self.classify_button.setEnabled(False)
        img_path = self.variant_combo.currentData()
        if self.classification_model_selector.currentData() is None:
            self.statusBar.showMessage("Please select models for classification first.")
            return

        class Signals(QObject):
            classification_done = pyqtSignal(list, list)
            
        # has to be bound to self.signals as signals will be garbage collected too early otherwise
        self.signals = signals = Signals()
        if selected_ml_models := self.classification_model_selector.currentData():
            predictor = Predictor(img_path, selected_ml_models, self.fuse_status, signals.classification_done)
            signals.classification_done.connect(lambda predictions, class_names: self.classification_results_viewer.set_text(predictions, class_names))
            signals.classification_done.connect(lambda *_: self.classify_button.setEnabled(True))
            predictor.start()
     
    def return_to_livefeed(self):

        if hasattr(self, "detect_worker") and self.detect_worker is not None:
            self.detect_worker.abort()
            self.detect_worker = None
            self.movie.stop()
            self.measurement_button.setIcon(QIcon())
            self.measurement_button.setProperty("processing", "False")
            self.measurement_button.style().unpolish(self.measurement_button)
            self.measurement_button.style().polish(self.measurement_button)
       
        self.showing_static_image = False
        self.measurement_button.setEnabled(False)
        self.static_label.drawing_enabled = False
        self.confirm_bttn.setEnabled(False)
        self.clear_rectangle_bttn.setEnabled(False)
        self.scroll_area.hide()
        self.livefeed_bttn.hide()
        self.delete_bttn.hide()
        self.confirm_bttn.hide()
        self.clear_rectangle_bttn.hide()
        self.save_annotated_bttn.hide()
        
        # Hide zoom controls
        self.zoom_in_bttn.hide()
        self.zoom_out_bttn.hide()
        self.zoom_fit_bttn.hide()
        self.zoom_level_label.hide()
        
        if self.device_selected:
            self._update_single_capture_button()
            if self.referenced:
                self.take_stack_picture_bttn.setEnabled(True)
        self.classification_results_viewer.clear()
        self.variant_combo.clear()
        self.classify_button.setEnabled(False)
        on_names = [name for name, on in self.image_settings_dialog.states.items() if on]
        if on_names:
            self.proc_var_progress_label.setText(f"{', ' .join(on_names)} selected")
        else:
            self.proc_var_progress_label.setText("No options selected")

    def eventFilter(self, source, event):
        """Event filter to handle resizing of the video label and scroll area."""
        if source == self.video_label and event.type() == QEvent.Resize:
            self.scroll_area.setGeometry(self.video_label.geometry())
            # Scroll area handles the static_label sizing

        return super().eventFilter(source, event)
        
    def delete_last_image(self):
        msg = QMessageBox()
        msg.setWindowTitle("please wait")
        msg.setWindowIcon(QIcon(r"UserInterface\imgs\enimas_icon.png"))
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText("Trying to stop processing image variants and delete the last image.")
        msg.setStandardButtons(QMessageBox.StandardButton.NoButton)
        msg.show()
        QApplication.processEvents()
        if hasattr(self, "variant_worker") and self.variant_worker is not None:
            self.variant_worker.abort()
            self.variant_worker = None
            self.proc_var_progress_label.setText("Variant processing aborted.")

        if not self._last_image_path or not os.path.exists(self._last_image_path):
            return
        
        # Handle case where _last_image_path is a folder (legacy - should not happen anymore)
        if os.path.isdir(self._last_image_path):
            try:
                shutil.rmtree(self._last_image_path)
                logger.info(f"Deleted raw data folder (no stacking): {self._last_image_path}")
                msg.close()
                self.return_to_livefeed()
                self.static_label.clear_rectangle()
                return
            except Exception as e:
                logger.error(f"Error deleting folder {self._last_image_path}: {e}")
                msg.close()
                return
        
        last = Path(self._last_image_path)
        
        # Common suffixes for all image types
        suffixs = [
            "",
            "_metadata",
            "_annotated",
            "_classification",
            "_uniform",
            "_cropped",
            "_uniform_cropped",
            "_uniform_classification",
            "_uniform_cropped_classification",
            "_cropped_classification"
        ]
        
        # Check if this is a "nostack" image (when "Do not stack" was selected)
        is_nostack_image = "_nostack_" in os.path.basename(self._last_image_path)
        
        if is_nostack_image:
            # This is a "nostack" image - delete the image and all its variants first
            for suf in suffixs:
                pattern = f"{last.stem}{suf}.*"
                for f in last.parent.glob(pattern):
                    try:
                        f.unlink()
                        logger.debug(f"Deleted file {f}")
                    except Exception as e:
                        logger.error(f"Error deleting file {f}: {e}")
            
            # Extract the specimen name and number from the nostack image filename
            # Expected format: {specimen_name}_nostack_{XX}.{extension}
            nostack_filename = os.path.basename(self._last_image_path)
            parts = nostack_filename.split('_nostack_')
            
            if len(parts) == 2:
                specimen_name = parts[0]
                number_part = parts[1].split('.')[0]  # Get the number without extension
                
                # Look for and delete the corresponding stack frames folder
                stack_folder_pattern = f"{specimen_name}_Stack_Frames_{number_part}"
                stack_folder_path = os.path.join(os.path.dirname(self._last_image_path), stack_folder_pattern)
                
                if os.path.exists(stack_folder_path) and os.path.isdir(stack_folder_path):
                    try:
                        shutil.rmtree(stack_folder_path)
                        logger.info(f"Deleted Stack_Frames folder for nostack image: {stack_folder_path}")
                    except Exception as e:
                        logger.error(f"Error deleting Stack_Frames folder {stack_folder_path}: {e}")
        
        elif "stacked" in os.path.basename(self._last_image_path):
            # For stacked images, delete the stacked image and its derivatives first
            for suf in suffixs:
                pattern = f"{last.stem}{suf}.*"
                for f in last.parent.glob(pattern):
                    try:
                        f.unlink()
                        logger.debug(f"Deleted file {f}")
                    except Exception as e:
                        logger.error(f"Error deleting file {f}: {e}")
            
            # Also delete the associated stack frames folder
            # Extract the specimen name and number from the stacked image filename
            # Expected format: {specimen_name}_stacked_{XX}.{extension}
            stacked_filename = os.path.basename(self._last_image_path)
            parts = stacked_filename.split('_stacked_')
            
            if len(parts) == 2:
                specimen_name = parts[0]
                number_part = parts[1].split('.')[0]  # Get the number without extension
                
                # Look for and delete the corresponding stack frames folder
                stack_folder_pattern = f"{specimen_name}_Stack_Frames_{number_part}"
                stack_folder_path = os.path.join(os.path.dirname(self._last_image_path), stack_folder_pattern)
                
                if os.path.exists(stack_folder_path) and os.path.isdir(stack_folder_path):
                    try:
                        shutil.rmtree(stack_folder_path)
                        logger.info(f"Deleted stack frames folder {stack_folder_path}")
                    except Exception as e:
                        logger.error(f"Error deleting stack frames folder {stack_folder_path}: {e}")
        else:
            # For non-stacked, non-nostack images (regular single images), just delete the files with the known suffixes
            for suf in suffixs:
                pattern = f"{last.stem}{suf}.*"
                for f in last.parent.glob(pattern):
                    try:
                        f.unlink()
                        logger.debug(f"Deleted file {f}")
                    except Exception as e:
                        logger.error(f"Error deleting file {f}: {e}")


        self.return_to_livefeed()
        self.static_label.clear_rectangle()
        msg.close()

    def show_static_image(self, img_file_path):
        self.take_single_picture_bttn.setEnabled(False)
        self.take_stack_picture_bttn.setEnabled(False)
        self.static_label.clear_rectangle()
        
        img = cv2.imread(img_file_path)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB) 
        self.static_label.cv_img = img

        if img is None:
            QMessageBox.critical(self, "Error", f"Unable to read image from:\n{img_file_path}")
            return
        
        # Enable fit-to-window mode for new images
        self.static_label.fit_to_window = True
        
        # Show the scroll area first to get correct viewport size
        self.scroll_area.show()
        self.scroll_area.raise_()
        QApplication.processEvents()  # Ensure the scroll area is rendered
        
        # Set the image with viewport size to calculate fit-to-window zoom
        viewport_size = self.scroll_area.viewport().size()
        self.static_label.set_image(img, viewport_size)
        
        # Update the zoom label to show actual zoom percentage
        self.update_zoom_label()

        self.showing_static_image = True

        self.livefeed_bttn.show()
        self.delete_bttn.show()
        self.confirm_bttn.show()
        self.clear_rectangle_bttn.show()
        
        # Show zoom controls
        self.zoom_in_bttn.show()
        self.zoom_out_bttn.show()
        self.zoom_fit_bttn.show()
        self.zoom_level_label.show()
        self.update_zoom_label()

    def change_stack_mode_menu(self, pos):
        menu = QMenu()
        
        change_option = menu.addAction("Change fuse method...")
        change_option.triggered.connect(method_selection.prompt_user_fuse_mode)
        
        menu.exec(self.take_stack_picture_bttn.mapToGlobal(pos))
    
    def load_lenses_from_file(self):
        if not self.is_entomoscope_connected:
            return
        """Loads the lens informations from the JSON file."""
        if not "lenses.json" in os.listdir():
            # create a default lens
            self.all_lenses.append(Lens("Default lens", 150, self.default_cam_values))
            self.current_lens = self.all_lenses[0]
        else:
            with open("lenses.json", "r") as file:
                json_file = load(file)
                
                if not "lenses" in json_file:
                    self.all_lenses.append(Lens("Default lens", 150, self.default_cam_values))
                else:
                    for lens in json_file["lenses"]:
                        self.all_lenses.append(Lens(**lens))
                
                if not "current" in json_file:
                    self.current_lens = self.all_lenses[0]
                else:
                    self.current_lens = [lens for lens in self.all_lenses if lens.name == json_file["current"]][0]
        
        self.set_settings(self.current_lens.settings)
        #self.set_new_limit()

    def dump_lenses_to_file(self):
        """Writes the list of Lenses to a JSON file with their camera settings"""
        with open("lenses.json", "w") as file:
            dump({"lenses": [lens.__dict__ for lens in self.all_lenses], "current": self.current_lens.name}, file, indent=4)
   
    def open_lens_dialog(self):
        """Opens a QDialog to change or add new lenses"""

        if "lens_dialog" in self.__dict__: 
            # close the other window if there is any
            self.lens_dialog.close()

        self.lens_dialog = LensDialog(self)
        self.lens_dialog.show()

    def update_lens_list(self, new_list):
        """Called from the QDialog with a new list of lenses"""
        self.all_lenses = new_list
    
    def set_new_limit(self):
        """Updates the soft limit with the current lens lenght"""
        # new_limit = 422 - (self.current_lens.lenght + 59) - 20 # see constants.py
        if self.axis_length == 0:
            print("Axis length is 0, no new limit.")
            return
        new_limit = self.axis_length - self.current_lens.lenght + self.sensor_distance
        # A lens shorter than the sensor-to-base-plate distance makes the
        # formula exceed the physical travel, which would let the stage be
        # driven past its lowest point. The travel always wins.
        new_limit = min(new_limit, self.axis_length)
        self.axis.set_current_limit(new_limit)
        logger.debug(f"New limit set to {new_limit}mm for lens: {self.current_lens.name}")
        print(f"New limit set to {new_limit}mm for lens: {self.current_lens.name}")

    def set_current_lens(self, lens: Lens):
        """called after the lens changed or after a new one has been added"""
        if (
            getattr(self, "_close_pending", False)
            or getattr(self, "_stack_operation_active", False)
            or getattr(self, "_autofocus_active", False)
            or getattr(self, "_lens_reposition_active", False)
            or getattr(self, "_single_capture_active", False)
            or getattr(self, "_manual_move_active", False)
        ):
            self.statusBar.showMessage(
                "Wait for the current image or motor operation before changing the lens.",
                10000,
            )
            return False

        is_new_lens = (self.current_lens != lens)
        
        self.current_lens = lens
        self.lens_info_label.setText(lens.label_str)
        if self.current_device_name == "Entomoscope PI":
            self.current_lens.base_focus_height = self.current_lens.base_focus_height_arducam_old
        elif self.current_device_name == "Entomoscope PIs":
            self.current_lens.base_focus_height = self.current_lens.base_focus_height_arducam
        elif self.current_device_name == "Entomoscope PI2AI":
            self.current_lens.base_focus_height = self.current_lens.base_focus_height_vaimaging
            
        self.current_scale = lens_scale_for_camera(self.current_lens, self.camera_type)
        self.current_lens.scale = self.current_scale
        scale_text = f"{self.current_scale:g} mm/pixel" if self.current_scale else "not calibrated"
        self.lens_info_label.setText(
            f"{self.current_lens.label_str}\nScale: {scale_text}"
        )
        
        # load the settings of the just selected lens
        self.set_settings(self.current_lens.settings)
        
        self.set_new_limit()
        self.dump_lenses_to_file()
        
        if is_new_lens and self.current_lens.base_focus_height is not None:
            self.lens_approved = self.approve_lens(lens, "The camera is going to move to the last saved focus point.")
            if not self.lens_approved:
                return
            
            # a base focus height has been saved for this lens
            # the motor will be moved to this focus height
            # focus_height = 422 - (59 + self.current_lens.lenght + self.current_lens.base_focus_height) # old
            # focus_height = self.axis_length - (self.current_lens.lenght + self.current_lens.base_focus_height)
            
            MessageBox.information(f"Moving camera to the focus point of lens: {self.current_lens.name}", abort=True, callback=self.motor_stop)
            QApplication.processEvents()
            self.stop = False
            self._lens_reposition_active = True
            self._lens_reposition_control_states = []
            for name in (
                "take_single_picture_bttn",
                "take_stack_picture_bttn",
                "motor_up_button",
                "motor_down_button",
                "autofocus_button",
                "motor_reference_button",
                "cam_setting_bttn",
                "change_lens_bttn",
            ):
                widget = getattr(self, name, None)
                if widget is None:
                    continue
                is_enabled = getattr(widget, "isEnabled", None)
                previous_state = bool(is_enabled()) if callable(is_enabled) else True
                self._lens_reposition_control_states.append(
                    (widget, previous_state)
                )
            self.set_motor_control_state(False)
            self.set_camera_lens_state(False)
            self._update_single_capture_button()
            focus_height = self.axis_length - (
                self.current_lens.lenght + self.current_lens.base_focus_height
            )
            reposition_thread = Thread(
                target=self.move_to_focus_hight,
                args=(focus_height,),
            )
            self._lens_reposition_thread = reposition_thread
            try:
                reposition_thread.start()
            except Exception as exc:
                logger.exception("Could not start saved-focus movement worker")
                self._finish_lens_reposition_ui()
                QMessageBox.warning(
                    self,
                    "Saved focus movement could not start",
                    f"ENIMAS could not start the motor movement: {exc}",
                )
    
    def move_to_focus_hight(self, focus_height=None):
        try:
            if focus_height is None:
                focus_height = self.axis_length - (
                    self.current_lens.lenght
                    + self.current_lens.base_focus_height
                )
            # focus_height = 422 - (59 + self.current_lens.lenght + self.current_lens.base_focus_height) # old
            if not getattr(self, "stop", False):
                self.move_motor_to(focus_height, wait=True)
        except Exception as exc:
            logger.exception("Saved-focus motor movement failed")
            self.lens_reposition_error_signal.emit(
                f"The camera could not move to the saved focus point: {exc}"
            )
        finally:
            self.lens_reposition_finished_signal.emit()

    def set_settings(self, settings):
        """Sets the given camera settings in the ArduCam"""
        if self.camera is None:
            #print("Entomoscope not connected")
            return
        self.arducam.camera.set_exposure_time(settings[0])
        self.arducam.camera.set_analog_gain(settings[1])
        self.arducam.camera.set_wb_gains_factor(settings[2], settings[3], settings[4])

    def open_settings_dialog(self):
        """Opens the camera settings dialog with default or current settings"""
        change_methods = {"gain": self.arducam.camera.set_analog_gain, 
                         "exp": self.arducam.camera.set_exposure_time,
                         "wb": self.arducam.camera.set_wb_gains_factor,
                         "auto_wb": self.arducam.white_balance,
                         "auto_exp": self.arducam.exposure_time}
        if self.camera_type == "VAImagingCamera":
            change_methods.update({
                "auto_gain": self.arducam.auto_gain,
                "gamma_enable": self.arducam.gamma_enable,
            })
        
        if "settings_dialog" in self.__dict__: 
            # close the other window if there is any
            self.settings_dialog.close()
        
        self.settings_dialog =   SettingsDialog(self, self.current_lens.settings, change_methods)
        if hasattr(self, "gamma_checked"):
            self.settings_dialog.gamma_enable_check.setChecked(self.gamma_checked)
        self.settings_dialog.show() 

    def open_image_settings_dialog(self):
        if "image_settings_dialog" in self.__dict__: 
            # close the other window if there is any
            self.image_settings_dialog.close()
        self.image_settings_dialog.show() 

    def save_cam_values(self, values):
        """Called after the settings have been changed"""
        if self.current_lens is not None:
            # change the settings of the current lens
            for lens in self.all_lenses:
                if lens.name == self.current_lens.name:
                    lens.settings = values
                    self.current_lens.settings = values
        # save the new settings in the json file
        self.dump_lenses_to_file()

    def camera_connection_error(self):
        if getattr(self, "_close_pending", False) or not self.isVisible():
            return
        QMessageBox.critical(self, "ENIMAS: Camera error", "The connection to the Entomoscope camera failed.\nPlease restart the program.")
        self.close()
    
    @pyqtSlot()
    def camera_connection_error_during_autofocus(self):
        """Handle camera connection error during autofocus without closing the application"""
        if getattr(self, "_close_pending", False) or not self.isVisible():
            return
        QMessageBox.warning(self, "ENIMAS: Camera warning", 
                           "The connection to the camera was temporarily lost during autofocus.\n"
                           "The autofocus operation has been aborted.\n"
                           "You may try again or continue working.\n\n"
                           "If this happens again, send enimas-output.log and debug.log to support.")

    @pyqtSlot(str)
    def _show_autofocus_error(self, message):
        if getattr(self, "_close_pending", False) or not self.isVisible():
            return
        QMessageBox.warning(
            self,
            "ENIMAS: Autofocus stopped",
            f"{message}\n\nYou may focus manually and continue working. "
            "If this happens again, send enimas-output.log and debug.log to support.",
        )

    @pyqtSlot()
    def _finish_autofocus_ui(self):
        self._autofocus_active = False
        if getattr(self, "_close_pending", False):
            return

        MessageBox.hide_message()
        self.set_motor_control_state(True)
        set_camera_lens_state = getattr(self, "set_camera_lens_state", None)
        if callable(set_camera_lens_state):
            set_camera_lens_state(True)
        self._update_single_capture_button()

    @pyqtSlot(str)
    def _show_lens_reposition_error(self, message):
        if getattr(self, "_close_pending", False) or not self.isVisible():
            return
        QMessageBox.warning(
            self,
            "Saved focus movement stopped",
            f"{message}\n\nFocus manually before taking images. If this "
            "happens again, send enimas-output.log and debug.log to support.",
        )

    @pyqtSlot()
    def _finish_lens_reposition_ui(self):
        self._lens_reposition_active = False
        if getattr(self, "_close_pending", False):
            self._lens_reposition_control_states = []
            return

        MessageBox.hide_message()
        for widget, enabled in getattr(
            self, "_lens_reposition_control_states", []
        ):
            try:
                widget.setEnabled(enabled)
            except RuntimeError:
                # The window may already be tearing down.
                pass
        self._update_single_capture_button()
        self._lens_reposition_control_states = []

    @pyqtSlot(str)
    def _show_manual_move_error(self, message):
        if getattr(self, "_close_pending", False) or not self.isVisible():
            return
        QMessageBox.warning(
            self,
            "Manual focus movement stopped",
            f"{message}\n\nReference the axis if its position is uncertain.",
        )

    @pyqtSlot()
    def _finish_manual_move_ui(self):
        self._manual_move_active = False
        if getattr(self, "_close_pending", False):
            self._manual_move_control_states = []
            return

        self.stop = False
        for widget, enabled in getattr(self, "_manual_move_control_states", []):
            try:
                widget.setEnabled(enabled)
            except RuntimeError:
                # The window may already be tearing down.
                pass
        self._manual_move_control_states = []
        self._update_single_capture_button()
        self.statusBar.showMessage("Manual focus adjustment complete.", 5000)

    def show_orientation_menu(self):
        self.orientation_dialog = OrientationDialog(self)
        self.orientation_dialog.setOrientation(self.current_orientation_primary, self.current_orientation_secondary)
        #self.orientation_dialog.show()
        if self.orientation_dialog.exec() == QDialog.Accepted:
            primary, secondary = self.orientation_dialog.getOrientation()
            self.current_orientation_primary = primary
            self.current_orientation_secondary = secondary

    def update_icon(self):
        pix = self.movie.currentPixmap()
        if not pix.isNull():
            self.measurement_button.setIcon(QIcon(pix))
    
    def zoom_in_image(self):
        """Zoom in the static image"""
        if self.static_label.zoom_in():
            self.update_zoom_label()
    
    def zoom_out_image(self):
        """Zoom out the static image"""
        if self.static_label.zoom_out():
            self.update_zoom_label()
    
    def fit_to_window_image(self):
        """Fit image to window"""
        self.static_label.fit_to_window = True
        viewport_size = self.scroll_area.viewport().size()
        self.static_label.calculate_fit_to_window_zoom(viewport_size)
        self.static_label.update_display()
        self.update_zoom_label()
    
    def update_zoom_label(self):
        """Update the zoom level label"""
        zoom_percent = int(self.static_label.zoom_factor * 100)
        if self.static_label.fit_to_window:
            self.zoom_level_label.setText(f"Zoom: {zoom_percent}% (Fit)")
        else:
            self.zoom_level_label.setText(f"Zoom: {zoom_percent}%")

    def measurement_clicked(self):
        """Handle measurement button click event."""
        # Never produce physical measurements from a guessed calibration.
        self.current_scale = lens_scale_for_camera(self.current_lens, self.camera_type)
        self.current_lens.scale = self.current_scale
        if not is_valid_scale(self.current_scale):
            QMessageBox.warning(
                self,
                "Lens calibration required",
                "This camera and lens do not have a valid scale. Open Lens settings, "
                "select the lens, and enter or calculate Scale (mm/pixel).",
            )
            return

        # Return to fit-to-window mode before starting measurement
        self.fit_to_window_image()
        self.static_label.calibrated_mm_per_pixel = self.current_scale
        self.movie.jumpToFrame(0)
        self.movie.start()
        self.measurement_button.setProperty("processing", "True")
        self.measurement_button.style().unpolish(self.measurement_button)
        self.measurement_button.style().polish(self.measurement_button)
        QApplication.processEvents()
        if not self._last_image_path or not os.path.exists(self._last_image_path):
            QMessageBox.warning(self, "No Image", "Please take a picture first before measuring.")
            return
            
        try:
            # disable the button to prevent duplicate clicks
            self.measurement_button.setEnabled(False)
            original_image = cv2.imread(self._last_image_path)
            self.padded_image = ImageUtils.pad_image_with_detected_color(original_image, 0.1)


            self.detect_worker = DetectionWorker(self._last_image_path, parent=self)
            self.detect_worker.started.connect(lambda: self.statusBar.showMessage("Starting detection..."))
            self.detect_worker.started.connect(lambda: self.movie.start())
            self.detect_worker.finished.connect(lambda detected_box: self.process_with_obbeditor(detected_box))
            self.detect_worker.finished.connect(lambda: self.confirm_bttn.setEnabled(True))
            self.detect_worker.finished.connect(lambda: self.clear_rectangle_bttn.setEnabled(True))
            self.detect_worker.aborted.connect(lambda msg: self.statusBar.showMessage(msg))
            self.detect_worker.start()
            self.current_detect_worker = self.detect_worker

        except Exception as e:
            QMessageBox.critical(self, "Measurement Error", f"Error during measurement: {str(e)}")
            # when an exception occurs, re-enable the button
            self.measurement_button.setEnabled(True)

    def process_with_obbeditor(self, detected_box):
        """Process an image with OBBEditor for detection."""
        # detected_box = self.detector.perform_detection(image_path, padding_percentage=0.1)

        print("worker finished, processing result")

        try:
            self.movie.stop()
            self.measurement_button.setIcon(QIcon())
            self.measurement_button.setProperty("processing", False)
            self.measurement_button.style().unpolish(self.measurement_button)
            self.measurement_button.style().polish(self.measurement_button)

            # print(f"Detected box: {detected_box}")
            if detected_box is None:
                # self.current_detect_worker = None
                logger.warning("No detection found")
                QMessageBox.warning(self, "Detection Failed", "No detection found in the image. Please annotate the image manually.")

                self.measurement_button.setEnabled(True)
                self.static_label.drawing_enabled = True
                return
                
            original_box = self.get_original_box(detected_box)
        except Exception as e:
            logger.error(f"Error in process_with_obbeditor: {e}")
            self.measurement_button.setEnabled(True)
            self.static_label.drawing_enabled = True
            QMessageBox.warning(self, "Detection Error", f"Error processing detection: {str(e)}")
            return
            self.clear_rectangle_bttn.setEnabled(True)
            self.confirm_bttn.setEnabled(True)    
            self.current_detect_worker = None

        else:
            original_box = self.get_original_box(detected_box)
            self.detected_box = detected_box
            self.original_box = original_box
            self.statusBar.showMessage("Detection completed.")

            if original_box:
                # print(f"box on original image: {original_box}")
                logger.info(f"Detection successful, box on original image: {original_box}")
                self.update_annotation(original_box)

            
            self.current_detect_worker = None            
        
    def update_annotation(self, box):
        """" Convert the detected box to points and update the static label """
        if box is None:
            return
        
        center, size, angle = box
        img = self.static_label.cv_img
        if img is None:
            return

        img_height, img_width = img.shape[:2]
        label_width  = self.static_label.width()
        label_height = self.static_label.height()
        
        self.static_label.scale = min(label_width / img_width, label_height / img_height)
        
        self.static_label.display_width  = img_width * self.static_label.scale
        self.static_label.display_height = img_height * self.static_label.scale
        self.static_label.offset_x = (label_width - self.static_label.display_width) / 2
        self.static_label.offset_y = (label_height - self.static_label.display_height) / 2

        pts = cv2.boxPoints(((center[0], center[1]), (size[0], size[1]), angle))    
        self.static_label.original_points = [QPointF(x, y) for x, y in pts]
        rect = np.array([
            [int(x * self.static_label.scale + self.static_label.offset_x ), 
             int(y * self.static_label.scale + self.static_label.offset_y)] 
             for x, y in pts
            ])
        rect = MeasurementUtils.order_points(rect)
        self.static_label.rectangle_points = [QPointF(x, y) for x, y in rect]
        self.static_label.update()
        
    def get_original_box(self, detected_box):
        """Get the original box coordinates"""
        padding_percentage = 0.10
        original_img = cv2.imread(self._last_image_path)
        center_padded, size_padded, angle = detected_box
        h, w, _= original_img.shape
        h_padded, w_padded, _ = self.padded_image.shape
        pad_h = int(h * padding_percentage)
        pad_w = int(w * padding_percentage)

        orig_center_x = center_padded[0] - pad_w
        orig_center_y = center_padded[1] - pad_h
        orig_size_w   = size_padded[0]
        orig_size_h   = size_padded[1]
        orig_angle = angle

        orig_box = ((orig_center_x, orig_center_y), (orig_size_w, orig_size_h), orig_angle)
        return orig_box

    def confirm_annotation(self):
        """Handle confirmation of annotation"""
        rectangle = self.static_label.rectangle_points
        self.static_label.show_rectangle = False
        self.static_label.drawing_enabled = False  
        self.measurement_button.setEnabled(False)
        
        if not rectangle:
            QMessageBox.warning(self, "No Annotation", "Please annotate the image before confirming.")
            return
        
        try:
            self.statusBar.showMessage("Annotation confirmed")
            lens=self.current_lens.name
           
            rect_array = np.array([
                 [(point.x() - self.static_label.offset_x) / self.static_label.scale, 
                  (point.y() - self.static_label.offset_y) / self.static_label.scale]
                for point in rectangle
            ], dtype=np.float32)
            if rect_array.shape[0] < 4:
               QMessageBox.critical(self, "Annotation Error", "Invalid rectangle dimensions.")
               return
            box = cv2.minAreaRect(rect_array)
            self.drawn_box = box
            (center_x, center_y), (width, height), angle = box
            # print(f"center coordinates({center_x}, {center_y}), width: {width}, height: {height}, angle: {angle}")
            
            
            edge_lengths = []
            for i in range(len(rect_array)):
                p1 = rect_array[i]
                p2 = rect_array[(i + 1) % len(rect_array)]  
                edge_length = MeasurementUtils.euclidean_distance(p1, p2)
                edge_lengths.append(edge_length)

            width_px  = min(edge_lengths)
            height_px = max(edge_lengths)

            if width_px >= height_px:  
                height_px, width_px = width_px, height_px

            print(f'width_px:{width_px}, height_px:{height_px}')

            self.static_label.width_mm  = ImageUtils.pixels_to_mm(width_px, lens, self.current_scale)
            self.static_label.height_mm = ImageUtils.pixels_to_mm(height_px, lens, self.current_scale)

            #print(f"Width: {self.static_label.width_mm} mm, Height: {self.static_label.height_mm} mm")

            self.save_annotated_bttn.show()
            self.clear_rectangle_bttn.setEnabled(True)
             
            self.static_label.update()
            self.confirm_bttn.setEnabled(False)
            self.confirm_bttn.hide()

        except Exception as e:
            QMessageBox.critical(self, "Annotation Error", f"An error occurred during annotation confirmation: {str(e)}") 
    
    def draw_scale_bar(self, image_path):
        """Embed a calibrated, adaptive physical scale bar into an image."""
        self.current_scale = lens_scale_for_camera(self.current_lens, self.camera_type)
        self.current_lens.scale = self.current_scale
        try:
            spec = add_scale_bar(
                image_path,
                self.current_scale,
                jpeg_quality=constants.STACK_JPEG_QUALITY,
            )
        except ScaleBarError as exc:
            logger.warning("Scale bar was not added: %s", exc)
            QMessageBox.warning(
                self,
                "Scale bar not added",
                f"{exc}\n\nOpen Lens settings and set Scale (mm/pixel) for "
                "the active camera and lens.",
            )
            return False
        self.statusBar.showMessage(f"Added calibrated {spec.label} scale bar.", 8000)
        return True

    def clear_annotation(self):
        """Handle clearing of annotation"""
        self.static_label.clear_rectangle()
        self.detected_box = None

        if self.showing_static_image:
            self.static_label.drawing_enabled = True
            self.confirm_bttn.setEnabled(True)
            self.confirm_bttn.show()
            self.measurement_button.setEnabled(True)

        #self.clear_rectangle_bttn.setEnabled(False)
        self.save_annotated_bttn.hide()
    
    def save_annotated_image(self):
        """Save the annotated image"""
        if not self._last_image_path:
            QMessageBox.warning(self, "No Image", "No image to save.")
            return
        
        self.static_label.save_annotated_image(self._last_image_path)
        self.statusBar.showMessage("Annotated image saved")
        self.save_metadata_file(self._last_image_path)
        self.save_annotated_bttn.hide()
            
    def closeEvent(self, event):
        capture_thread = getattr(self, "_stack_capture_thread", None)
        active_stacker = getattr(self, "_active_stacker", None)
        autofocus_thread = getattr(self, "_autofocus_thread", None)
        lens_reposition_thread = getattr(self, "_lens_reposition_thread", None)
        single_capture_thread = getattr(self, "_single_capture_thread", None)
        manual_move_thread = getattr(self, "_manual_move_thread", None)
        capture_running = bool(capture_thread and capture_thread.is_alive())
        stacker_running = bool(active_stacker and active_stacker.is_alive())
        autofocus_running = bool(
            autofocus_thread and autofocus_thread.is_alive()
        )
        lens_reposition_running = bool(
            lens_reposition_thread and lens_reposition_thread.is_alive()
        )
        single_capture_running = bool(
            single_capture_thread and single_capture_thread.is_alive()
        )
        manual_move_running = bool(
            manual_move_thread and manual_move_thread.is_alive()
        )
        if (
            capture_running
            or stacker_running
            or autofocus_running
            or lens_reposition_running
            or single_capture_running
            or manual_move_running
        ):
            self.motor_stop()
            if not getattr(self, "_close_pending", False):
                self._close_pending = True
                QMessageBox.information(
                    self,
                    "Finishing current image operation",
                    "ENIMAS is safely stopping the current camera or motor "
                    "operation before closing. The window will close "
                    "automatically.",
                )
            self.statusBar.showMessage(
                "Waiting for the current camera/motor operation before closing..."
            )
            QTimer.singleShot(250, self.close)
            event.ignore()
            return

        logger.info("Window is closing.")
        print("Window is closing.")
        # Remain protected through final teardown. Worker completion signals
        # may already be queued even after their Python threads have exited.
        self._close_pending = True
        
        try:
            # Clean up BoxSegmenter resources
            try:
                from Tools.cropping_boxsegmenter import cleanup_resources
                cleanup_resources()
                logger.info("BoxSegmenter resources cleaned up")
            except Exception as e:
                logger.error(f"Error cleaning up BoxSegmenter resources: {e}")
            
            # Clean up detection pool
            global DETECT_POOL
            if DETECT_POOL is not None:
                try:
                    logger.info("Shutting down detection pool...")
                    safe_shutdown_pool(DETECT_POOL)
                    DETECT_POOL = None
                except Exception as e:
                    logger.error(f"Error shutting down detection pool: {e}")
            
            # Clean up workers
            if hasattr(self, 'detect_worker') and self.detect_worker is not None:
                try:
                    self.detect_worker.abort()
                    self.detect_worker = None
                except Exception as e:
                    logger.error(f"Error cleaning up detect worker: {e}")
            
            # Stop motors
            self.motor_stop()
            self.axis.__del__()
            
            # Clean up camera
            if hasattr(self, "arducam") and self.arducam is not None:
                self.arducam.stop()
                self.arducam.wait()
        except Exception as exception:
            logger.error(exception)
        
        # save the user inputs to constants.py file, then it will be saved in config.txt
        constants.STACK_SPEED = self.stack_speed_spinbox.value()
        constants.DEFAULT_NUM_OF_STACKS = self.number_stacks_spinbox.value()
        constants.DEFAULT_STACK_STEP_SIZE = self.step_size_spinbox.value()
        constants.DEFAULT_BASE_DIR = self.directory_manager.base_dir
        
        event.accept()

class Arducam(QThread):
    def __init__(self, camera: ArducamCamera, video_label, show_error):
        super().__init__()
        """ Thread that reads the camera input everytime possible
        https://github.com/ArduCAM/ArduCAM_USB_Camera_Shield_Python_Demo """
        
        self.show_error = show_error
        self.video_label = video_label

        self.current_img = None
        self._run_flag = True
        self.paused = False
        self.capture_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self.frame_counter = 0
        self.autofocus_warmup_discard_frames = 2
        self.nonfatal_camera_errors = False

        self.camera = camera
        if self.camera is not None:
            self.color_mode = self.camera.color_mode
            self.start()

        #self.start()  #if use this one, the user interface -->no response
    
    def get_reg_values(self):
        if self.camera is None:
            return
        return self.camera.read_reg_values()

    def read_image(self, timeout_ms=1500, notify_error=True):
        try:
            return self.camera.read(timeout=max(1, int(timeout_ms)))
        except RuntimeError:
            if notify_error and not getattr(self, "nonfatal_camera_errors", False):
                self.show_error.emit()
            raise
        

    def _take_image_unlocked(
        self,
        timeout_s=5.0,
        raise_errors=False,
        notify_error=True,
    ):
        self.last_capture_error = ""
        if self.camera is None:
            self.last_capture_error = "camera is unavailable"
            return None
        try:
            deadline = monotonic() + max(0.0, float(timeout_s))
            remaining = deadline - monotonic()
            if remaining <= 0:
                self.last_capture_error = "capture deadline expired before the driver read"
                return None
            ret, data, cfg = self.read_image(
                timeout_ms=remaining * 1000,
                notify_error=notify_error,
            )

            if data is None:
                self.last_capture_error = "camera driver returned no image data"
                return None

            while not ret:
                if monotonic() >= deadline:
                    self.last_capture_error = "camera connection timed out"
                    logger.error("Connection to camera failed")
                    if notify_error and not getattr(
                        self, "nonfatal_camera_errors", False
                    ):
                        self.show_error.emit()
                    return None
                remaining = deadline - monotonic()
                ret, data, cfg = self.read_image(
                    timeout_ms=remaining * 1000,
                    notify_error=notify_error,
                )

            return cv2.flip(convert_image_arducam(data, cfg, self.color_mode), 1)
        except Exception as exc:
            self.last_capture_error = f"{type(exc).__name__}: {exc}"
            logger.error(f"Error taking image with Arducam: {exc}")
            if raise_errors:
                raise
            return None

    def take_image(self):
        with self.capture_lock:
            return self._take_image_unlocked()

    def _capture_and_publish_preview(self):
        with self.capture_lock:
            image = self._take_image_unlocked()
            if image is not None and self._run_flag:
                self.update_image(image)
            return image

    def run(self):
        while self._run_flag:
            if self.paused:
                self.msleep(20)
                continue

            image = self._capture_and_publish_preview()
            if image is None and self._run_flag:
                self.msleep(100)

    def update_image(self, cv_img):
        """Updates the image_label with a new opencv image"""
        if isdeleted(self.video_label):
            return
        qt_img = self.convert_cv_qt(cv_img)
        #self.video_label.setPixmap(qt_img)
        QMetaObject.invokeMethod(
                self.video_label,
                "setPixmap",
                Qt.QueuedConnection,
                Q_ARG(QPixmap, qt_img)
            )
        with self.frame_lock:
            self.current_img = cv_img
            self.frame_counter += 1
    
    def convert_cv_qt(self, cv_img):
        """ Converts an opencv image to QPixmap
        https://github.com/docPhil99/opencvQtdemo/blob/master/liveLabel3.py """

        rgb_image = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb_image.shape
        bytes_per_line = ch * w
        convert_to_Qt_format = QImage(rgb_image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        width, height = self.video_label.width(), self.video_label.height()
        p = convert_to_Qt_format.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatio)
        return QPixmap.fromImage(p)

    def get_image(self):
        if self.current_img is None:
            # Try to get an image with retries
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    new_img = self.take_image()
                    if new_img is not None:
                        self.current_img = new_img
                        break
                    sleep(0.1)  # Short wait between attempts
                except Exception as e:
                    logger.error(f"Camera error (attempt {attempt+1}/{max_attempts}): {e}")
                    sleep(0.2)  # Slightly longer wait after an error
            
            # If we still don't have an image after all attempts
            if self.current_img is None:
                logger.error("Failed to get camera image after multiple attempts")
                # Don't show message box during autofocus - we'll handle it there
                if threading.current_thread() != threading.main_thread():
                    return None
                QMessageBox.critical(self.video_label, "Error", "Connection to camera failed.")
                return None
                 
        return self.current_img

    def begin_autofocus_capture(self):
        self.nonfatal_camera_errors = True
        self.paused = True
        sleep(0.05)

    def end_autofocus_capture(self):
        self.paused = False
        self.nonfatal_camera_errors = False

    def begin_stack_capture(self):
        self.nonfatal_camera_errors = True

    def end_stack_capture(self):
        self.nonfatal_camera_errors = False

    def begin_single_capture(self):
        self.nonfatal_camera_errors = True

    def end_single_capture(self):
        self.nonfatal_camera_errors = False

    def get_frame_counter(self):
        with self.frame_lock:
            return self.frame_counter

    def get_preview_fresh_image(self, start_counter, discard_frames=2, timeout_s=2.5, stop_check=None):
        def snapshot():
            with self.frame_lock:
                return self.frame_counter, self.current_img

        return wait_for_new_preview_frame(
            snapshot,
            start_counter,
            discard_frames=discard_frames,
            timeout_s=timeout_s,
            stop_check=stop_check,
        )

    def get_fresh_image(
        self,
        discard_frames=2,
        max_attempts=3,
        timeout_s=2.5,
        stop_check=None,
        after_counter=None,
    ):
        timeout_s = max(0.0, float(timeout_s))
        start_counter = (
            self.get_frame_counter() if after_counter is None else int(after_counter)
        )
        was_paused = bool(self.paused)
        direct_discard_frames = max(0, int(discard_frames))
        self.last_fresh_capture_error = ""
        self.last_fresh_capture_source = ""

        if not was_paused:
            preview_timeout = timeout_s
            img = self.get_preview_fresh_image(
                start_counter,
                discard_frames=discard_frames,
                timeout_s=preview_timeout,
                stop_check=stop_check,
            )
            if img is not None:
                self.last_fresh_capture_source = "preview"
                return img
            if stop_check is not None and stop_check():
                return None
            current_counter = self.get_frame_counter()
            direct_discard_frames = remaining_direct_discard_frames(
                start_counter,
                current_counter,
                discard_frames,
            )
            logger.warning(
                "Arducam preview freshness timed out (start=%s, current=%s, "
                "remaining direct discards=%s); "
                "pausing preview for direct capture",
                start_counter,
                current_counter,
                direct_discard_frames,
            )
        else:
            logger.debug("Arducam preview is paused; using direct fresh capture")

        def publish_direct_image(image):
            with self.frame_lock:
                self.current_img = image
                self.frame_counter += 1

        direct_result = capture_direct_with_paused_preview(
            self.capture_lock,
            lambda paused: setattr(self, "paused", True if paused else was_paused),
            lambda capture_timeout: self._take_image_unlocked(
                timeout_s=capture_timeout,
                raise_errors=True,
                notify_error=False,
            ),
            discard_frames=direct_discard_frames,
            max_attempts=max_attempts,
            timeout_s=timeout_s,
            stop_check=stop_check,
            last_error=lambda: getattr(self, "last_capture_error", ""),
            publish_image=publish_direct_image,
        )
        if direct_result.image is not None:
            self.last_fresh_capture_source = "direct-fallback"
            return direct_result.image
        if direct_result.reason == "cancelled":
            return None

        preview_status = (
            "preview was intentionally paused"
            if was_paused
            else f"preview counter advanced from {start_counter} to only "
            f"{self.get_frame_counter()}"
        )
        detail = f"; driver detail: {direct_result.detail}" if direct_result.detail else ""
        self.last_fresh_capture_error = (
            f"{preview_status}; direct capture failed with {direct_result.reason} "
            f"after {direct_result.read_count} driver reads and "
            f"{direct_result.lock_wait_s:.2f}s waiting for the camera lock{detail}"
        )
        logger.error("Arducam %s", self.last_fresh_capture_error)
        return None

    @staticmethod
    def calc_focus(image):
        return calc_focus_score(image)

    def white_balance(self):
        """Improved white balance using Gray World algorithm with multiple sampling regions.

        Improvements over previous version:
        - Multiple sampling regions instead of single center ROI
        - Dampening factor to prevent oscillation
        - Better convergence criteria
        - More robust averaging (excludes extreme values)
        - No recursive calls
        """
        # Start from neutral gains
        rr, rg, rb = 100, 100, 100
        self.camera.set_wb_gains_factor(rr, rg, rb)

        logger.info(f"White Balance Start: Gains=({rr}, {rg}, {rb})")
        sleep(.3)  # Allow camera to settle

        img = self.get_image()
        if img is None:
            logger.error("Failed to get initial image for white balance")
            return rr, rg, rb

        height, width = img.shape[:2]

        # Configuration
        max_iterations = 5  # Increased from 3
        min_gain = constants.WB_MIN  # 100
        max_gain = constants.WB_MAX  # 1600
        convergence_threshold = 0.05  # 5% change threshold
        dampening_factor = 0.6  # Apply only 60% of correction to prevent oscillation

        prev_gains = (rr, rg, rb)

        for iteration in range(max_iterations):
            # Sample multiple regions for more robust statistics
            # Use 5 regions: center, and 4 quadrants at 50% from center
            regions = []

            # Center region (30% of image)
            center_size = int(min(width, height) * 0.15)
            cy, cx = height // 2, width // 2
            regions.append(img[cy-center_size:cy+center_size, cx-center_size:cx+center_size])

            # Four quadrant regions (smaller, offset from center)
            quad_size = int(min(width, height) * 0.1)
            offset = int(min(width, height) * 0.25)
            regions.append(img[cy-offset-quad_size:cy-offset, cx-offset-quad_size:cx-offset])  # Top-left
            regions.append(img[cy-offset-quad_size:cy-offset, cx+offset:cx+offset+quad_size])  # Top-right
            regions.append(img[cy+offset:cy+offset+quad_size, cx-offset-quad_size:cx-offset])  # Bottom-left
            regions.append(img[cy+offset:cy+offset+quad_size, cx+offset:cx+offset+quad_size])  # Bottom-right

            # Calculate average RGB for each region
            region_avgs = []
            for region in regions:
                if region.size > 0:
                    b_avg, g_avg, r_avg = average(average(region, axis=0), axis=0)
                    # Only include region if it's not too dark (> 15) or too bright (< 240)
                    if min(r_avg, g_avg, b_avg) > 15 and max(r_avg, g_avg, b_avg) < 240:
                        region_avgs.append((r_avg, g_avg, b_avg))

            if len(region_avgs) == 0:
                logger.warning("No valid regions found for white balance")
                break

            # Compute median of region averages (more robust than mean)
            r_values = [rgb[0] for rgb in region_avgs]
            g_values = [rgb[1] for rgb in region_avgs]
            b_values = [rgb[2] for rgb in region_avgs]

            r = sorted(r_values)[len(r_values)//2]  # Median
            g = sorted(g_values)[len(g_values)//2]
            b = sorted(b_values)[len(b_values)//2]

            logger.debug(f"WB Iter {iteration+1}: RGB=({int(r)}, {int(g)}, {int(b)}) from {len(region_avgs)} regions")

            # Check if image is too dark
            if min(r, g, b) < 20:
                logger.warning(f"White Balance: Image too dark (R:{int(r)}, G:{int(g)}, B:{int(b)}). Check lighting.")
                break

            # Gray World algorithm: target is the average of all channels
            gray_target = (r + g + b) / 3.0

            # Calculate correction factors
            rf = gray_target / r if r > 0 else 1.0
            gf = gray_target / g if g > 0 else 1.0
            bf = gray_target / b if b > 0 else 1.0

            # Apply dampening to prevent oscillation
            rf = 1.0 + (rf - 1.0) * dampening_factor
            gf = 1.0 + (gf - 1.0) * dampening_factor
            bf = 1.0 + (bf - 1.0) * dampening_factor

            # Limit single-step correction to prevent wild swings
            max_single_correction = 1.5
            rf = max(1.0/max_single_correction, min(max_single_correction, rf))
            gf = max(1.0/max_single_correction, min(max_single_correction, gf))
            bf = max(1.0/max_single_correction, min(max_single_correction, bf))

            # Calculate new absolute gains
            new_rr = int(rr * rf)
            new_rg = int(rg * gf)
            new_rb = int(rb * bf)

            # Clamp to valid hardware range
            new_rr = max(min_gain, min(max_gain, new_rr))
            new_rg = max(min_gain, min(max_gain, new_rg))
            new_rb = max(min_gain, min(max_gain, new_rb))

            # Check for convergence (gains changed less than threshold)
            gain_change_r = abs(new_rr - rr) / float(rr)
            gain_change_g = abs(new_rg - rg) / float(rg)
            gain_change_b = abs(new_rb - rb) / float(rb)
            max_change = max(gain_change_r, gain_change_g, gain_change_b)

            logger.debug(f"WB Iter {iteration+1}: Gains ({rr},{rg},{rb}) -> ({new_rr},{new_rg},{new_rb}), change={max_change:.3f}")

            # Update gains
            rr, rg, rb = new_rr, new_rg, new_rb
            self.camera.set_wb_gains_factor(rr, rg, rb)

            # Check convergence
            if max_change < convergence_threshold:
                logger.info(f"White Balance converged after {iteration+1} iterations")
                break

            # Check if we hit limits
            if rr == max_gain or rg == max_gain or rb == max_gain:
                logger.warning(f"White Balance: Gain limit reached at iteration {iteration+1}")
                break

            # Wait for camera to settle before next iteration
            sleep(.3)

            # Get new image
            img = self.get_image()
            if img is None:
                logger.warning(f"Failed to get image at iteration {iteration+1}")
                break

        # Final verification
        img_final = self.get_image()
        if img_final is not None:
            center_size = int(min(width, height) * 0.15)
            cy, cx = height // 2, width // 2
            center_roi = img_final[cy-center_size:cy+center_size, cx-center_size:cx+center_size]
            b_final, g_final, r_final = average(average(center_roi, axis=0), axis=0)

            logger.info(f"White Balance Complete: RGB=({int(r_final)}, {int(g_final)}, {int(b_final)}), Gains=({rr}, {rg}, {rb})")

            # Check if image is too bright or too dark
            avg_brightness = (r_final + g_final + b_final) / 3.0

            if avg_brightness > 230:
                logger.info("Image is very bright after WB, consider reducing exposure")
            elif avg_brightness < 50:
                logger.warning("Image is very dark after WB, consider increasing exposure or lighting")
                answer = QMessageBox.information(
                    self.video_label,
                    "Image is dark",
                    "The image is quite dark after white balance. You may want to:\n"
                    "1. Increase lighting\n"
                    "2. Adjust exposure time\n"
                    "3. Adjust analog gain\n\n"
                    "The white balance gains have been set.",
                    QMessageBox.StandardButton.Ok
                )

        return rr, rg, rb
    
    def exposure_time(self):
        val_beginning = 4*1e4
        self.camera.set_analog_gain(1)
        self.camera.set_exposure_time(int(val_beginning))
        sleep(.2)
        img = self.take_image()
        
        if img is None:
            logger.error("Failed to get image for auto exposure")
            return int(val_beginning)
        
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        average_ = average(average(img, axis=0), axis=0)
        
        # Sanity check: avoid division by very small numbers
        if average_ < 5:
            logger.warning(f"Image too dark for auto exposure (avg={average_:.1f}). Using default.")
            return int(val_beginning)
        
        new_val = int(val_beginning * 150 / average_)
        
        # Clamp to valid exposure range
        new_val = max(constants.EXP_TIME_MIN, min(constants.EXP_TIME_MAX, new_val))
        
        logger.debug(f"Auto Exposure: average={average_:.1f}, new_exposure={new_val}")
        self.camera.set_exposure_time(new_val)
        return new_val
    
    def stop(self):
        self._run_flag = False
        self.camera.running_ = False
        self.wait()
        try:
            self.camera.stop()
            self.camera.closeCamera()
        except RuntimeError:
            pass

class VAImaging(QThread):
    def __init__(self, camera: VAImagingCamera, video_label, show_error):
        """ Thread that reads the camera input everytime possible"""
        super().__init__()

        self.show_error = show_error
        self.video_label = video_label

        self.current_img = None
        self._run_flag = True
        self.paused = False
        self.capture_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self.frame_counter = 0
        self.autofocus_warmup_discard_frames = 0
        self.nonfatal_camera_errors = False

        self.camera = camera
        if self.camera is not None:
             
            self.color_mode, pixel_format_str = self.camera.remote_device_feature.get_enum_feature("PixelFormat").get()
            self.start()

    def get_reg_values(self):
        if self.camera is None:
            return
        return self.camera.read_reg_values()
    
    def read_image(self, timeout_ms=500, notify_error=True):
        try:
            return self.camera.read(timeout=max(1, int(timeout_ms)))
        except RuntimeError:
            if notify_error and not getattr(self, "nonfatal_camera_errors", False):
                self.show_error.emit()
            raise

    def _take_image_unlocked(
        self,
        timeout_s=0.5,
        raise_errors=False,
        notify_error=True,
    ):
        self.last_capture_error = ""
        if self.camera is None:
            self.last_capture_error = "camera is unavailable"
            return None

        try:
            timeout_ms = max(1, int(max(0.0, float(timeout_s)) * 1000))
            success, raw_img = self.read_image(
                timeout_ms=timeout_ms,
                notify_error=notify_error,
            )
            if raw_img is None:
                self.last_capture_error = "camera driver returned no image data"
                return None

            return cv2.flip(convert_image(raw_img, self.color_mode), 1)
        except Exception as exc:
            self.last_capture_error = f"{type(exc).__name__}: {exc}"
            logger.error(f"Error taking image with VAImaging camera: {exc}")
            if raise_errors:
                raise
            return None

    def take_image(self):
        with self.capture_lock:
            return self._take_image_unlocked()

    def _capture_and_publish_preview(self):
        with self.capture_lock:
            image = self._take_image_unlocked()
            if image is not None and self._run_flag:
                self.update_image(image)
            return image

    def run(self):
        while self._run_flag:
            if self.paused:
                self.msleep(20)
                continue
            try:
                image = self._capture_and_publish_preview()
                if image is None and self._run_flag:
                    self.msleep(100)
            except Exception as e:
                print("[CameraThread] get_image error:", e)
                break

    def update_image(self, cv_img):
        if isdeleted(self.video_label):
            return
        qt_img = self.convert_cv_qt(cv_img)
        #self.video_label.setPixmap(qt_img)
        # To avoid ui crash 
        QMetaObject.invokeMethod(
                self.video_label,
                "setPixmap",
                Qt.QueuedConnection,
                Q_ARG(QPixmap, qt_img)
            )
        with self.frame_lock:
            self.current_img = cv_img
            self.frame_counter += 1

    def convert_cv_qt(self, cv_img):
        """ Converts an opencv image to QPixmap
        https://github.com/docPhil99/opencvQtdemo/blob/master/liveLabel3.py """
        
        h, w, ch = cv_img.shape
        bytes_per_line = ch * w
        convert_to_Qt_format = QImage(cv_img, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        width, height = self.video_label.width(), self.video_label.height()
        p = convert_to_Qt_format.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatio)
        return QPixmap.fromImage(p)

    def get_image(self):
        if self.current_img is None:
            # Try to get an image with retries
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    new_img = self.take_image()
                    if new_img is not None:
                        self.current_img = new_img
                        break
                    sleep(0.1)  # Short wait between attempts
                except Exception as e:
                    logger.error(f"Camera error (attempt {attempt+1}/{max_attempts}): {e}")
                    sleep(0.2)  # Slightly longer wait after an error
            
            # If we still don't have an image after all attempts
            if self.current_img is None:
                logger.error("Failed to get camera image after multiple attempts")
                # Only show error message if not in autofocus thread
                if threading.current_thread() is threading.main_thread():
                    QMessageBox.critical(self.video_label, "Error", "Connection to camera failed.")
                return None
        return self.current_img

    def begin_autofocus_capture(self):
        self.nonfatal_camera_errors = True
        self.paused = False

    def end_autofocus_capture(self):
        self.paused = False
        self.nonfatal_camera_errors = False

    def begin_stack_capture(self):
        self.nonfatal_camera_errors = True

    def end_stack_capture(self):
        self.nonfatal_camera_errors = False

    def begin_single_capture(self):
        self.nonfatal_camera_errors = True

    def end_single_capture(self):
        self.nonfatal_camera_errors = False

    def get_frame_counter(self):
        with self.frame_lock:
            return self.frame_counter

    def get_preview_fresh_image(self, start_counter, discard_frames=0, timeout_s=6.0, stop_check=None):
        def snapshot():
            with self.frame_lock:
                return self.frame_counter, self.current_img

        return wait_for_new_preview_frame(
            snapshot,
            start_counter,
            discard_frames=discard_frames,
            timeout_s=timeout_s,
            stop_check=stop_check,
        )

    def get_fresh_image(
        self,
        discard_frames=2,
        max_attempts=3,
        timeout_s=6.0,
        stop_check=None,
        after_counter=None,
    ):
        timeout_s = max(0.0, float(timeout_s))
        start_counter = (
            self.get_frame_counter() if after_counter is None else int(after_counter)
        )
        was_paused = bool(self.paused)
        direct_discard_frames = max(0, int(discard_frames))
        self.last_fresh_capture_error = ""
        self.last_fresh_capture_source = ""

        if not was_paused:
            preview_timeout = timeout_s
            img = self.get_preview_fresh_image(
                start_counter,
                discard_frames=discard_frames,
                timeout_s=preview_timeout,
                stop_check=stop_check,
            )
            if img is not None:
                self.last_fresh_capture_source = "preview"
                return img
            if stop_check is not None and stop_check():
                return None
            current_counter = self.get_frame_counter()
            direct_discard_frames = remaining_direct_discard_frames(
                start_counter,
                current_counter,
                discard_frames,
            )
            logger.warning(
                "VAImaging preview freshness timed out (start=%s, current=%s, "
                "remaining direct discards=%s); "
                "pausing preview for direct capture",
                start_counter,
                current_counter,
                direct_discard_frames,
            )
        else:
            logger.debug("VAImaging preview is paused; using direct fresh capture")

        def publish_direct_image(image):
            with self.frame_lock:
                self.current_img = image
                self.frame_counter += 1

        direct_result = capture_direct_with_paused_preview(
            self.capture_lock,
            lambda paused: setattr(self, "paused", True if paused else was_paused),
            lambda capture_timeout: self._take_image_unlocked(
                timeout_s=capture_timeout,
                raise_errors=True,
                notify_error=False,
            ),
            discard_frames=direct_discard_frames,
            max_attempts=max_attempts,
            timeout_s=timeout_s,
            stop_check=stop_check,
            last_error=lambda: getattr(self, "last_capture_error", ""),
            publish_image=publish_direct_image,
        )
        if direct_result.image is not None:
            self.last_fresh_capture_source = "direct-fallback"
            return direct_result.image
        if direct_result.reason == "cancelled":
            return None

        preview_status = (
            "preview was intentionally paused"
            if was_paused
            else f"preview counter advanced from {start_counter} to only "
            f"{self.get_frame_counter()}"
        )
        detail = f"; driver detail: {direct_result.detail}" if direct_result.detail else ""
        self.last_fresh_capture_error = (
            f"{preview_status}; direct capture failed with {direct_result.reason} "
            f"after {direct_result.read_count} driver reads and "
            f"{direct_result.lock_wait_s:.2f}s waiting for the camera lock{detail}"
        )
        logger.error("VAImaging %s", self.last_fresh_capture_error)
        return None

    @staticmethod
    def calc_focus(image):
        return calc_focus_score(image)

    def exposure_time(self):
        """Sets the exposure time automatically that is suitable for the current lighting conditions."""
        self.camera.cam.data_stream[0].flush_queue()
        self.camera.remote_device_feature.get_enum_feature("ExposureAuto").set(gx.GxAutoEntry.ONCE)
        sleep(2)
        exposuretime = self.camera.remote_device_feature.get_float_feature("ExposureTime").get()
        # keep the value 
        #self.camera.set_exposure_time(exposuretime)
        print("Auto exposuretime:", exposuretime)
        return int(exposuretime) #[0]

    def white_balance(self):
        """Sets the white balance automatically that is suitable for the current lighting conditions."""
        self.camera.remote_device_feature.get_enum_feature("BalanceWhiteAuto").set(gx.GxAutoEntry.ONCE)
        sleep(1)
        ratios = {}
        for ch in ("Red", "Green", "Blue"):
            self.camera.remote_device_feature.get_enum_feature("BalanceRatioSelector").set(ch)
            ratios[ch] = self.camera.remote_device_feature.get_float_feature("BalanceRatio").get()
            print(f"WB-{ch} ratio:", ratios[ch])
        r, g, b =  100*ratios["Red"], 100*ratios["Green"], 100*ratios["Blue"]
        self.camera.set_wb_gains_factor(r, g, b)
        return r,g,b


    def gamma_enable(self, enable: bool):
        gammaEnable_feature = self.camera.remote_device_feature.get_bool_feature("GammaEnable")
        gammaEnable_feature.set(enable)         

    def auto_gain(self):
        self.camera.remote_device_feature.get_enum_feature("GainAuto").set(gx.GxAutoEntry.ONCE)
        sleep(1)
        gain = self.camera.remote_device_feature.get_float_feature("Gain").get()  
        #self.camera.set_analog_gain(gain)     
        return gain                                                             


    def stop(self):
        self._run_flag = False
        self.camera.running_ = False
        self.wait()
        
        try:
            if self.camera is not None:
                self.camera.stop()
                self.camera.closeCamera()
        except RuntimeError:
            pass

class DirectoryManager:
    def __init__(self, dir_widgets, statusBar):
        self.explorer, self.browse_dir_bttn, self.add_specimen_bttn, self.prefix_entry, self.plate_id_entry, self.code_number_entry, self.tab_widget, self.single_specimen_entry, self.single_folder_entry, self.browse_single_dir_bttn, self.sample_id_entry = dir_widgets
        self.statusBar: QStatusBar = statusBar
        self.tab_widget: QTabWidget
        
        self.explorer.clicked.connect(self.explorer_clicked)
        self.browse_dir_bttn.clicked.connect(self.browse_dir_clicked)
        self.browse_single_dir_bttn.clicked.connect(self.browse_single_dir_clicked)
        self.add_specimen_bttn.clicked.connect(self.add_specimen)
        self.tab_widget.currentChanged.connect(self.new_tab_selected)
        
        self.current_dir_mode: int = self.tab_widget.currentIndex()
        self.base_dir = constants.DEFAULT_BASE_DIR
        self.init_folder_names()

        self.model = QFileSystemModel()
        self.model.setRootPath(self.base_dir)
        self.explorer.setModel(self.model)
        self.explorer.setRootIndex(self.model.index(self.base_dir))
        self.explorer.setColumnWidth(0, 920)

    def init_folder_names(self) -> None:
        """Inserts latest folder names or creates new ones"""
        
        if not os.path.isdir(self.base_dir):
            os.makedirs(self.base_dir)
        
        # Structure Tab
        prefix_init, plate_init, code_init = "", "", 0

        if prefixes := [dir for basename in os.listdir(self.base_dir) if os.path.isdir(dir := os.path.join(self.base_dir, basename)) and not "_Stack_Frames_" in basename]:
            latest = max(prefixes, key=os.path.getctime)
            prefix_init = os.path.basename(latest)

            if plates := [dir for basename in os.listdir(latest) if os.path.isdir(dir := os.path.join(latest, basename)) and not "_Stack_Frames_" in basename]:
                latest = max(plates, key=os.path.getctime)
                plate_init = os.path.basename(latest)

                if codes := [dir for basename in os.listdir(latest) if basename.isdigit() and os.path.isdir(dir := os.path.join(latest, basename)) and not "_Stack_Frames_" in basename]:
                    latest = max(codes, key=os.path.getctime)
                    code_init = os.path.basename(latest)

        self.prefix_entry.setText(prefix_init)
        self.plate_id_entry.setText(plate_init)
        try:
            self.code_number_entry.setValue(int(code_init))
        except ValueError:
            logger.error("Code number was not an int while inserting for init.")
        
        
        # Single Tab
        self.single_folder_entry.setText(self.base_dir)
        

    def new_tab_selected(self):
        """Slot called after Tab changed"""
        self.current_dir_mode = self.tab_widget.currentIndex()
        
        if self.current_dir_mode:
            if os.path.isdir(self.single_folder_entry.text()):
                self.explorer.setRootIndex(self.model.index(self.single_folder_entry.text()))
        else:
            self.explorer.setRootIndex(self.model.index(self.base_dir))

    def browse_dir_clicked(self):
        """Open File dialog to select a new base directory"""
        dialog = QFileDialog()
        selected_dir = dialog.getExistingDirectory(self.explorer, "Select a base directory to save the folder structure in.", self.base_dir)
        self.base_dir = selected_dir
        self.explorer.setRootIndex(self.model.index(self.base_dir))

    def browse_single_dir_clicked(self):
        """Open File dialog to select a new single folder for the single image file"""
        dialog = QFileDialog()
        selected_dir = dialog.getExistingDirectory(self.explorer, "Select a folder to save individual images.", self.base_dir)
        self.single_folder_entry.setText(selected_dir)
        self.explorer.setRootIndex(self.model.index(selected_dir))

    def explorer_clicked(self, clicked_folder):
        """Shows the folder name clicked in the explorer to save next imgs in or opens the clicked image in the standart image viewing app"""
        path = self.model.filePath(clicked_folder)
        self.statusBar.clearMessage()
        
        if os.path.isfile(path) and Path(path).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
            # Image clicked, open it 
            Image.open(path).show()
            return
        
        if not os.path.isdir(path):
            self.statusBar.showMessage("Clicked file is neither a directory nor an image")
            return
        
        if self.current_dir_mode == 1:
            # Nothing happend when in single image saving mode
            return
        
        new_prefix, new_plate, new_code = None, None, None

        level = len(path.split("/")) - len(os.path.abspath(self.base_dir).split("\\"))

        if level == 1:
            # a prefix folder was clicked
            new_prefix = os.path.basename(path)
            if plates := [plate_dir for plate_basename in os.listdir(path) if os.path.isdir(plate_dir := os.path.join(path, plate_basename))]:
                path = max(plates, key=os.path.getctime)
                level = 2
        
        if level == 2:
            # a plate_id folder was clicked
            new_prefix = os.path.basename(os.path.dirname(path))
            new_plate = os.path.basename(path)
            if codes := [code_dir for code_basename in os.listdir(path) if code_basename.isdigit() and os.path.isdir(code_dir := os.path.join(path, code_basename))]:
                path = max(codes, key=os.path.getctime)
                level = 3

        if level == 3:
            # a code_number folder was clicked
            new_prefix = os.path.basename(os.path.dirname(os.path.dirname(path)))
            new_plate = os.path.basename(os.path.dirname(path))
            if not (new_code := os.path.basename(path)).isdigit(): 
                new_code = 0
        
        if level > 3:
            self.statusBar.showMessage("Clicked folder is not a Prefix folder, Plate ID folder or a Code number folder.")

        if new_code: self.code_number_entry.setValue(int(new_code))
        if new_plate: self.plate_id_entry.setText(new_plate)
        if new_prefix: self.prefix_entry.setText(new_prefix)

    def add_specimen(self):
        """Adds 1 to the specimen code number (should be an integer)"""
        code_number = self.code_number_entry.value()
        self.code_number_entry.setValue(code_number + 1)
    
    def get_current_folder_name(self, create_dir=True) -> str:
        """Checks the user input and created these files if they dont exist. Returns an existing folder and the name to save an image in"""
        
        if self.current_dir_mode == 1:
            # single file mode
            folder_name = self.single_folder_entry.text()
            if not os.path.isabs(folder_name): 
                folder_name = os.path.join(self.base_dir, folder_name)
            
            specimen_name = self.single_specimen_entry.text()
            if not (specimen_name and folder_name):
                return None, None
            return folder_name, specimen_name
        
        prefix = self.prefix_entry.text()
        plate_id = self.plate_id_entry.text()
        code_number = self.code_number_entry.value()
        if not all([prefix, plate_id, code_number]):
            return None, None

        code_number = f"{code_number:0>7}"
        specimen_name = f"{plate_id}_{prefix}{code_number}"
        path_to_specimen_folder = os.path.join(self.base_dir, prefix, plate_id, f"{prefix}{code_number}")
        
        if not os.path.isdir(path_to_specimen_folder) and create_dir:
            try:
                os.makedirs(path_to_specimen_folder)
            except Exception as exception:
                QMessageBox.critical(self, "Saving images failed", str(exception) + "\nSaving images in desired folder failed. Images will be saved in the default Images folder")
                self.base_dir = constants.DEFAULT_BASE_DIR
                self.explorer.setRootIndex(self.model.index(self.base_dir))
                return self.get_current_folder_name()

        return os.path.normpath(path_to_specimen_folder), specimen_name
    
    def add_image(self, create_dir=True, get_dir=False) -> str:
        """Returns a path to a new image file in the given specimen directory"""
        img_path, specimen_name = self.get_current_folder_name(create_dir)
        if img_path is None:
            return None if not get_dir else (None, None)

        # image files
        files = [file for file in os.listdir(img_path) if os.path.isfile(os.path.join(img_path, file)) and file.startswith(specimen_name)] if os.path.isdir(img_path) else []
        if files:
            # get the latest img file and create a new one 
            all_numbers = [int(match.group(1)) for name in files if (match := search(f"^{specimen_name}_(\d+).{constants.IMG_EXTENSION}$", name))]
            highest = max(all_numbers) if all_numbers else 0
            new_img_name = f"{specimen_name}_{highest+1:0>3}.{constants.IMG_EXTENSION}"
        else:
            # it's the first img file in this directory
            new_img_name = f"{specimen_name}_001.{constants.IMG_EXTENSION}" if self.current_dir_mode == 0 else f"{specimen_name}.{constants.IMG_EXTENSION}"
            
        if get_dir:
            return os.path.join(img_path, new_img_name), img_path 
        else:
            return os.path.join(img_path, new_img_name)

    def add_stack_image(self, number_of_stacks, create_dir=True) -> Tuple[List[str], str, str] | None:
        """Generates paths for stacked images and a directory to save them.

        Args:
            number_of_stacks (int): The number of stacked images to generate.
            create_dir (bool, optional): Whether to create a new directory for the stacked images. Defaults to True.

        Returns:
            tuple: A tuple containing the full paths of the stacked images, the path to the new directory, and the full path of the stacked image.

        Example:
            >>> add_stack_image(3)
            (['/base/new_dir/image1.jpg', '/base/new_dir/image2.jpg', '/base/new_dir/image3.jpg'], '/base/new_dir', '/base/stacked_image.jpg')
        """
        img_path, specimen_name = self.get_current_folder_name(create_dir)
        if img_path is None:
            return None
        
        # Use the union of raw folders and surviving final outputs. This
        # prevents overwrites when a host archives or deletes raw frame folders.
        indices = []
        if os.path.isdir(img_path):
            specimen_pattern = escape(specimen_name)
            for entry in os.listdir(img_path):
                entry_path = os.path.join(img_path, entry)
                if os.path.isdir(entry_path):
                    match = search(
                        rf"^{specimen_pattern}_Stack_Frames_(\d+)$",
                        entry,
                    )
                else:
                    match = search(
                        rf"(?i)^{specimen_pattern}_(?:stacked|nostack)_(\d+)\."
                        r"(?:tiff?|jpe?g|png|bmp)$",
                        entry,
                    )
                if match:
                    indices.append(int(match.group(1)))
        next_index = max(indices, default=0) + 1
        new_dir = f"{specimen_name}_Stack_Frames_{next_index:0>2}"
        new_dir_path = os.path.join(img_path, new_dir)
        if create_dir:
            os.makedirs(new_dir_path)

        img_paths = [
            os.path.join(
                new_dir_path,
                f"{specimen_name}_Frame_{next_index:0>2}_{i+1:0>2}."
                f"{constants.IMG_EXTENSION}",
            )
            for i in range(number_of_stacks)
        ]
        final_extension = normalize_image_extension(constants.STACK_OUTPUT_EXTENSION)
        stacked_path = os.path.join(
            img_path,
            f"{specimen_name}_stacked_{next_index:0>2}.{final_extension}",
        )
        if os.path.exists(stacked_path):
            raise FileExistsError(
                f"Refusing to overwrite existing final stack: {stacked_path}"
            )
        return img_paths, new_dir_path, stacked_path

class LensDialog(QDialog):
    def __init__(self, main: MainWindow):
        super().__init__(main)

        uic.loadUi("UserInterface/dialog.ui", self)
        self.setWindowIcon(QIcon(r"UserInterface\imgs\enimas_icon.png"))
        
        self.main_window = main
        # Work on an isolated copy. Cancel/close discards every edit; OK commits
        # the complete list and selected calibration atomically to MainWindow.
        self.all_lenses = deepcopy(main.all_lenses)

        self.lens_list_widget: QListWidget = self.findChild(QListWidget, "lens_list")
        self.move_up_bttn = self.findChild(QPushButton, "move_up_bttn")
        
        self.name_edit = self.findChild(QLineEdit, "name_edit")
        self.lenght_edit = self.findChild(QLineEdit, "lenght_edit")
        self.distance_edit = self.findChild(QLineEdit, "dist_edit")
        self.magnification_factor_edit = self.findChild(QLineEdit, "magnificationLineEdit")
        self.scale_label = self.findChild(QLabel, "ScaleLabel")
        self.scale_edit = self.findChild(QLineEdit, "ScaleLineEdit")
        self.scale_label.setText("Scale (mm/pixel)")
        self.scale_edit.setPlaceholderText("e.g. 0.00357567")
        self.scale_edit.setToolTip(
            "Physical millimetres represented by one image pixel for this exact camera and lens."
        )
        self.scale_edit.setEnabled(False)
        self.calculate_scale_bttn = QPushButton("Calculate...")
        self.calculate_scale_bttn.setToolTip(
            "Calculate mm/pixel from a photographed calibration ruler or stage micrometer."
        )
        self.calculate_scale_bttn.setEnabled(False)
        self.horizontalLayout_5.addWidget(self.calculate_scale_bttn)
        self.new_bttn = self.findChild(QPushButton, "new_bttn")
        
        self.delete_bttn = self.findChild(QPushButton, "delete_bttn")
        self.status_bar: QStatusBar = self.findChild(QStatusBar, "status_bar")
        self.ok_bttn = self.findChild(QPushButton, "ok_bttn")
        self.cancel_bttn = self.findChild(QPushButton, "cancel_bttn")

        self.delete_bttn.setEnabled(False)

        self.move_up_bttn.clicked.connect(self.move_up_clicked)
        self.new_bttn.clicked.connect(self.new_clicked)
        self.ok_bttn.clicked.connect(self.ok_clicked)
        self.cancel_bttn.clicked.connect(self.close)
        self.lens_list_widget.itemClicked.connect(self.item_clicked)
        self.delete_bttn.clicked.connect(self.delete_item)

        self.lens_list_widget.currentItemChanged.connect(self.item_changed)
        self.scale_edit.returnPressed.connect(self.sclae_changed)
        self.calculate_scale_bttn.clicked.connect(self.calculate_scale_clicked)

        for lens in self.all_lenses:
            # insert all the saved lenses into the QListWidget
            item = QListWidgetItem(lens.label_str)
            self.lens_list_widget.addItem(item)
            if lens == self.main_window.current_lens:
                self.lens_list_widget.setCurrentItem(item)
    
    def move_up_clicked(self):
        self.main_window.move_motor_to(10)
    
    def item_clicked(self, item):
        self.delete_bttn.setEnabled(True)

    def item_changed(self, current, previous):
        current_lens = self.lens_list_widget.currentItem()
        if not current_lens:
            self.scale_edit.setText("")
            self.scale_edit.setEnabled(False)
            self.calculate_scale_bttn.setEnabled(False)
            return
        self.status_bar.showMessage(
            "Enter Scale (mm/pixel) and press Enter, or use Calculate from a calibration target."
        )
        for lens in self.all_lenses:
            if lens.label_str == current_lens.text():
                if self.main_window.camera_type == "VAImagingCamera":
                    value = lens.scale_vaimaging
                elif self.main_window.camera_type == "ArducamCamera":
                    value = lens.scale_arducam
                else:
                    value = None
                self.scale_edit.setText(f"{value:g}" if is_valid_scale(value) else "")
        self.scale_edit.setEnabled(True)
        self.calculate_scale_bttn.setEnabled(True)

    def sclae_changed(self):
        current_lens = self.lens_list_widget.currentItem()
        if not current_lens:
            return
        for lens in self.all_lenses:
            if lens.label_str == current_lens.text():
                try:
                    new_scale = float(self.scale_edit.text())
                    if not is_valid_scale(new_scale):
                        raise ValueError
                except (TypeError, ValueError):
                    self.status_bar.showMessage(f"Invalid scale value: '{self.scale_edit.text()}'")
                    if self.main_window.camera_type == "VAImagingCamera":
                        old_value = lens.scale_vaimaging
                    elif self.main_window.camera_type == "ArducamCamera":
                        old_value = lens.scale_arducam
                    else:
                        old_value = None
                    self.scale_edit.setText(
                        f"{old_value:g}" if is_valid_scale(old_value) else ""
                    )
                    return
                
                if self.main_window.camera_type == "VAImagingCamera":
                    lens.scale_vaimaging = new_scale
                    scale_arducam = new_scale * 0.00157727 / 0.00178784
                    lens.scale_arducam = round(scale_arducam, 8)  # avoid too many digits
                elif self.main_window.camera_type == "ArducamCamera":
                    lens.scale_arducam = new_scale
                    scale_vaimaging = new_scale * 0.00178784 / 0.00157727
                    lens.scale_vaimaging = round(scale_vaimaging, 8)  # avoid too many digits
                print(f"Set new scale for lens {lens.name}: {new_scale}")
                break
    
    def calculate_scale_clicked(self):
        """Calculate mm/pixel from a known physical span and its pixel span."""
        if not self.lens_list_widget.currentItem():
            return
        known_mm, ok = QInputDialog.getDouble(
            self,
            "Scale calibration",
            "Known physical length on the calibration target (mm):",
            1.0,
            0.000001,
            1000000.0,
            6,
        )
        if not ok:
            return
        measured_pixels, ok = QInputDialog.getDouble(
            self,
            "Scale calibration",
            "Pixel distance covering that known length:",
            1000.0,
            0.01,
            1000000000.0,
            2,
        )
        if not ok:
            return
        try:
            calculated = calculate_mm_per_pixel(known_mm, measured_pixels)
        except ScaleBarError as exc:
            QMessageBox.warning(self, "Invalid calibration", str(exc))
            return
        self.scale_edit.setText(f"{calculated:.10g}")
        self.sclae_changed()
        self.status_bar.showMessage(
            f"Scale set to {calculated:.10g} mm/pixel for the selected camera and lens."
        )

    def delete_item(self):
        lens_to_delete = self.lens_list_widget.currentItem()

        for lens in self.all_lenses:
            if lens.label_str == lens_to_delete.text():
                logger.info(f"delete item {lens.label_str}")
                self.all_lenses.remove(lens)

        row = self.lens_list_widget.row(lens_to_delete)
        self.lens_list_widget.takeItem(row)
        del lens_to_delete

        if not self.all_lenses:
            # all lenses habe been delete. Creating a default one
            default_lens = Lens("Default Lens", 150, self.main_window.default_cam_values)
            self.all_lenses.append(default_lens)

            item = QListWidgetItem(default_lens.label_str)
            self.lens_list_widget.addItem(item)
            self.lens_list_widget.setCurrentItem(item)

    def new_clicked(self):
        """A new Lens will be added"""
        self.status_bar.clearMessage()
        name = self.name_edit.text()
        lenght: str = self.lenght_edit.text()
        distance: str = self.distance_edit.text()
        magnification_factor: str = self.magnification_factor_edit.text()
        
        if not (name and lenght):
            self.status_bar.showMessage("Please enter a name and a lenght for the new lens.")
            return
        if lenght.endswith("mm"):
            lenght = lenght[:-2]
        if not lenght.isdigit() or not (0 < int(lenght) <= 360):
            self.status_bar.showMessage(f"Invalid lenght for new lens: '{lenght}'")
            return
        if distance and (not distance.isdigit() or not (0 < int(distance) <= 360)):
            self.status_bar.showMessage(f"Invalid object distance for new lens: '{distance}'")
            return
        
        if not magnification_factor:
            #self.status_bar.showMessage("Please enter a magnification factor for the new lens.")
            factor = None
            scale_vai = None
            scale_arducam = None
            self.status_bar.showMessage("Please set correct scale manually after creating the new lens.")
        else:
            # calculate the scale from the magnification factor
            try:
                factor = float(magnification_factor)
            except(TypeError, ValueError):
                self.status_bar.showMessage(f"Invalid magnification factor for new lens: '{magnification_factor}'")
                return
            if not (0 < factor <= 10):
                self.status_bar.showMessage(f"Magnification factor must be >0 and <=10: '{magnification_factor}'")
                return
            scale_vai = 0.00178784 / factor
            scale_vai = round(scale_vai, 8)  
            scale_arducam = 0.00157727 / factor
            scale_arducam = round(scale_arducam, 8) 
            

        if name in [lens.name for lens in self.all_lenses]:
            self.status_bar.showMessage(f"Lens named {name} already exists")
            return

        # get the current cam setting from the previous lens (or the default ones)
        current_setting = tuple(int(x) for x in self.main_window.default_cam_values)
        new_lens = Lens(
            name, int(lenght), current_setting,
            float(distance) if distance else None,
            None, None, None,
            factor,  # magnification factor
            None,  # scale
            scale_vai,  # scale_vaimaging
            scale_arducam  # scale_arducam
        )
        
        self.all_lenses.append(new_lens)

        item = QListWidgetItem(new_lens.label_str)
        self.lens_list_widget.addItem(item)
        self.lens_list_widget.setCurrentItem(item)

    def ok_clicked(self):
        """The selected lens will be the current lens."""
        selected_lens = self.lens_list_widget.currentItem()
        if not selected_lens:
            self.status_bar.showMessage("No lens selected.")
            return
        
        for lens in self.all_lenses:
            if lens.label_str == selected_lens.text():
                if lens.scale_vaimaging is None or lens.scale_arducam is None:
                    self.status_bar.showMessage("❗Please set correct scale for the selected lens.")
                    return

        selected_lens = [lens for lens in self.all_lenses if lens.label_str == selected_lens.text()][0]
        self.close()
        self.main_window.update_lens_list(self.all_lenses)
        self.main_window.set_current_lens(selected_lens)

    def closeEvent(self, event):
        # Cancel/close must remain possible. Any uncalibrated lens is safe: scale
        # bars and physical measurements are disabled until a valid scale is set.
        event.accept()
        super().closeEvent(event)


class SettingsDialog(QDialog):
    def __init__(self, main: MainWindow, value_list, change_methods):
        super().__init__(main)

        self.main = main
        self.values_beginning = value_list
        self.change_methods = change_methods

        if self.main.camera_type == "VAImagingCamera":
            uic.loadUi("UserInterface/VAImagingcamera_settings.ui", self)
            self.setWindowIcon(QIcon(r"UserInterface\imgs\enimas_icon.png"))
            self.gamma_enable_check = self.findChild(QCheckBox, "GammaEnablecheckBox")
            self.auto_gain_bttn = self.findChild(QPushButton, "GainAutoButton")
            self.auto_gain_bttn.clicked.connect(self.auto_gain)
            self.gamma_enable_check.toggled.connect(
                lambda checked: self.change_methods["gamma_enable"](checked)
            )

        else:
            uic.loadUi("UserInterface/camera_settings.ui", self)
            self.setWindowIcon(QIcon(r"UserInterface\imgs\enimas_icon.png"))



        self.sliders = [self.findChild(QSlider, "horizontalSlider"),
                        self.findChild(QSlider, "horizontalSlider_2"),
                        self.findChild(QSlider, "horizontalSlider_3"),
                        self.findChild(QSlider, "horizontalSlider_4"),
                        self.findChild(QSlider, "horizontalSlider_5")]

        self.entries = [self.findChild(QSpinBox, "spinBox"),
                        self.findChild(QSpinBox, "spinBox_2"),
                        self.findChild(QSpinBox, "spinBox_3"),
                        self.findChild(QSpinBox, "spinBox_4"),
                        self.findChild(QSpinBox, "spinBox_5")]

        self.ok_bttn = self.findChild(QPushButton, "ok_bttn")
        self.cancel_bttn = self.findChild(QPushButton, "cancel_bttn")
        self.auto_wb_bttn = self.findChild(QPushButton, "auto_wb_bttn")
        self.auto_exp_bttn = self.findChild(QPushButton, "auto_exp_bttn")
        
        self.ok_bttn.clicked.connect(self.save_values)
        self.cancel_bttn.clicked.connect(self.close)
        self.auto_exp_bttn.clicked.connect(self.auto_exp)
        self.auto_wb_bttn.clicked.connect(self.auto_wb)

        if self.main.camera_type == "VAImagingCamera":
            self.exp_time_setting = Setting(self.sliders[0], self.entries[0], constants.VAI_EXP_TIME_MIN, constants.VAI_EXP_TIME_MAX, self.change_methods["exp"], self.values_beginning[0])
            self.gain_setting = Setting(self.sliders[1], self.entries[1], constants.VAI_GAIN_MIN, constants.VAI_GAIN_MAX, self.change_methods["gain"], self.values_beginning[1])
            self.wb_R_setting = Setting(self.sliders[2], self.entries[2], constants.VAI_WB_MIN, constants.VAI_WB_MAX, self.change_wb, self.values_beginning[2])
            self.wb_G_setting = Setting(self.sliders[3], self.entries[3], constants.VAI_WB_MIN, constants.VAI_WB_MAX, self.change_wb, self.values_beginning[3])
            self.wb_B_setting = Setting(self.sliders[4], self.entries[4], constants.VAI_WB_MIN, constants.VAI_WB_MAX, self.change_wb, self.values_beginning[4])
        else:
            self.exp_time_setting = Setting(self.sliders[0], self.entries[0], constants.EXP_TIME_MIN, constants.EXP_TIME_MAX, self.change_methods["exp"], self.values_beginning[0])
            self.gain_setting = Setting(self.sliders[1], self.entries[1], constants.GAIN_MIN, constants.GAIN_MAX, self.change_methods["gain"], self.values_beginning[1])
            self.wb_R_setting = Setting(self.sliders[2], self.entries[2], constants.WB_MIN, constants.WB_MAX, self.change_wb, self.values_beginning[2])
            self.wb_G_setting = Setting(self.sliders[3], self.entries[3], constants.WB_MIN, constants.WB_MAX, self.change_wb, self.values_beginning[3])
            self.wb_B_setting = Setting(self.sliders[4], self.entries[4], constants.WB_MIN, constants.WB_MAX, self.change_wb, self.values_beginning[4])

    def save_values(self):
        values = [self.exp_time_setting.value, self.gain_setting.value, self.wb_R_setting.value, self.wb_G_setting.value, self.wb_B_setting.value]
        self.main.save_cam_values(values)
        if self.main.camera_type == "VAImagingCamera":
            self.main.gamma_checked = self.gamma_enable_check.isChecked()
        self.close()
    
    def change_wb(self, _):
        values = self.wb_R_setting.value, self.wb_G_setting.value, self.wb_B_setting.value
        self.change_methods["wb"](*values)

    def auto_wb(self):
        rf, gf, bf = self.change_methods["auto_wb"]()
        
        self.wb_R_setting.set_value(int(rf))
        self.wb_G_setting.set_value(int(gf))
        self.wb_B_setting.set_value(int(bf))

    def auto_exp(self):
        exp = self.change_methods["auto_exp"]()
        
        self.gain_setting.set_value(1)
        self.exp_time_setting.set_value(exp)

    def auto_gain(self):
        gain = self.change_methods["auto_gain"]()

        self.gain_setting.set_value(int(gain))

    def close(self):
        if self.main.camera_type == "VAImagingCamera":
            self.main.gamma_checked = self.gamma_enable_check.isChecked()
        super().close()

class OrientationDialog(QDialog):
    def __init__(self, parent = None):
        super().__init__(parent)
        flags = self.windowFlags()
        flags &= ~Qt.WindowContextHelpButtonHint
        self.setWindowFlags(flags)

        self.setWindowTitle('Orientation')

        label = QLabel("Select the orientation of the image:<br><span style = 'font-size: 8pt;font-weight:normal;'>Select the main view of the specimen. You can also select a secondary view if the orientation isn't clearly defined.</span>")
        label.setStyleSheet("font: 700 12pt 'Segoe UI';")

        self.primaryCombo = QComboBox()
        self.secondaryCombo = QComboBox()

        options = ["Dorsal", "Ventral", "Left_Lateral", "Right_Lateral", "Undefined"]
        for option in options:
            self.primaryCombo.addItem(option)
            self.secondaryCombo.addItem(option)

        self.primaryCombo.setCurrentText("Undefined")
        self.secondaryCombo.setCurrentText("Undefined")
        self.primary = "Undefined"
        self.secondary = "Undefined"

        comboLayout = QHBoxLayout()
        comboLayout.addWidget(QLabel("Primary Orientation:"))
        comboLayout.addWidget(self.primaryCombo)
        comboLayout.addWidget(QLabel("Secondary Orientation:"))
        comboLayout.addWidget(self.secondaryCombo)

        self.buttonBox = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)

        mainLayout = QVBoxLayout()
        mainLayout.addWidget(label)
        mainLayout.addLayout(comboLayout)
        mainLayout.addWidget(self.buttonBox)

        self.setLayout(mainLayout)

    def getOrientation(self):
        self.primary = self.primaryCombo.currentText()
        self.secondary = self.secondaryCombo.currentText()
        return self.primary, self.secondary
    
    def setOrientation(self, primary: str, secondary: str):
        """Sets the primary and secondary orientation in the combo boxes."""
        self.primaryCombo.setCurrentText(primary)
        self.secondaryCombo.setCurrentText(secondary)

    
    # def showEvent(self, a0):
    #     super().showEvent(a0)
    #     self.primaryCombo.setCurrentText(self.primary)
    #     self.secondaryCombo.setCurrentText(self.secondary)

class Setting:
    def __init__(self, slider, spinbox, min_val, max_val, callback, begin_value):
        self.slider: QSlider = slider
        self.spinbox: QSpinBox = spinbox
        self.change_method = callback

        self.value = begin_value
        self.spinbox.setRange(min_val, max_val)
        self.slider.setValue(self.value)
        self.spinbox.setValue(self.value)
        self.slider.valueChanged.connect(self.slider_value_changed)
        self.spinbox.valueChanged.connect(self.spinbox_value_changed)
    
    def slider_value_changed(self):
        self.value = self.slider.value()
        
        if not self.value == self.spinbox.value():
            # update the spinbox if it isn't the same value
            self.spinbox.setValue(self.value)
        
        self.change_method(self.value)
    
    def spinbox_value_changed(self):
        value = self.spinbox.value()
        self.slider.setValue(value)
    
    def set_value(self, value: int):
        self.slider.setValue(value)
    
    def get_value(self):
        return self.value

class AddModelDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle('Add Model')
        self.resize(400, 100)
        self.initUI()

    def initUI(self):
        layout = QVBoxLayout()

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Enter a name for the model")
        layout.addWidget(self.name_edit)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.saveModel)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.setLayout(layout)
    
    def saveModel(self):
        if name := self.name_edit.text():
            models_dir = Path('models', 'classification', name).resolve()
            if not models_dir.exists():
                self.openFileDialog(models_dir)
                self.accept()
            else:
                QMessageBox.critical(self, "Error", f"The path {models_dir} does already exist.")
    
    def openFileDialog(self, models_directory: Path):
        file_dialog = QFileDialog(self)
        file_dialog.setWindowTitle("Select ONNX models")
        file_dialog.setWindowIcon(QIcon(r"UserInterface\imgs\enimas_icon.png"))
        file_dialog.setFileMode(QFileDialog.ExistingFiles) # select multiple files
        file_dialog.setNameFilter("ONNX Files (*.onnx)")

        if file_dialog.exec() == QFileDialog.Accepted:
            paths = [Path(path) for path in file_dialog.selectedFiles()]
            models_directory.mkdir(parents=True)
            try:
                for model_path in paths:
                    dst_path = models_directory / model_path.name
                    shutil.copy(model_path, dst_path)
                QMessageBox.information(self, "Success", f"Successfully added the model(s) to {models_directory}.")
            except Exception as e:
                shutil.rmtree(models_directory)
                QMessageBox.critical(self, "Error", f"Error adding the model.")

class ClassificationResultsViewer():
    def __init__(self, parent: QWidget, block_spacing=20):
        self.content_layout = QHBoxLayout(parent)
        self.content_layout.setSpacing(block_spacing)
        self.content_layout.setContentsMargins(0, 0, 0, 0)

    def set_text(self,
                 data_blocks: List[List[str]] | str,
                 wikipedia_search_strings: List[str] = [],
                 clear=True):
        
        if clear: self.clear()
        is_single_line = isinstance(data_blocks, str)
        data_blocks = [[data_blocks]] if is_single_line else data_blocks
        if not wikipedia_search_strings: wikipedia_search_strings = [None]*len(data_blocks)
        
        for block, search_string in zip(data_blocks, wikipedia_search_strings):
            block_layout = QVBoxLayout()
            block_layout.setSpacing(0)  # no vertical spacing
            for i, text in enumerate(block):
                label = QLabel(text)
                
                if i == 0 and not is_single_line:
                    label.setFont(QFont('Segoe UI', 9, QFont.Bold))
                else:
                    label.setFont(QFont('Segoe UI', 9))

                label.setAlignment(Qt.AlignCenter)
                label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                block_layout.addWidget(label)
            
            
            wikipedia_url = self.get_wikipedia_url(search_string) if (search_string and not 'other' in search_string.lower()) else None
            if wikipedia_url is not None:
                label = QLabel(f'<a href="{wikipedia_url}">Open in Wikipedia</a>')
                label.setOpenExternalLinks(True)
                label.setFont(QFont('Segoe UI', 9))
                label.setAlignment(Qt.AlignCenter)
                label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                block_layout.addWidget(label)
            
            block_widget = QWidget()
            block_widget.setLayout(block_layout)
            block_widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.content_layout.addWidget(block_widget)
            
    def clear(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def get_wikipedia_url(self, page_title) -> str | None:
        url = "https://en.wikipedia.org/w/api.php"
        params = {
            "action": "opensearch",
            "search": page_title,
            "limit": 1,
            "format": "json"
        }

        try:
            response = requests.get(url, params=params)
        except requests.ConnectionError:
            logger.debug("Connection error occurred while calling the wikipedia API.")
            return None
        except Exception as e:
            logger.debug(f"An error occurred while calling the wikipedia API: {e}")
            return None
        
        try:
            data = response.json()
        except ValueError as e:
            logger.debug(f"[get_wikipedia_ur]: json decode error:{e!r}")
            return None
        
        if len(data) > 1 and len(data[1]) > 0:
            url = data[3][0]
            return url
        else:
            return None
        
