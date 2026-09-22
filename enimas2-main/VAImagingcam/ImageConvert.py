import gxipy as gx
from PIL import Image
import ctypes
from ctypes import c_ubyte, addressof, POINTER
from gxipy.gxidef import GxPixelFormatEntry, DxValidBit
import numpy
import threading
import traceback
from gxipy.ImageFormatConvert import ImageFormatConvert

# Global variables with thread safety
image_convert_lock = threading.Lock()
device_manager = gx.DeviceManager()
image_convert = device_manager.create_image_format_convert()
image_process = device_manager.create_image_process()

# Utility class for color mode operations
class ImageUtility:
    @staticmethod
    def is_gray(color_mode):
        """
        Determine if the color mode is grayscale
        
        Args:
            color_mode: The color mode to check
            
        Returns:
            bool: True if grayscale, False otherwise
        """
        gray_modes = ['MONO', 'GRAY', 'BW']
        return any(mode in str(color_mode).upper() for mode in gray_modes)

def get_best_valid_bits(pixel_format):
    valid_bits = DxValidBit.BIT0_7
    if pixel_format in (GxPixelFormatEntry.MONO8,
                        GxPixelFormatEntry.BAYER_GR8, GxPixelFormatEntry.BAYER_RG8,
                        GxPixelFormatEntry.BAYER_GB8, GxPixelFormatEntry.BAYER_BG8,
                        GxPixelFormatEntry.RGB8, GxPixelFormatEntry.BGR8,
                        GxPixelFormatEntry.R8, GxPixelFormatEntry.B8, GxPixelFormatEntry.G8):
        valid_bits = DxValidBit.BIT0_7
    elif pixel_format in (GxPixelFormatEntry.MONO10, GxPixelFormatEntry.MONO10_PACKED, GxPixelFormatEntry.MONO10_P,
                          GxPixelFormatEntry.BAYER_GR10, GxPixelFormatEntry.BAYER_RG10,
                          GxPixelFormatEntry.BAYER_GB10, GxPixelFormatEntry.BAYER_BG10,
                          GxPixelFormatEntry.BAYER_GR10_P, GxPixelFormatEntry.BAYER_RG10_P,
                          GxPixelFormatEntry.BAYER_GB10_P, GxPixelFormatEntry.BAYER_BG10_P,
                          GxPixelFormatEntry.BAYER_GR10_PACKED, GxPixelFormatEntry.BAYER_RG10_PACKED,
                          GxPixelFormatEntry.BAYER_GB10_PACKED, GxPixelFormatEntry.BAYER_BG10_PACKED):
        valid_bits = DxValidBit.BIT2_9
    elif pixel_format in (GxPixelFormatEntry.MONO12, GxPixelFormatEntry.MONO12_PACKED, GxPixelFormatEntry.MONO12_P,
                          GxPixelFormatEntry.BAYER_GR12, GxPixelFormatEntry.BAYER_RG12,
                          GxPixelFormatEntry.BAYER_GB12, GxPixelFormatEntry.BAYER_BG12,
                          GxPixelFormatEntry.BAYER_GR12_P, GxPixelFormatEntry.BAYER_RG12_P,
                          GxPixelFormatEntry.BAYER_GB12_P, GxPixelFormatEntry.BAYER_BG12_P,
                          GxPixelFormatEntry.BAYER_GR12_PACKED, GxPixelFormatEntry.BAYER_RG12_PACKED,
                          GxPixelFormatEntry.BAYER_GB12_PACKED, GxPixelFormatEntry.BAYER_BG12_PACKED):
        valid_bits = DxValidBit.BIT4_11
    elif pixel_format in (GxPixelFormatEntry.MONO14, GxPixelFormatEntry.MONO14_P,
                          GxPixelFormatEntry.BAYER_GR14, GxPixelFormatEntry.BAYER_RG14,
                          GxPixelFormatEntry.BAYER_GB14, GxPixelFormatEntry.BAYER_BG14,
                          GxPixelFormatEntry.BAYER_GR14_P, GxPixelFormatEntry.BAYER_RG14_P,
                          GxPixelFormatEntry.BAYER_GB14_P, GxPixelFormatEntry.BAYER_BG14_P,
                          ):
        valid_bits = DxValidBit.BIT6_13
    elif pixel_format in (GxPixelFormatEntry.MONO16,
                          GxPixelFormatEntry.BAYER_GR16, GxPixelFormatEntry.BAYER_RG16,
                          GxPixelFormatEntry.BAYER_GB16, GxPixelFormatEntry.BAYER_BG16):
        valid_bits = DxValidBit.BIT8_15
    return valid_bits


