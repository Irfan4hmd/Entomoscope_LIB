"""source: https://github.com/ArduCAM/ArduCAM_USB_Camera_Shield_Python_Demo/blob/main/Arducam.py"""


import constants
import threading
import time

import ArducamSDK
from Arducam.utils import *

# Setup logging
import logging
logger = logging.getLogger(__name__)


class ArducamCamera(object):
    def __init__(self):
        self.isOpened = False
        self.running_ = False
        self.signal_ = threading.Condition()
        self.camera_lock = threading.Lock()  # Add lock for thread safety
        self.handle = None
        self.capture_thread_ = None
        self.last_frame_time = None  # Track last successful frame capture
        self.dev_num = 0  # Number of detected devices

        # Try to enumerate devices
        try:
            self.dev_num = ArducamSDK.Py_ArduCam_scan()
            if self.dev_num == 0:
                logger.warning("No Arducam devices detected. Please connect the camera.")
            else:
                logger.info(f"Found {self.dev_num} Arducam device(s)")
        except Exception as e:
            logger.error(f"Error scanning for Arducam devices: {e}")
            self.dev_num = 0

    def openCamera(self, fname, index=0):
        try:
            # No lock needed - SDK handles internal synchronization
            self.isOpened, self.handle, self.cameraCfg, self.color_mode = camera_initFromFile(
                fname, index)

            if self.isOpened:
                logger.info(f"Arducam camera opened successfully (index {index})")
            else:
                logger.error(f"Failed to open Arducam camera (index {index})")

            return self.isOpened
        except Exception as e:
            logger.error(f"Exception while opening Arducam camera: {e}")
            self.isOpened = False
            return False

    def start(self):
        if not self.isOpened:
            raise RuntimeError("The camera has not been opened.")

        try:
            # No lock needed - called before capture thread starts
            self.running_ = True
            self.last_frame_time = time.time()  # Initialize frame timestamp
            ArducamSDK.Py_ArduCam_setMode(self.handle, ArducamSDK.CONTINUOUS_MODE)

            self.capture_thread_ = threading.Thread(target=self.capture_thread)
            self.capture_thread_.daemon = True  # Keep as daemon to avoid hanging on exit
            self.capture_thread_.start()
            logger.info("Arducam capture thread started successfully")
        except Exception as e:
            logger.error(f"Error starting Arducam camera: {e}")
            self.running_ = False
            raise RuntimeError(f"Failed to start camera: {e}")
    
    def read(self, timeout=1500):
        if not self.running_:
            logger.error("[ArducamCamera] Camera is not running")
            return (False, None, None)

        try:
            # Check if images are available
            if ArducamSDK.Py_ArduCam_availableImage(self.handle) <= 0:
                with self.signal_:
                    self.signal_.wait(timeout/1000.0)

            # Check again after waiting
            if ArducamSDK.Py_ArduCam_availableImage(self.handle) <= 0:
                # Check if capture thread is still alive
                if self.capture_thread_ and not self.capture_thread_.is_alive():
                    logger.error("[ArducamCamera] Capture thread has died")
                    self.running_ = False
                return (False, None, None)

            # Don't use lock here - SDK handles synchronization between capture and read
            ret, data, cfg = ArducamSDK.Py_ArduCam_readImage(self.handle)
            ArducamSDK.Py_ArduCam_del(self.handle)

            size = cfg['u32Size']
            if ret != 0 or size == 0:
                logger.warning(f"[ArducamCamera] Invalid frame: ret={ret}, size={size}")
                return (False, data, cfg)

            self.last_frame_time = time.time()  # Update last frame time
            return (True, data, cfg)

        except Exception as e:
            logger.error(f"[ArducamCamera] Exception in read(): {e}")
            return (False, None, None)

    def stop(self):
        if not self.running_:
            raise RuntimeError("The camera is not running.")

        logger.info("Stopping Arducam camera...")
        self.running_ = False

        # Wait for capture thread to finish with timeout
        if self.capture_thread_ and self.capture_thread_.is_alive():
            self.capture_thread_.join(timeout=5.0)  # 5 second timeout

            # Check if thread is still alive after timeout
            if self.capture_thread_.is_alive():
                logger.error("[ArducamCamera] Capture thread did not stop within timeout!")
                # Thread is stuck, but we can't force kill it in Python
                # Just log and continue
            else:
                logger.info("Arducam capture thread stopped successfully")

    def closeCamera(self):
        if not self.isOpened:
            raise RuntimeError("The camera has not been opened.")

        if self.running_:
            try:
                self.stop()  # This waits for capture thread to finish
            except Exception as e:
                logger.error(f"Error stopping camera during close: {e}")

        self.isOpened = False
        try:
            # No lock needed - capture thread already stopped
            if self.handle is not None:
                ArducamSDK.Py_ArduCam_close(self.handle)
                self.handle = None
                logger.info("Arducam camera closed successfully")
        except Exception as e:
            logger.error(f"Error closing Arducam camera: {e}")
            self.handle = None

    def capture_thread(self):
        try:
            ret = ArducamSDK.Py_ArduCam_beginCaptureImage(self.handle)

            if ret != 0:
                logger.error(f"Error beginning capture: {GetErrorString(ret)}")
                self.running_ = False
                return

            logger.info("Arducam capture thread started")

            # Keep capture loop simple and close to original
            while self.running_:
                ret = ArducamSDK.Py_ArduCam_captureImage(self.handle)

                if ret > 255:
                    # Error occurred
                    if ret == ArducamSDK.USB_CAMERA_USB_TASK_ERROR:
                        logger.error("USB task error - stopping capture")
                        break
                    # Continue on other errors
                elif ret > 0:
                    # Frame captured successfully, notify waiting thread
                    with self.signal_:
                        self.signal_.notify()

        except Exception as e:
            logger.error(f"Exception in capture_thread: {e}")

        finally:
            self.running_ = False
            try:
                ArducamSDK.Py_ArduCam_endCaptureImage(self.handle)
                logger.info("Arducam capture thread stopped")
            except Exception as e:
                logger.error(f"Error ending capture: {e}")

    def read_reg_values(self):
        err, exp1 = ArducamSDK.Py_ArduCam_readSensorReg(self.handle, 0x202)
        err, exp2 = ArducamSDK.Py_ArduCam_readSensorReg(self.handle, 0x203)
        hts, pix_clk_hz = 12740, 840000000
        val = (exp1 << 8) | exp2
        exp_time = int(val*hts*1e9/(1000*pix_clk_hz))

        err, gain1 = ArducamSDK.Py_ArduCam_readSensorReg(self.handle, 0x0204)
        err, gain2 = ArducamSDK.Py_ArduCam_readSensorReg(self.handle, 0x0205)
        val = (gain1 << 8) | gain2
        gain = int(1024/(1024-val))

        err, wb_r_upper = ArducamSDK.Py_ArduCam_readSensorReg(self.handle, 0x0210)
        err, wb_r_lower = ArducamSDK.Py_ArduCam_readSensorReg(self.handle, 0x0211)
        wb_r = wb_r_upper + wb_r_lower / 256

        err, wb_gr_upper = ArducamSDK.Py_ArduCam_readSensorReg(self.handle, 0x020E)
        err, wb_gr_lower = ArducamSDK.Py_ArduCam_readSensorReg(self.handle, 0x020F)
        wb_gr = wb_gr_upper + wb_gr_lower / 256

        err, wb_b_upper = ArducamSDK.Py_ArduCam_readSensorReg(self.handle, 0x0212)
        err, wb_b_lower = ArducamSDK.Py_ArduCam_readSensorReg(self.handle, 0x0213)
        wb_b = wb_b_upper + wb_b_lower / 256

        return exp_time, gain, int(wb_r*constants.WB_SCALING), int(wb_gr*constants.WB_SCALING), int(wb_b*constants.WB_SCALING)

    def set_exposure_time(self, val: int):
        '''Set the exposure time in th unit of [µs]
        MIN_VALUE   = 1
        MAX_VALUE   = 330000
        STEP        = 1
        '''
        hts = 12740
        pix_clk_hz = 840000000
        exp = round(val*1000/(hts/pix_clk_hz*1e9))
        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x0202, (exp & 0xFF00) >> 8)
        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x0203, (exp & 0x00FF) >> 0)

    def set_analog_gain(self, val: int):
        '''Set the gain with a gain factor from 1 to 26'''
        gain = round(1024 - (1024 / val))
        # print('gain: ', gain)
        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x0204, (gain & 0xFF00) >> 8)
        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x0205, (gain & 0x00FF) >> 0)
   
    def set_wb_gains_factor(self, r_gain, gr_gain, b_gain):
        """
        Set the digital gain for the individual colors.
        Values range from 0x0100 to 0x0FFF (1.0x to 15.99x gain)

                7654 3210 
        XXXX XXXX XXXX XXXX
        UpperByte LowerByte

        Digital gain [times] = upper_byte + (lower_byte/256)

        upper_byte ranges from 1-15
        lower_byte ranges from 0-255
        """
        
        # CRITICAL: Clamp gains to valid range to prevent weird colors
        # WB_SCALING = 100, so valid range is 100-1600 (1.0x to 16.0x)
        min_gain = constants.WB_MIN  # 100
        max_gain = constants.WB_MAX  # 1600
        
        r_gain = max(min_gain, min(max_gain, r_gain))
        gr_gain = max(min_gain, min(max_gain, gr_gain))
        b_gain = max(min_gain, min(max_gain, b_gain))
        
        # Log if clamping occurred
        if r_gain != r_gain or gr_gain != gr_gain or b_gain != b_gain:
            print(f"Warning: White balance gains clamped to valid range ({min_gain}-{max_gain})")
            print(f"  R: {r_gain} -> {r_gain}, GR: {gr_gain} -> {gr_gain}, B: {b_gain} -> {b_gain}")

        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x3FF9, 0)
        gr_gain, r_gain, b_gain = gr_gain/constants.WB_SCALING, r_gain/constants.WB_SCALING, b_gain/constants.WB_SCALING

        GAIN_GR_upper = int(gr_gain)
        GAIN_GR_lower = int(round((gr_gain-float(GAIN_GR_upper))*256))
        if GAIN_GR_lower == 256:
            GAIN_GR_lower = 255
        
        # Validate upper byte is within sensor limits (1-15)
        GAIN_GR_upper = max(1, min(15, GAIN_GR_upper))

        GAIN_GB_lower = GAIN_GR_lower
        GAIN_GB_upper = GAIN_GR_upper

        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x020E, GAIN_GR_upper)
        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x020F, GAIN_GR_lower)
        
        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x0214, GAIN_GB_upper)
        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x0215, GAIN_GB_lower)

        GAIN_R_upper = int(r_gain)
        GAIN_R_lower = int(round((r_gain-float(GAIN_R_upper))*256))
        
        # Validate upper byte is within sensor limits (1-15)
        GAIN_R_upper = max(1, min(15, GAIN_R_upper))

        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x0210, GAIN_R_upper)
        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x0211, GAIN_R_lower)

        GAIN_B_upper = int(b_gain)
        GAIN_B_lower = int(round((b_gain-float(GAIN_B_upper))*256))
        
        # Validate upper byte is within sensor limits (1-15)
        GAIN_B_upper = max(1, min(15, GAIN_B_upper))
    
        # print(f"WB: {GAIN_GR_upper}:{GAIN_GR_lower}, {GAIN_GB_upper}:{GAIN_GB_lower}, {GAIN_R_upper}:{GAIN_R_lower}, {GAIN_B_upper}:{GAIN_B_lower}")
        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x0212, GAIN_B_upper)
        ArducamSDK.Py_ArduCam_writeSensorReg(self.handle, 0x0213, GAIN_B_lower)
