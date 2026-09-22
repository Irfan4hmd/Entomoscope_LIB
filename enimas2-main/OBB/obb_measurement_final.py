import os
import cv2
import numpy as np
import math
import uuid
import logging
from ultralytics import YOLO

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =============================================================================
# IMAGE & MEASUREMENT UTILITIES
# =============================================================================
class ImageUtils:
    """
    Utility class for image-related functions including padding, background
    detection, and conversion from pixels to millimeters.
    """
    # Dictionary mapping lens names to mm/pixel conversion factors
    lens_conversion_factors = {

        'ArducamCamera': {
            'MV65051B': 0.00315455,
            'LCM-TELECENTRIC-0.3X-WD65-1.5-NI2 - GetCameras': 0.00525758,
            'LCM-TELECENTRIC-1X-WD65-1.5-NI - GetCameras': 0.00157727,
            'LCM-TELECENTRIC-0.5X-WD65-1.5-NI - GetCameras': 0.00315455,  # MV65051B - 117 mm correct
            'MV65051D - 117mm': 0.00315455,
            'TC10M-05-65I': 0.00315455, #correct
            'Lensagon TC5M-10-65 - Lensation': 0.00157727,  # TC5M-10-65
            'Lensagon TCST-10-40 - Lensation': 0.00157727,  # TCST-10-40
            'TCST-10-40 - 58mm': 0.00157727,
            'TCST-10-65': 0.00157727,
            'TC5M-03-110 - 142mm': 0.00525758,
            'Lensagon TC5M-03-110 - Lensation': 0.00525758,  # TC5M-03-110
            # 'SmallCam (3-6mm)': 0.00157727,
            'TC-20-40 - Lensation': 0.00078863,  # TCST-20-40
            'Lensagon TCST-30-40 - Lensation': 0.00052575,  # TCST-30-40
            'Telecentric Lens 0.2X WD65': 0.007886375,
            },

        'VAImagingCamera': {
            'LCM-TELECENTRIC-0.3X-WD65-1.5-NI2 - GetCameras': 0.00595945,
            'LCM-TELECENTRIC-1X-WD65-1.5-NI - GetCameras': 0.00178784,
            'LCM-TELECENTRIC-0.5X-WD65-1.5-NI - GetCameras': 0.00357567,  # MV65051B - 117 mm correct
            'TC10M-05-65I': 0.00357567, #correct
            'Lensagon TC5M-10-65 - Lensation': 0.00178784,  # TC5M-10-65
            'Lensagon TCST-10-40 - Lensation': 0.00178784,  # TCST-10-40
            'TCST-10-40 - 58mm': 0.00178784,
            'TCST-10-65': 0.00178784,
            'TC5M-03-110 - 142mm': 0.00595945,
            'Lensagon TC5M-03-110 - Lensation': 0.00595945,  # TC5M-03-110
            'TC-20-40 - Lensation': 0.00089392,  # TCST-20-40
            'Lensagon TCST-30-40 - Lensation': 0.00059594,  # TCST-30-40
            'Telecentric Lens 0.2X WD65': 0.00893918,
            }
    }

    @staticmethod
    def pixels_to_mm(pixel_value, lens, conversion_factor):
        """
        Convert a pixel value to millimeters based on the provided lens.
        """
        #conversion_factor = ImageUtils.lens_conversion_factors.get(lens)
        # print(type(pixel_value))
        # print(type(conversion_factor))
        if conversion_factor is not None:
            return pixel_value * conversion_factor
        else:
            logger.warning(f"Lens '{lens}' not found in conversion dictionary. Returning original pixel value.")
            return pixel_value

    @staticmethod
    def detect_background_color(image):
        """
        Detect the background color by averaging the colors of the image borders.
        """
        top_border = image[0, :]
        bottom_border = image[-1, :]
        left_border = image[:, 0]
        right_border = image[:, -1]
        # Concatenate all border pixels and compute the mean color
        border_pixels = np.concatenate((top_border, bottom_border, left_border, right_border), axis=0)
        average_color = np.mean(border_pixels, axis=0).astype(int)
        return (int(average_color[0]), int(average_color[1]), int(average_color[2]))

    @staticmethod
    def pad_image_with_detected_color(image, padding_percentage):
        """
        Pad the image with its detected background color based on the given percentage.
        """
        height, width, _ = image.shape
        pad_h = int(height * padding_percentage)
        pad_w = int(width * padding_percentage)
        background_color = ImageUtils.detect_background_color(image)
        padded_image = cv2.copyMakeBorder(
            image, pad_h, pad_h, pad_w, pad_w,
            cv2.BORDER_CONSTANT, value=background_color
        )
        return padded_image

