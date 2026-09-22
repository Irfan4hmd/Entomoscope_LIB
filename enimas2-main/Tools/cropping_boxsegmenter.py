import numpy as np
import cv2
import threading
import logging
import gc
from pathlib import Path
from PIL import Image
from refiners.solutions import BoxSegmenter
import time
import atexit

# Setup logging
logger = logging.getLogger(__name__)

# Global segmenter instance with thread-local storage to avoid sharing between threads
_thread_local = threading.local()
_segmenter_lock = threading.Lock()
_initialized_threads = set()
_threads_lock = threading.Lock()

def _create_segmenter(device="cpu"):
    """Create a new BoxSegmenter instance with proper error handling"""
    try:
        logger.debug(f"Creating new BoxSegmenter on {device}")
        return BoxSegmenter(device=device)
    except Exception as e:
        logger.error(f"Failed to create BoxSegmenter: {e}")
        return None

def _get_thread_segmenter(device="cpu"):
    """Get or create a thread-local segmenter instance"""
    thread_id = threading.get_ident()
    
    # Register this thread for cleanup
    with _threads_lock:
        _initialized_threads.add(thread_id)
    
    # Check if we have a segmenter for this thread
    if not hasattr(_thread_local, 'segmenter'):
        _thread_local.segmenter = _create_segmenter(device)
    
    return _thread_local.segmenter

def _cleanup_thread_segmenter():
    """Clean up the segmenter for the current thread"""
    if hasattr(_thread_local, 'segmenter') and _thread_local.segmenter is not None:
        try:
            logger.debug("Cleaning up thread-local BoxSegmenter")
            segmenter = _thread_local.segmenter
            _thread_local.segmenter = None
            del segmenter
            # Force garbage collection to ensure resources are released
            gc.collect()
        except Exception as e:
            logger.error(f"Error cleaning up thread-local BoxSegmenter: {e}")

def cleanup_all_segmenters():
    """Clean up all segmenters - should be called at application exit"""
    logger.debug("Cleaning up all BoxSegmenter instances")
    _cleanup_thread_segmenter()  # Clean up current thread
    
    # Force garbage collection to ensure resources are released
    gc.collect()
    time.sleep(0.1)  # Give a moment for cleanup to complete
    gc.collect()  # Run garbage collection again to be thorough

# Register cleanup function to run at exit
atexit.register(cleanup_all_segmenters)

def get_backgound_mask(image_path: str, 
                      box_prompt: tuple[int, int, int, int] | None = None, 
                      device: str = "cpu" ) -> tuple[Image.Image, Image.Image]:
    """
    Remove the background of an image using the BoxSegmenter.

    Args:
        image_path (str): The path to the input image.
        box_prompt (tuple[int, int, int, int] | None): Optional bounding box coordinates (x0, y0, x1, y1) for segmentation.
        device (str): The device to use for processing ('cpu' or 'cuda').

    Returns:
        tuple: (mask, original_image) where mask is the segmentation mask
    """
    try:
        # Load the image
        img = Image.open(image_path).convert("RGB")
        logger.debug(f"Image loaded: {image_path}, size: {img.size}")
        
        # Get thread-local segmenter
        segmenter = _get_thread_segmenter(device)
        if segmenter is None:
            logger.error("Failed to get segmenter instance")
            return None, img
            
        # Run the segmenter on the image with timeout protection
        try:
            mask = segmenter(img, box_prompt)
            return mask, img
        except Exception as e:
            logger.error(f"Error during segmentation: {e}")
            # If segmentation fails, try to recreate the segmenter
            _cleanup_thread_segmenter()
            segmenter = _get_thread_segmenter(device)
            if segmenter is None:
                logger.error("Failed to recreate segmenter after error")
                return None, img
                
            # Try one more time
            try:
                mask = segmenter(img, box_prompt)
                return mask, img
            except Exception as retry_error:
                logger.error(f"Segmentation retry failed: {retry_error}")
                return None, img
                
    except Exception as e:
        logger.error(f"Error in get_backgound_mask: {e}")
        return None, None

# Constants
extensions = [".jpg", ".jpeg", ".png", ".bmp", ".tiff"]
confidence = 0.80
output_show_size = (300, 300)  # Size to which the cropped images will be resized for display
show_images = False


