import gxipy as gx
import cv2
import traceback
import numpy as np
import threading
from time import sleep
from PyQt5.QtWidgets import *
from PyQt5.QtGui import *
from PyQt5.QtCore import *
from gxipy.gxidef import *
from ctypes import *
from gxipy.dxwrapper import *
from gxipy.ImageProc import Utility
from VAImagingcam.ImageConvert import convert_to_special_pixel_format

# Setup logging
import logging
logger = logging.getLogger(__name__)

class VAImagingCamera(object):
    def __init__(self):
        self.isOpened = False
        self.running_ = False
        self.camera_lock = threading.Lock()  # Add a lock for thread safety

        self.__gamma_value = 1  # Gamma current value
        self.__contrast_value = 0  # Current value of contrast

        self.device_manager = gx.DeviceManager()
        self.dev_num, self.dev_info_list = self.device_manager.update_all_device_list()
        if self.dev_num == 0:
            print("No devices listed, please insert the camera and restart the program!")
            return
        self.dev_info = self.dev_info_list[0]

        pass

    def openCamera(self):
        # Get the list of basic device information
        str_sn = self.dev_info_list[0].get("sn")
        # Open the device by serial number
        self.cam = self.device_manager.open_device_by_sn(str_sn)
        # Set Triggermode, can connect to buttons on UI
        # self.cam.TriggerMode.set(1)
        # self.cam.TriggerSource.set(0)

        # Create Attribute Controller
        self.remote_device_feature = self.cam.get_remote_device_feature_control()
        # Creating Image Quality Improvement Parameter Configuration Objects
        self.image_process_config = self.cam.create_image_process_config()

        reverse_x = self.remote_device_feature.get_bool_feature("ReverseX")
        # Set any value within the current exposure value range
        reverse_x.set(False)
        # Get the current exposure value
        reverse_x_status = reverse_x.get()
        print("reverse_x: ", reverse_x_status)

        reverse_y = self.remote_device_feature.get_bool_feature("ReverseY")
        # Set any value within the current exposure value range
        reverse_y.set(True)
        # Get the current exposure value
        reverse_y_status = reverse_y.get()
        print("reverse_y: ", reverse_y_status)

        #create a format converter
        self.image_format_convert = self.device_manager.create_image_format_convert()
        # Create image quality improvement processing objects
        self.image_process = self.device_manager.create_image_process()

        self.read_reg_values()
        
        self.isOpened = True
        self.image_process_config.set_gamma_param(self.__gamma_value)
        self.image_process_config.set_contrast_param(self.__contrast_value)
        self.image_process_config.enable_color_correction(False)

        return self.isOpened

    def start(self):
        if not self.isOpened:
            raise RuntimeError("The camera has not been opened.")
        try:
            # Start collecting
            self.cam.stream_on()
            self.running_ = True

        except Exception as exception:
            raise RuntimeError



    def read(self, timeout = 500):
        if not self.running_ or self.cam is None:
            return False, None

        try:
            # Use a lock to ensure thread safety during camera operations
            with self.camera_lock:
                img = self.cam.data_stream[0].get_image(timeout = timeout)

        except Exception as e:
            logger.error(f"[VAImagingCamera] get_image exception:{e}")
            return False, None

        if img is None:
            return False, None

        return True, img
    
    def stop(self):
        if not self.running_:
            raise RuntimeError("The camera is not running.")
        
        self.running_ = False
        try:
            with self.camera_lock:
                self.cam.stream_off()
                self.cam.data_stream[0].unregister_capture_callback()
        except Exception as e:
            logger.error(f"Warning: Failed to stop camera stream: {e}")
            #self.cam.data_stream[0].unregister_capture_callback()

        

    def closeCamera(self):
        if not self.isOpened:
            raise RuntimeError("The camera has not been opened.")
        
        if self.running_:
            self.stop()
        
        self.isOpened = False
        try:
            with self.camera_lock:
                if self.cam is not None:
                    self.cam.close_device()
                    self.cam = None
        except Exception as e:
            logger.error(f"Error closing camera: {e}")

    def soft_trigger(self):
        if not self.running_:
            self.start()
            #raise RuntimeError("The camera is not running.")
        
        try:
            with self.camera_lock:
                if self.cam is not None:
                    self.cam.TriggerSoftware.send_command()
        except Exception as e:
            logger.error(f"Error in soft trigger: {e}")

    def read_reg_values(self):
        if self.remote_device_feature.is_implemented("ExposureTime") is True:
            exposure_time_value = self.remote_device_feature.get_float_feature("ExposureTime").get()

            exposure_range = self.remote_device_feature.get_float_feature("ExposureTime").get_range()
            self.__exposure_max = exposure_range["max"]
            self.__exposure_min = exposure_range["min"]

        else:
            print("Device does not support the ExposureTime feature.")

        if self.remote_device_feature.is_implemented("Gain") is True:
            gain_value = self.remote_device_feature.get_float_feature("Gain").get()

            gain_range = self.remote_device_feature.get_float_feature("Gain").get_range()
            self.__gain_max = gain_range["max"]
            self.__gain_min = gain_range["min"]

        else:
            print("Device does not support the feature Gain")

        #if self.remote_device_feature.is_implemented("BalanceRatioRed"):
        if self.remote_device_feature.is_readable("BalanceRatioSelector"):
            self.remote_device_feature.get_enum_feature("BalanceRatioSelector").set("Red")
            wb_r = self.remote_device_feature.get_float_feature("BalanceRatio").get()
            self.remote_device_feature.get_enum_feature("BalanceRatioSelector").set("Green")
            wb_g = self.remote_device_feature.get_float_feature("BalanceRatio").get()
            self.remote_device_feature.get_enum_feature("BalanceRatioSelector").set("Blue")
            wb_b = self.remote_device_feature.get_float_feature("BalanceRatio").get()
            return exposure_time_value, gain_value, wb_r, wb_g, wb_b

        else:
            print("Device does not support the feature White Balance")



    def set_exposure_time(self, val: int):
        exposure_time_value = float(val)

        if exposure_time_value <= self.__exposure_min:
            exposure_time_value = self.__exposure_min
        elif exposure_time_value >= self.__exposure_max:
            exposure_time_value = self.__exposure_max

        current_val = self.remote_device_feature.get_float_feature("ExposureTime").get()
        if current_val == exposure_time_value:
            return

        self.remote_device_feature.get_float_feature("ExposureTime").set(exposure_time_value)

    def set_analog_gain(self, val: int):
        gain_value = float(val)

        if gain_value <= self.__gain_min:
            gain_value = self.__gain_min
        elif gain_value >= self.__gain_max:
            gain_value = self.__gain_max

        if not self.remote_device_feature.is_writable("Gain"):
            return

        current_val = self.remote_device_feature.get_float_feature("Gain").get()
        if current_val == gain_value:
            return

        self.remote_device_feature.get_float_feature("Gain").set(gain_value)

    def set_wb_gains_factor(self, r, g, b):

        r = float(r) / 100
        g = int(g) / 100
        b = int(b) / 100
        if self.remote_device_feature.is_writable("BalanceRatio"):
            self.remote_device_feature.get_enum_feature("BalanceRatioSelector").set("Red")
            self.remote_device_feature.get_float_feature("BalanceRatio").set(r)
            self.remote_device_feature.get_enum_feature("BalanceRatioSelector").set("Green")
            self.remote_device_feature.get_float_feature("BalanceRatio").set(g)
            self.remote_device_feature.get_enum_feature("BalanceRatioSelector").set("Blue")
            self.remote_device_feature.get_float_feature("BalanceRatio").set(b)
        else:
            print("no new white balance value to set")

    def gamma_enabled_check(self):
        if self.remote_device_feature.is_implemented("GaammaEnable"):

            enabled = self.remote_device_feature.get_bool_feature("GammaEnable").get()
            return enabled
        else:
            enabled = False
            return enabled





