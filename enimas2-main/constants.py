import os
from pathlib import Path

# Application version — updated with each release
APP_VERSION = "1.4.11"

# ungefähre Motorgeschwindigkeit in Umdrehungen/s
DEFAULT_SPEED = 3
LOW_SPEED = 2
MAX_SPEED = 4

"""
N: New version PI2AI
S: Short version PIs
"""
# Linear axis length in mm: zero point to the highest slide point.
# This machine's rail is 300mm (measured), against 343mm in the reference
# build. It is read only by the "Entomoscope PI" profile; the PIs and PI2AI
# profiles use their own TRAVEL_LENGHT_* below.
AXIS_LENGHT = 300
AXIS_LENGHT_S = 300  # unused - nothing reads this

# Usable movement range of the linear axis in mm. This is NOT the rail length:
# the carriage and end clearances take up the difference. These are the values
# that bound how deep the stage may go, so raising one lets the lens travel
# further down. Only change them against a measured travel.
TRAVEL_LENGHT_N = 200
SENSOR_TO_BASE_PLATE_N = 70  # sensor to base plate at the LOWEST position, in mm

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

# Microstepping set on the stepper driver. This MUST match the driver's jumpers:
# it scales steps-per-mm, and a value larger than the hardware uses makes the
# stage travel further than the software books, which is how the lens reaches
# the base plate while every soft limit still reads as satisfied.
# 8 agrees with the firmware's own note in servo.ino ("1 step = 1.8/8 = 0.225")
# and with the measured travel. If you change the driver to 1/16, set 16 here.
MICROSTEPS = 8
ROTATION_HEIGHT = 10
ROTATION_HEIGHT_N = 8   # measured: 8mm lead screw on this machine

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

# Distance in mm the stage always keeps clear of its lowest reachable point, so
# that a lens can never be driven onto the specimen or the base plate.
AXIS_SAFETY_MARGIN = 35

# How long to wait for the Arduino's reply to a single move before giving up.
# A full traverse at LOW_SPEED takes about 13s, so this is generous: it only
# fires when the controller has genuinely stopped answering, and it is what
# stops a blocking move from waiting on that reply forever.
AXIS_MOVE_REPLY_TIMEOUT = 60
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