def convert_to_RGB(raw_image):
    if raw_image is None:
        return None
        
    with image_convert_lock:
        try:
            image_convert.set_dest_format(GxPixelFormatEntry.RGB8)
            valid_bits = get_best_valid_bits(raw_image.get_pixel_format())
            image_convert.set_valid_bits(valid_bits)

            # create out put image buffer
            buffer_out_size = image_convert.get_buffer_size_for_conversion(raw_image)
            output_image_array = (c_ubyte * buffer_out_size)()
            output_image = addressof(output_image_array)

            # convert to rgb
            image_convert.convert(raw_image, output_image, buffer_out_size, False)
            if output_image is None:
                print('Failed to convert RawImage to RGBImage')
                return None

            return output_image_array, buffer_out_size
        except Exception as e:
            print(f"Error in convert_to_RGB: {e}")
            return None

def convert_to_special_pixel_format(raw_image, pixel_format):
    if raw_image is None:
        return None
        
    with image_convert_lock:
        try:
            image_convert.set_dest_format(pixel_format)
            valid_bits = get_best_valid_bits(raw_image.get_pixel_format())
            image_convert.set_valid_bits(valid_bits)

            # create out put image buffer
            buffer_out_size = image_convert.get_buffer_size_for_conversion(raw_image)
            output_image_array = (c_ubyte * buffer_out_size)()
            output_image = addressof(output_image_array)

            # convert to pixel_format
            image_convert.convert(raw_image, output_image, buffer_out_size, False)
            if output_image is None:
                print('Pixel format conversion failed')
                return None

            return output_image_array, buffer_out_size
        except Exception as e:
            print(f"Error in convert_to_special_pixel_format: {e}")
            return None


def convert_image(raw_image, color_mode):
    """
    Convert a raw image to the appropriate format based on color mode.
    
    Args:
        raw_image: The raw image data
        color_mode: The target color mode
        
    Returns:
        numpy.ndarray: Converted image as numpy array or None if conversion fails
    """
    if raw_image is None:
        print("Getting image failed.")
        return None
    
    try:
        if ImageUtility.is_gray(color_mode) is False:
            # get RGB image from raw image
            if raw_image.get_pixel_format() != GxPixelFormatEntry.RGB8:
                result = convert_to_RGB(raw_image)
                if result is None:
                    print("RGB conversion failed")
                    return None
                
                rgb_image_array, rgb_image_buffer_length = result
                
                # Convert image buffer to bytes for numpy
                buffer_from_memory = ctypes.cast(
                    rgb_image_array, 
                    ctypes.POINTER(ctypes.c_ubyte * rgb_image_buffer_length)
                ).contents
                
                # Create numpy array from buffer
                numpy_image = numpy.array(buffer_from_memory, dtype=numpy.ubyte).reshape(
                    raw_image.frame_data.height, raw_image.frame_data.width, 3
                )
            else:
                numpy_image = raw_image.get_numpy_array()
        else:
            if raw_image.get_pixel_format() not in (
            GxPixelFormatEntry.MONO8, GxPixelFormatEntry.R8, GxPixelFormatEntry.B8, GxPixelFormatEntry.G8):
                result = convert_to_special_pixel_format(raw_image, GxPixelFormatEntry.MONO8)
                if result is None:
                    print("Mono conversion failed")
                    return None
                    
                mono_image_array, mono_image_buffer_length = result
                
                # Convert image buffer to bytes for numpy
                buffer_from_memory = ctypes.cast(
                    mono_image_array, 
                    ctypes.POINTER(ctypes.c_ubyte * mono_image_buffer_length)
                ).contents
                
                # Create numpy array from buffer
                numpy_image = numpy.array(buffer_from_memory, dtype=numpy.ubyte).reshape(
                    raw_image.frame_data.height, raw_image.frame_data.width
                )
            else:
                numpy_image = raw_image.get_numpy_array()

        return numpy_image
    except Exception as e:
        print(f"Error in convert_image: {e}")
        traceback.print_exc()
        return None