class MeasurementUtils:
    """
    Utility class for measuring and annotating bounding boxes.
    """

    @staticmethod
    def order_points(pts):
        """
        Order 4 points in a consistent order: top-left, top-right, bottom-right, bottom-left.
        """
        rect = np.zeros((4, 2), dtype="float32")
        s = pts.sum(axis=1)
        diff = np.diff(pts, axis=1)
        rect[0] = pts[np.argmin(s)]
        rect[2] = pts[np.argmax(s)]
        rect[1] = pts[np.argmin(diff)]
        rect[3] = pts[np.argmax(diff)]
        return rect

    @staticmethod
    def euclidean_distance(p1, p2):
        """
        Calculate the Euclidean distance between two points.
        """
        return math.hypot(p2[0] - p1[0], p2[1] - p1[1])

    @staticmethod
    def categorize_size(height_mm):
        """
        Categorize the size of the bounding box based on the height in millimeters.
        """
        if height_mm < 1.5:
            return 'Small'
        elif 1.5 <= height_mm <= 5:
            return 'Medium'
        elif 5 < height_mm <= 10:
            return 'Large'
        else:
            return 'Extra Large'

    @staticmethod
    def calculate_box_lengths(box, lens, scale):
        """
        Calculate the width and height (in both pixels and mm) of a rotated bounding box.
        Assumes the shorter side is the width.
        """
        print("\n=== Debug calculate_box_lengths ===")
        print(f"Input box: {box}")
        print(f"Input lens: {lens}")
        print(f"Input scale: {scale}")
        
        pts = cv2.boxPoints(box)
        pts = np.array(pts, dtype="float32")
        pts = MeasurementUtils.order_points(pts)
        (tl, tr, br, bl) = pts
        print(f"Ordered points: tl={tl}, tr={tr}, br={br}, bl={bl}")

        width_px = MeasurementUtils.euclidean_distance(tl, tr)
        height_px = MeasurementUtils.euclidean_distance(tr, br)
        print(f"Initial measurements - width_px: {width_px}, height_px: {height_px}")

        # Ensure width is the shorter side
        if width_px >= height_px:
            height_px, width_px = width_px, height_px
            print(f"After swap - width_px: {width_px}, height_px: {height_px}")

        width_mm = ImageUtils.pixels_to_mm(width_px, lens, scale)
        height_mm = ImageUtils.pixels_to_mm(height_px, lens, scale)
        print(f"Final measurements - width_mm: {width_mm}, height_mm: {height_mm}")

        logger.info(f"Width (shorter side): {width_px:.2f} pixels, {width_mm:.2f} mm")
        logger.info(f"Height (longer side): {height_px:.2f} pixels, {height_mm:.2f} mm")
        logger.info(f"Size Category based on height: {MeasurementUtils.categorize_size(height_mm)}")

        return width_px, height_px, width_mm, height_mm

    @staticmethod
    def annotate_image(image, box, width_mm, height_mm, arrow_thickness=15, tipLength=0.05):
        """
        Annotate the image with width and height lines (arrows) and corresponding mm labels.
        The shorter side (width) is drawn in green and the longer side (height) in red.
        """
        pts = cv2.boxPoints(box)
        pts = np.array(pts, dtype="float32")
        pts = MeasurementUtils.order_points(pts)
        (tl, tr, br, bl) = pts

        # Compute edge lengths of the two main sides
        edge1 = MeasurementUtils.euclidean_distance(tl, tr)
        edge2 = MeasurementUtils.euclidean_distance(tr, br)

        if edge1 < edge2:
            width_line = (tl, tr)
            height_line = (tr, br)
            width_mid = (int((tl[0] + tr[0]) / 2), int((tl[1] + tr[1]) / 2))
            height_mid = (int((tr[0] + br[0]) / 2), int((tr[1] + br[1]) / 2))
        else:
            width_line = (tr, br)
            height_line = (tl, tr)
            width_mid = (int((tr[0] + br[0]) / 2), int((tr[1] + br[1]) / 2))
            height_mid = (int((tl[0] + tr[0]) / 2), int((tl[1] + tr[1]) / 2))

        # Convert points to integers for drawing functions
        width_line_pt1 = tuple(int(x) for x in width_line[0])
        width_line_pt2 = tuple(int(x) for x in width_line[1])
        height_line_pt1 = tuple(int(x) for x in height_line[0])
        height_line_pt2 = tuple(int(x) for x in height_line[1])

        # Draw width (green) and height (red) arrows
        cv2.arrowedLine(image, width_line_pt1, width_line_pt2, (0, 255, 0), thickness=arrow_thickness, tipLength=tipLength)
        cv2.arrowedLine(image, width_line_pt2, width_line_pt1, (0, 255, 0), thickness=arrow_thickness, tipLength=tipLength)
        cv2.arrowedLine(image, height_line_pt1, height_line_pt2, (0, 0, 255), thickness=arrow_thickness, tipLength=tipLength)
        cv2.arrowedLine(image, height_line_pt2, height_line_pt1, (0, 0, 255), thickness=arrow_thickness, tipLength=tipLength)

        # Draw text annotations for the measurements
        cv2.putText(image, f"{width_mm:.2f} mm", (width_mid[0] + 10, width_mid[1] + 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 0), 6)
        cv2.putText(image, f"{height_mm:.2f} mm", (height_mid[0] + 10, height_mid[1] + 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 6)
        return image

# =============================================================================
# DETECTION MODEL INTEGRATION
# =============================================================================
class DetectionModel:
    """
    Wrapper class for the YOLO-based detection model.
    Handles model loading, image pre-processing, and box detection.
    """
    def __init__(self, model_path):
        self.model = YOLO(model_path, task="obb")

    def process_image_for_detection(self, file_path, padding_percentage):
        """
        Reads the image, applies padding, saves a temporary padded image,
        and returns the temporary file path.
        """
        image = cv2.imread(file_path)
        if image is None:
            logger.error(f"Failed to load image: {file_path}")
            return None
        padded_image = ImageUtils.pad_image_with_detected_color(image, padding_percentage)
        temp_file_path = os.path.splitext(file_path)[0] + f"_padded_{uuid.uuid4().hex}.jpg"
        cv2.imwrite(temp_file_path, padded_image)
        return temp_file_path

    def perform_detection(self, image_path, padding_percentage):
        """
        Runs the model detection on the padded image and returns the oriented
        bounding box in original image coordinates.
        """
        padded_image_path = None
        try:
            padded_image_path = self.process_image_for_detection(image_path, padding_percentage)
            if padded_image_path is None:
                return None
            results = self.model(padded_image_path, imgsz=1024, conf=0.5, max_det=1, save=False)
            #self.saved_image_path = os.path.splitext(image_path)[0] + f"_detected_{uuid.uuid4().hex}.jpg"
            if results and len(results) > 0:
                result = results[0]
                #cv2.imwrite(self.saved_image_path, result.plot())
                if hasattr(result, 'obb') and result.obb:
                    # Extract and reshape the detected box corners
                    corners_tensor = result.obb.xyxyxyxy
                    corners = corners_tensor[0].cpu().numpy().reshape((4, 2))
                    ordered_corners = MeasurementUtils.order_points(corners)
                    box = cv2.minAreaRect(ordered_corners)
                    center = (float(box[0][0]), float(box[0][1]))
                    size = (float(box[1][0]), float(box[1][1]))
                    angle = float(box[2])
                    if size[0] < size[1]:
                        size = (size[1], size[0])
                        angle += 90
                    if angle > 0:
                        angle -= 180
                    angle = normalize_angle(angle)
                    return (center, size, angle)
                else:
                    logger.info(f"No OBB detected in {os.path.basename(image_path)}, skipping.")
                    return None
            else:
                logger.info(f"No detection in {os.path.basename(image_path)}, skipping.")
                return None
        except Exception as e:
            logger.error(f"Error processing {image_path}: {e}")
            return None
        finally:
            # Clean up temporary padded image
            if padded_image_path and os.path.exists(padded_image_path):
                os.remove(padded_image_path)

def normalize_angle(angle):
    """
    Normalize an angle to be within the range [-90, 90) degrees.
    """
    while angle < -90:
        angle += 180
    while angle >= 90:
        angle -= 180
    return angle

