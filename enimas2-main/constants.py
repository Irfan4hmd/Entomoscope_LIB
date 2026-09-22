import os
from pathlib import Path

# Application version — updated with each release
APP_VERSION = "1.4.11"

# ungefähre Motorgeschwindigkeit in Umdrehungen/s
DEFAULT_SPEED = 5
LOW_SPEED = 2
MAX_SPEED = 6

"""
N: New version PI2AI
S: Short version PIs
"""
# linear Achse länge in mm
AXIS_LENGHT = 343
AXIS_LENGHT_S = 300

TRAVEL_LENGHT_N = 200  # maximaler bewegungsbereich der linearen Achse in mm
SENSOR_TO_BASE_PLATE_N = 70  # Sensor zu Basisplatte bei höchster Position in mm

TRAVEL_LENGHT_S = 216
SENSOR_TO_BASE_PLATE_S = 20

"""
lenght linear axis: 409mm
lenght slide: 66mm
max lenght zero-point to highest-slide-point: 343mm

lenght zero-point to base-plate: 422mm
highest-slide-point to lowest-sensor-point: 59mm
"""
"""
sensor to base plate at highest point: 285mm
sensor to base plate at lowest point: 70mm
215mm maximal moving rannge
"""

HELICON_PATH = "undefined"

MICROSTEPS = 16
ROTATION_HEIGHT = 10
ROTATION_HEIGHT_N = 8

STACK_SPEED = 50

# Fuse methods: 0 = Helicon Focus, 1 = Self-made, 2 = Do not stack (just save images)
LAST_FUSE_METHOD = 0

DEFAULT_NUM_OF_STACKS = 10
MIN_NUM_OF_STACKS = 2
MAX_NUM_OF_STACKS = 100

DEFAULT_STACK_STEP_SIZE = 1.0
MIN_STACK_STEP_SIZE = 0.01
MAX_STACK_STEP_SIZE = AXIS_LENGHT

# Number of pre-movement steps before stacking (set to 0 to disable pre-movement)
PRE_STACK_MOVEMENT_STEPS = 5

# Settling delay in seconds after motor movement and before image capture
# Increase this value (e.g., 0.3 to 0.5) if you see distortion in stacked images
STACK_SETTLING_DELAY = 0.3

# Fresh-frame capture policy for each focus-stack position. These values reject
# only camera acquisition failures; they do not classify image content.
STACK_CAPTURE_DISCARD_FRAMES = 2
STACK_CAPTURE_MAX_ATTEMPTS = 3
STACK_CAPTURE_TIMEOUT = 6.0
AUTOFOCUS_CAPTURE_TIMEOUT = 3.0
SINGLE_CAPTURE_MAX_ATTEMPTS = 2
SINGLE_CAPTURE_TIMEOUT = 3.0

DEFAULT_BASE_DIR = os.path.join(os.environ["USERPROFILE"], "Pictures", "ENIMAS")
IMG_EXTENSION = "tiff"
# Raw focus-stack frames always use IMG_EXTENSION. The final stack format is a
# persisted user choice made in the stacking-method dialog.
STACK_OUTPUT_EXTENSION = "tiff"
SHOW_SCALEBAR = False
STACK_JPEG_QUALITY = 95

EXP_TIME_MIN = 1
EXP_TIME_MAX = 330000

GAIN_MIN = 1
GAIN_MAX = 26

WB_SCALING = 100
WB_MIN =  WB_SCALING
WB_MAX = 16 * WB_SCALING

# Constants for the ML models
# if CROP_IMAGES is True an *.onnx file must be included in models\cropping
CROP_IMAGES = False

# Constants for VAIamgingCam
VAI_EXP_TIME_MIN = 10
VAI_EXP_TIME_MAX = 1000000

VAI_GAIN_MIN = 0
VAI_GAIN_MAX = 24

VAI_WB_MIN = 100
VAI_WB_MAX = 1599