def extract_bounding_box_from_mask(mask: np.ndarray) -> tuple[int, int, int, int]:
    """
    Extracts bounding box coordinates from a binary image mask.

    Args:
        mask (np.ndarray): Binary mask as numpy array

    Returns:
        tuple: Bounding box coordinates (x1, y1, x2, y2).
    """
    try:
        if mask is None:
            logger.error("Mask is None, cannot extract bounding box")
            return 0, 0, 100, 100  # Return a default small box
            
        # Ensure mask is a numpy array
        if not isinstance(mask, np.ndarray):
            logger.warning("Converting mask to numpy array")
            mask = np.array(mask)
            
        # Threshold the mask to ensure it's binary
        _, binary_mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)

        # Find contours in the binary mask
        contours, _ = cv2.findContours(binary_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if not contours:
            logger.warning("No contours found in the mask image, using full image")
            h, w = mask.shape[:2]
            return 0, 0, w, h

        # Find the largest contour (assuming the object of interest is the largest)
        largest_contour = max(contours, key=cv2.contourArea)

        # Get the bounding box for the largest contour
        x, y, w, h = cv2.boundingRect(largest_contour)

        # Return bounding box coordinates as (x1, y1, x2, y2)
        return x, y, x + w, y + h
    except Exception as e:
        logger.error(f"Error extracting bounding box from mask: {e}")
        # Return a safe default
        return 0, 0, 100, 100

def save_cropped_images(image, input_path):
    """
    Save the cropped image to disk.
    
    Args:
        image: The PIL Image to save
        input_path: The original input image path
        
    Returns:
        Path: The path to the saved cropped image
    """
    try:
        orig = Path(input_path)
        stem = orig.stem
        ext = orig.suffix or ".png"

        output_dir = orig.parent
        output_file = output_dir / f"{stem}_cropped{ext}"
        
        # Ensure image is a valid PIL Image
        if image is None:
            logger.error("Cannot save None image")
            return None
            
        image.save(output_file)
        logger.debug(f"Saved cropped image to {output_file}")
        return output_file
    except Exception as e:
        logger.error(f"Error saving cropped image: {e}")
        return None

def cleanup_resources():
    """
    Clean up the BoxSegmenter instances properly.
    Call this when the application is shutting down.
    """
    logger.debug("Cleaning up BoxSegmenter resources")
    cleanup_all_segmenters()


def crop_image(image_path: str, show_images: bool = False) -> Path:
    """
    Crops the image using BoxSegmenter and saves the cropped image.
    
    Args:
        image_path (str): Path to the input image
        show_images (bool): Whether to display the cropped image
        
    Returns:
        Path: Path to the saved cropped image file, or None if cropping failed
    """
    try:
        logger.debug(f"Cropping image: {image_path}")
        mask, image = get_backgound_mask(image_path, device="cpu")
        
        if image is None:
            logger.error("Failed to load image for cropping")
            return None
            
        # Convert to numpy array
        image_np = np.array(image)
        
        # Get mask and handle potential None value
        if mask is None:
            logger.warning("No mask generated, using entire image")
            h, w = image_np.shape[:2]
            x1, y1, x2, y2 = 0, 0, w, h
        else:
            mask_np = np.array(mask)
            x1, y1, x2, y2 = extract_bounding_box_from_mask(mask_np)
        
        # Safety check on coordinates
        h, w = image_np.shape[:2]
        x1 = max(0, min(x1, w-1))
        y1 = max(0, min(y1, h-1))
        x2 = max(x1+1, min(x2, w))
        y2 = max(y1+1, min(y2, h))
            
        # Crop using NumPy array with safety checks
        try:
            cropped_image = image_np[y1:y2, x1:x2]
            cropped_image_pil = Image.fromarray(cropped_image)
            cropped_img_file = save_cropped_images(cropped_image_pil, image_path)
        except Exception as crop_error:
            logger.error(f"Error during cropping: {crop_error}")
            return None
        
        if show_images and cropped_image is not None:
            try:
                # Create small image of cropped image to show
                show_image = cv2.resize(cropped_image, output_show_size)
                cv2.imshow("Cropped Image", show_image)
                cv2.waitKey(5)
            except Exception as show_error:
                logger.error(f"Error showing cropped image: {show_error}")

        return cropped_img_file
    except Exception as e:
        logger.error(f"Error in crop_image: {e}")
        return None


def crop_image_1(image_path: str, show_images: bool = False) -> Image.Image:
    """
    For cropping plugin - returns the PIL Image without saving it
    
    Args:
        image_path (str): Path to the input image
        show_images (bool): Whether to display the cropped image
        
    Returns:
        Image.Image: The cropped PIL Image, or None if cropping failed
    """
    try:
        mask, image = get_backgound_mask(image_path, device="cpu")
        
        if image is None:
            logger.error("Failed to load image for cropping")
            return None
            
        # Convert to numpy array
        image_np = np.array(image)
        
        # Get mask and handle potential None value
        if mask is None:
            logger.warning("No mask generated, using entire image")
            h, w = image_np.shape[:2]
            x1, y1, x2, y2 = 0, 0, w, h
        else:
            mask_np = np.array(mask)
            x1, y1, x2, y2 = extract_bounding_box_from_mask(mask_np)
        
        # Safety check on coordinates
        h, w = image_np.shape[:2]
        x1 = max(0, min(x1, w-1))
        y1 = max(0, min(y1, h-1))
        x2 = max(x1+1, min(x2, w))
        y2 = max(y1+1, min(y2, h))
            
        # Crop using NumPy array
        cropped_image = image_np[y1:y2, x1:x2]
        cropped_image_pil = Image.fromarray(cropped_image)
        
        if show_images:
            try:
                # Create small image of cropped image to show
                show_image = cv2.resize(cropped_image, output_show_size)
                cv2.imshow("Cropped Image", show_image)
                cv2.waitKey(5)
            except Exception as show_error:
                logger.error(f"Error showing cropped image: {show_error}")
                
        return cropped_image_pil
    except Exception as e:
        logger.error(f"Error in crop_image_1: {e}")
        return None
    