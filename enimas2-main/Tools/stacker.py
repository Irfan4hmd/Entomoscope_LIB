
import os
import cv2
import numpy
import constants
import subprocess
from logging import getLogger, basicConfig, INFO, DEBUG
from threading import Thread
from UserInterface import method_selection
from PyQt5.QtCore import pyqtBoundSignal
from Tools.image_output import image_write_params

basicConfig(level=DEBUG)
logger = getLogger("stacker")
logger.setLevel(DEBUG)


def fuse_stacks(
    dir_path,
    img_name,
    fuse_status_signal,
    stacking_done_signal,
    stacking_failed_signal=None,
):
    fuse_status_signal.emit("Fusing stack...")
    
    if constants.HELICON_PATH == "undefined":
        # The user hat not yet decided between helicon and selfmade
        method_selection.prompt_user_fuse_mode()

    helicon_path = constants.HELICON_PATH
        
    logger.debug(f"{helicon_path=}")
    
    if helicon_path == "NoStack":
        # User chose "Do not stack" - just save images without stacking
        logger.info("Skipping stacking process - user selected 'Do not stack'")
        fuse_status_signal.emit("Images saved. Stacking skipped.")
        if stacking_done_signal:
            stacking_done_signal.emit()
        return None
    elif helicon_path == "None":
        # Fuse stacks with selfmade version
        stacker = Stacker(
            dir_path,
            img_name,
            fuse_status_signal,
            stacking_done_signal,
            stacking_failed_signal,
        )
        stacker.start()
        return stacker
    else:
        # Fuse stacks with Helicon Focus
        stacker = HeliconStacker(
            helicon_path,
            dir_path,
            img_name,
            fuse_status_signal,
            stacking_done_signal,
            stacking_failed_signal,
        )
        stacker.start()
        return stacker

class HeliconStacker(Thread):
    def __init__(
        self,
        helicon_path: str,
        dir_path: str,
        stacked_img_name: str,
        fuse_status_signal,
        stacking_done_signal,
        stacking_failed_signal=None,
    ) -> None:
        super().__init__()
        logger.info("fuze with helicon")
    
        self.helicon_path = helicon_path
        self.dir_path = dir_path
        self.stacked_img_name = stacked_img_name
        self.fuse_status_signal = fuse_status_signal
        self.stacking_done_signal = stacking_done_signal
        self.stacking_failed_signal = stacking_failed_signal

    def run(self):
        logger.info("fuze with helicon")
        self.fuse_status_signal.emit("Fusing stack with Helicon focus...")

        try:
            command = [
                self.helicon_path,
                "-silent",
                self.dir_path,
                f"-save:{self.stacked_img_name}",
            ]
            if os.path.splitext(self.stacked_img_name)[1].lower() in {".jpg", ".jpeg"}:
                command.append(f"-j:{constants.STACK_JPEG_QUALITY}")
            logger.debug("Helicon command: %r", command)
            result = subprocess.run(command, shell=False, encoding="utf-8")
            if result.returncode != 0 or not os.path.isfile(self.stacked_img_name):
                raise RuntimeError("Helicon Focus did not create the stacked image")
            self.fuse_status_signal.emit("Fusing stack finished.")

            if self.stacking_done_signal:
                self.stacking_done_signal.emit()
        except Exception as exc:
            logger.exception("Helicon Focus stacking failed")
            message = f"{exc}. The source frames were kept."
            self.fuse_status_signal.emit(message)
            if self.stacking_failed_signal:
                self.stacking_failed_signal.emit(message)


