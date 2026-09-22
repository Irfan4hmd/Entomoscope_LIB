import os
import logging
import threading
import traceback
import atexit
from PIL import Image
from refiners.solutions import BoxSegmenter
import numpy as np
from pathlib import Path
from .data_util import create_content_dataframe, scan_folders, pathify
from rich.progress import Progress, TextColumn, BarColumn, TimeRemainingColumn, TaskProgressColumn

logger = logging.getLogger(__name__)

# Thread-local segmenter cache — model is initialized once per thread and reused
_thread_local = threading.local()

def _get_segmenter(device="cpu"):
    """Get or create a cached BoxSegmenter instance for the current thread."""
    if not hasattr(_thread_local, 'segmenter') or _thread_local.segmenter is None or getattr(_thread_local, 'device', None) != device:
        logger.debug(f"Initializing BoxSegmenter on device={device}")
        _thread_local.segmenter = BoxSegmenter(device=device)
        _thread_local.device = device
    return _thread_local.segmenter

def cleanup_segmenter():
    """Clean up the cached segmenter. Called at application exit."""
    if hasattr(_thread_local, 'segmenter') and _thread_local.segmenter is not None:
        logger.debug("Cleaning up cached BoxSegmenter")
        _thread_local.segmenter = None

atexit.register(cleanup_segmenter)

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
        tuple: (mask, original_image) where mask is the segmentation mask.
    """
    try:
        # Load the image
        img = Image.open(image_path).convert("RGB")
        logger.debug(f"Image loaded: {image_path}, size: {img.size}, mode: {img.mode}")

        # Get cached segmenter (initialized once, reused across calls)
        segmenter = _get_segmenter(device)

        # Run the segmenter on the image
        mask = segmenter(img, box_prompt)

        return mask, img
    except Exception as e:
        logger.error(f"Error in get_backgound_mask: {e}")
        traceback.print_exc()
        return None, None

def save_image(image: Image.Image, 
               input_path,
               output_path: str | Path, 
               save_alpha: bool = True ) -> None:
    """
    Save the image to a specified path.

    Args:
        image (Image.Image): The image to save.
        output_path (str | pathlib.Path): The path where the image will be saved.
        save_alpha (bool): If True, save the image with an alpha channel (transparency and PNG format).
    """

    orig = Path(input_path)
    stem = orig.stem
    ext = orig.suffix or ".png"

    out = Path(output_path)
    if out.is_dir() or out.suffix == "":
        output_dir = out
        output_dir.mkdir(parents=True, exist_ok=True)

        output_file = output_dir / f"{stem}_uniform{ext}"
    else:
        output_file = out
        output_file.parent.mkdir(parents=True, exist_ok=True)
        if output_file.stem == stem:
            output_file = output_file.with_name(f"{stem}_uniform{output_file.suffix}")

    if not save_alpha:
        # ensure the output path is a PNG file
        if output_path.suffix != ".png":
            output_path = output_path.with_suffix(".png")
    else:
        # remove alpha channel
        image = image.convert("RGB")

    image.save(output_file)
    return output_file

def remove_backgound(image_path: str, 
                     box_prompt: tuple[int, int, int, int] | None = None, 
                     device: str = "cpu", 
                     replace: tuple[int, int, int] | Image.Image | str | Path | None = (120, 125, 135),
                     output_path: str| Path | None = None,
                     save_img_here: bool = False,
                     load_as_alpha_image: bool = False) -> tuple[Image.Image, Image.Image]:
    """
    Remove the background of an image and save the result.

    Args:
        image_path (str): The path to the input image.
        box_prompt (tuple[int, int, int, int] | None): Optional bounding box coordinates (x0, y0, x1, y1) for segmentation.
        device (str): The device to use for processing ('cpu' or 'cuda').
        replace (tuple[int, int, int] | None): Optional RGB tuple for background replacement.
        output_path (str): The path where the output image will be saved.
        save_img_here (bool): If True, save the image in the same directory as the input image if no output path is provided.
        load_as_alpha_image (bool): If True, load the image as an alpha image (RGBA) instead of using the segmenter.
    """

    # check if the image should be cut out by finegrain or can be loaded as alpha channel image
    if not load_as_alpha_image:
        try:
            # Get the segmented image
            mask, original_image = get_backgound_mask(image_path, box_prompt, device)
        except Exception as e:
            print(f"[error] remove_backgound: get mask failed{e}")
            return None
        
        try:
            # cut out mask in original image
            cutout_image = original_image.convert("RGBA")
            cutout_image.putalpha(mask)

        except Exception as e:
            print(f"[error] remove_backgound: RGBA conversion failed {e}")
            return None
    else:
        try:
            # Load the image as an alpha image
            original_image = Image.open(image_path).convert("RGBA")
            cutout_image = original_image.copy()
            mask = original_image.split()[-1]  # Get the alpha channel as the mask
        except Exception as e:
            print(f"[error] remove_backgound: loading image as alpha image failed {e}")
            return None
    try:
        if replace is None:
            # Use default gray background (120, 125, 135)
            background = Image.new("RGBA", original_image.size, (120, 125, 135))
            output_image = Image.alpha_composite(background, cutout_image)
        elif isinstance(replace, tuple) and len(replace) == 3:
            # Replace the background with the specified color
            background = Image.new("RGBA", original_image.size, replace)
            output_image = Image.alpha_composite(background, cutout_image)
        elif isinstance(replace, Image.Image):
            # Replace the background with the specified image
            if replace.size != original_image.size:
                replace = replace.resize(original_image.size)
            output_image = Image.alpha_composite(replace.convert("RGBA"), cutout_image)
        elif isinstance(replace, str) or isinstance(replace, Path):
            try:
                # Replace the background with the specified image path
                replace_image = Image.open(replace).convert("RGBA")
                if replace_image.size != original_image.size:
                    replace_image = replace_image.resize(original_image.size)
                output_image = Image.alpha_composite(replace_image, cutout_image)
            except Exception as e:
                print(f"[error] remove_backgound: loading replace image failed {e}")
                return None
        else:
            raise ValueError("replace must be a tuple of RGB values or Pillow Image or None")
    except Exception as e:
        print(f"[error] remove_backgound: background replacement failed {e}")
        return None

    # Save the segmented image
    if output_path is None:
        if save_img_here:
            # Use original filename with "_uniform" suffix instead of "_segmented"
            base_name = Path(image_path).stem
            output_path = os.path.join(os.path.dirname(image_path), f"{base_name}.png")
            output_img_file = save_image(output_image, image_path,output_path)
    else:
        output_img_file = save_image(output_image, image_path, output_path)
    
    #return mask, output_image, output_img_file
    return output_img_file

def remove_background_dataset(dataset_root: str | Path, 
                              output_root: str | Path,
                              device: str = "cpu", 
                              replace: tuple[int, int, int] | Image.Image | None = (120, 125, 135),
                              extensions: tuple | list | None = ['.jpg', '.png', '.jpeg', '.tif', '.tiff'],
                              load_as_alpha_image: bool = False) -> None:
    """
    Remove the background of all images in a dataset.

    Args:
        dataset_root (str): The root folder containing the dataset.
        output_root (str): The folder where the processed images will be saved, in the same structure as the dataset root.
        device (str): The device to use for processing ('cpu' or 'cuda').
        replace (tuple[int, int, int] | Image.Image | None): Optional RGB tuple or image for background replacement.
        extensions (tuple | list | None): A list of file extensions to search for (e.g., ['.tiff', '.jpg']).
        load_as_alpha_image (bool): If True, load the image as an alpha image (RGBA) instead of using the segmenter.
    """
    
    images_df = create_content_dataframe(scan_folders(dataset_root, extensions, abs_paths=True))
    image_paths = pathify(images_df['filepath'].tolist())

    # adding progress bar to evaluate lenghy processes
    with Progress(
            TextColumn("[cyan]{task.description}"),  # Task description
            BarColumn(),  # Progress bar
            TaskProgressColumn(),  # Percentage and progress
            TimeRemainingColumn(),  # Estimated time remaining
            TextColumn("[green]Processed: {task.completed}/{task.total}  {task.fields[image_name]}"),  # Custom text showing processed/total
        ) as progress:
            task = progress.add_task("Processing Images  ", total=len(image_paths), image_name="")
            for image_path in image_paths:
                save_dir = Path(output_root) / image_path.relative_to(dataset_root).parent
                save_file = save_dir / image_path.name

                # Perform background removal
                mask, output_image = remove_backgound(image_path, output_path=save_file, device=device, replace=replace, load_as_alpha_image=load_as_alpha_image)

                # Update progress bar
                progress.update(task, advance=1, image_name=image_path.name)

if __name__ == "__main__":
    pass