class Stacker(Thread):
    _BLUR_SIZE = 1
    _SIGMAX = 0
    _KERNEL_SIZE = 7
    _IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff"}
    
    def __init__(
        self,
        raw_img_dir_path: str,
        stacked_img_name: str,
        fuse_status_signal: pyqtBoundSignal,
        stacking_done_signal: pyqtBoundSignal,
        stacking_failed_signal: pyqtBoundSignal | None = None,
    ) -> None:
        super().__init__()
        logger.debug("fuze selfmade init")
        
        self.raw_img_dir_path = raw_img_dir_path
        self.stacked_img_name = stacked_img_name
        self.fuse_status_signal = fuse_status_signal
        self.stacking_done_signal = stacking_done_signal
        self.stacking_failed_signal = stacking_failed_signal
        
    def run(self):
        try:
            logger.debug("stacking running")
            self.fuse_status_signal.emit("Preparing to fuse stacks...")

            image_paths = [
                os.path.join(self.raw_img_dir_path, img_name)
                for img_name in sorted(os.listdir(self.raw_img_dir_path))
                if (
                    os.path.isfile(os.path.join(self.raw_img_dir_path, img_name))
                    and os.path.splitext(img_name)[1].lower() in self._IMAGE_EXTENSIONS
                )
            ]
            if not image_paths:
                raise RuntimeError("No stack image files were found")

            stacked_img = self.stack_image_paths(image_paths)

            if not cv2.imwrite(
                self.stacked_img_name,
                stacked_img,
                image_write_params(
                    self.stacked_img_name,
                    jpeg_quality=constants.STACK_JPEG_QUALITY,
                ),
            ):
                raise RuntimeError("Could not save the stacked image")
            self.fuse_status_signal.emit("Fusing complete.")
            self.stacking_done_signal.emit()
            logger.debug("stacking complete")
        except Exception as exc:
            logger.exception("Built-in stacker failed")
            message = f"Stacking failed: {exc}. The source frames were kept."
            self.fuse_status_signal.emit(message)
            if self.stacking_failed_signal:
                self.stacking_failed_signal.emit(message)

    def stack_image_paths(self, image_paths: list[str]) -> numpy.ndarray:
        """Read, align, and fuse frames one at a time to bound peak memory."""
        def frames():
            for image_path in image_paths:
                image = cv2.imread(image_path)
                if image is None:
                    raise RuntimeError(
                        f"Could not read stack frame: {os.path.basename(image_path)}"
                    )
                yield os.path.basename(image_path), image

        return self._stack_labeled_frames(frames())

    def stack_images(self, raw_img_list: list) -> numpy.ndarray:
        """Align and fuse in-memory frames without retaining aligned copies."""
        return self._stack_labeled_frames(
            (f"frame {index}", image)
            for index, image in enumerate(raw_img_list, start=1)
        )

    def _stack_labeled_frames(self, labeled_frames) -> numpy.ndarray:
        iterator = iter(labeled_frames)
        try:
            first_label, first_color = next(iterator)
        except StopIteration as exc:
            raise RuntimeError("No stack frames were supplied") from exc

        self._validate_color_frame(first_color, None, first_label)
        expected_shape = first_color.shape
        previous_gray = cv2.cvtColor(first_color, cv2.COLOR_BGR2GRAY)
        output = first_color.copy()
        best_focus = self._focus_response(previous_gray)

        self.fuse_status_signal.emit("Aligning stack images...")
        self.fuse_status_signal.emit("Fusing stacks...")
        for frame_number, (label, color_image) in enumerate(iterator, start=2):
            self._validate_color_frame(color_image, expected_shape, label)
            gray_image = cv2.cvtColor(color_image, cv2.COLOR_BGR2GRAY)
            aligned_color, aligned_gray = self._align_frame(
                previous_gray,
                color_image,
                gray_image,
                frame_number,
            )
            focus_response = self._focus_response(aligned_gray)
            output, best_focus = self._merge_focus_frame(
                output,
                best_focus,
                aligned_color,
                focus_response,
            )
            previous_gray = aligned_gray
            self.fuse_status_signal.emit("Fusing stacks...")

        return output
        
    def align_images(self, raw_img_list: list) -> tuple[list, list]:
        """Apply each ECC translation to both the grayscale and color frame."""
        if not raw_img_list:
            raise RuntimeError("No stack frames were supplied")

        expected_shape = raw_img_list[0].shape
        for index, image in enumerate(raw_img_list, start=1):
            self._validate_color_frame(image, expected_shape, f"frame {index}")

        gray_img_list = [
            cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            for image in raw_img_list
        ]
        aligned_color = [raw_img_list[0].copy()]
        aligned_gray = [gray_img_list[0].copy()]
        for i, (color_img, gray_img) in enumerate(
            zip(raw_img_list[1:], gray_img_list[1:])
        ):
            next_color, next_gray = self._align_frame(
                aligned_gray[i],
                color_img,
                gray_img,
                i + 2,
            )
            aligned_color.append(next_color)
            aligned_gray.append(next_gray)

        return aligned_color, aligned_gray

    @staticmethod
    def _validate_color_frame(image, expected_shape, label) -> None:
        if image is None or not hasattr(image, "shape"):
            raise RuntimeError(f"Invalid stack frame: {label}")
        if image.ndim != 3 or image.shape[2] != 3:
            raise RuntimeError(f"Stack frame is not a color image: {label}")
        if expected_shape is not None and image.shape != expected_shape:
            raise RuntimeError("All stack frames must have the same dimensions")

    def _align_frame(
        self,
        reference_gray,
        color_image,
        gray_image,
        frame_number,
    ) -> tuple[numpy.ndarray, numpy.ndarray]:
        logger.debug("aligning img %s", frame_number)
        self.fuse_status_signal.emit(f"Aligning stack image {frame_number}...")
        height, width = reference_gray.shape
        warp_matrix = numpy.eye(2, 3, dtype=numpy.float32)
        criteria = (
            cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
            5000,
            1e-10,
        )

        try:
            _, warp_matrix = cv2.findTransformECC(
                reference_gray,
                gray_image,
                warp_matrix,
                cv2.MOTION_TRANSLATION,
                criteria,
            )
        except cv2.error as exc:
            raise RuntimeError(
                f"Could not align stack frame {frame_number}"
            ) from exc

        warp_flags = cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP
        warp_size = (width, height)
        aligned_gray = cv2.warpAffine(
            gray_image,
            warp_matrix,
            warp_size,
            flags=warp_flags,
            borderMode=cv2.BORDER_REFLECT,
        )
        aligned_color = cv2.warpAffine(
            color_image,
            warp_matrix,
            warp_size,
            flags=warp_flags,
            borderMode=cv2.BORDER_REFLECT,
        )
        return aligned_color, aligned_gray
        
    def focus_stack(self, images, gray_images):
        """
        Find the sharpest area of each of the superimposed images 
        and generates an image from the different sharp areas.
        """

        if not images or len(images) != len(gray_images):
            raise RuntimeError("Color and grayscale stack frames must correspond")

        output = images[0].copy()
        best_focus = self._focus_response(gray_images[0])
        for image, gray_image in zip(images[1:], gray_images[1:]):
            self.fuse_status_signal.emit("Fusing stacks...")
            output, best_focus = self._merge_focus_frame(
                output,
                best_focus,
                image,
                self._focus_response(gray_image),
            )

        return output

    def _focus_response(self, gray_image) -> numpy.ndarray:
        blurred = cv2.GaussianBlur(
            gray_image,
            (self._BLUR_SIZE, self._BLUR_SIZE),
            self._SIGMAX,
        )
        return numpy.absolute(
            cv2.Laplacian(blurred, cv2.CV_64F, ksize=self._KERNEL_SIZE)
        )

    @staticmethod
    def _merge_focus_frame(
        output,
        best_focus,
        image,
        focus_response,
    ) -> tuple[numpy.ndarray, numpy.ndarray]:
        # The legacy implementation selected the later frame on an exact
        # focus-score tie, so use >= to preserve that behavior.
        replace = focus_response >= best_focus
        output[replace] = image[replace]
        best_focus[replace] = focus_response[replace]
        return output, best_focus
