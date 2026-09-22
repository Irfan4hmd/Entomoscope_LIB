"""Image-output helpers shared by stacking, export, and scale-bar workflows."""

from __future__ import annotations

import math
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import cv2
from PIL import Image, ImageDraw, ImageFont


SUPPORTED_IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff"}
JPEG_EXTENSIONS = {".jpeg", ".jpg"}


class ImageOutputError(RuntimeError):
    """An image could not be converted or annotated safely."""


class ScaleBarError(ImageOutputError):
    """A calibrated scale bar could not be created."""


@dataclass(frozen=True)
class ScaleBarSpec:
    length_mm: float
    length_px: int
    label: str


def normalize_image_extension(value: str | None, *, default: str = "tiff") -> str:
    """Return a canonical extension without a leading dot."""
    normalized = str(value or default).strip().lower().lstrip(".")
    aliases = {"tif": "tiff", "jpeg": "jpg"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"tiff", "jpg"}:
        raise ValueError(f"Unsupported final image format: {value}")
    return normalized


def image_write_params(path: Path | str, jpeg_quality: int = 95) -> list[int]:
    if Path(path).suffix.lower() in JPEG_EXTENSIONS:
        quality = max(1, min(100, int(jpeg_quality)))
        return [cv2.IMWRITE_JPEG_QUALITY, quality]
    return []


def is_valid_scale(mm_per_pixel) -> bool:
    try:
        value = float(mm_per_pixel)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and value > 0


def calculate_mm_per_pixel(known_length_mm: float, measured_pixels: float) -> float:
    try:
        known = float(known_length_mm)
        pixels = float(measured_pixels)
    except (TypeError, ValueError) as exc:
        raise ScaleBarError("Calibration values must be numbers") from exc
    if not math.isfinite(known) or not math.isfinite(pixels) or known <= 0 or pixels <= 0:
        raise ScaleBarError("Calibration length and pixel distance must be greater than zero")
    return known / pixels


def choose_scale_bar(width_px: int, mm_per_pixel: float, target_fraction: float = 0.2) -> ScaleBarSpec:
    """Choose a readable 1/2/5 physical length near 20% of image width."""
    if width_px <= 0:
        raise ScaleBarError("Image width must be greater than zero")
    if not is_valid_scale(mm_per_pixel):
        raise ScaleBarError("The active camera and lens do not have a valid mm/pixel calibration")

    scale = float(mm_per_pixel)
    target_mm = width_px * scale * max(0.05, min(0.4, float(target_fraction)))
    exponent = math.floor(math.log10(target_mm))
    candidates = [
        factor * (10 ** power)
        for power in range(exponent - 2, exponent + 3)
        for factor in (1.0, 2.0, 5.0)
    ]
    valid = [
        length
        for length in candidates
        if 0.08 * width_px <= length / scale <= 0.32 * width_px
    ]
    pool = valid or candidates
    length_mm = min(pool, key=lambda value: abs(math.log(value / target_mm)))
    length_px = max(1, int(round(length_mm / scale)))
    label = _format_scale_label(length_mm)
    return ScaleBarSpec(length_mm=length_mm, length_px=length_px, label=label)


def _format_scale_label(length_mm: float) -> str:
    if length_mm >= 1:
        value = f"{length_mm:g}"
    elif length_mm >= 0.01:
        value = f"{length_mm:.3f}".rstrip("0").rstrip(".")
    else:
        value = f"{length_mm:.6f}".rstrip("0").rstrip(".")
    return f"{value} mm"


def _load_font(size: int):
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _atomic_image_save(image: Image.Image, destination: Path, *, jpeg_quality: int = 95, metadata=None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.parent / f".enimas-{uuid.uuid4().hex}.part"
    suffix = destination.suffix.lower()
    save_options = {}
    image_format = None
    if suffix in JPEG_EXTENSIONS:
        image_format = "JPEG"
        if image.mode != "RGB":
            background = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image.convert("RGB"))
            image = background
        save_options.update(quality=max(1, min(100, int(jpeg_quality))), subsampling=0, optimize=True)
    elif suffix in {".tif", ".tiff"}:
        image_format = "TIFF"
        save_options["compression"] = "tiff_lzw"
    elif suffix == ".png":
        image_format = "PNG"
        save_options["optimize"] = True
    else:
        raise ImageOutputError(f"Unsupported destination image format: {destination.suffix}")

    metadata = metadata or {}
    for key in ("icc_profile", "exif", "dpi"):
        if metadata.get(key):
            save_options[key] = metadata[key]
    try:
        image.save(temp_path, format=image_format, **save_options)
        os.replace(temp_path, destination)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def convert_image_file(source: Path | str, destination: Path | str, *, jpeg_quality: int = 95) -> Path:
    """Copy or convert an image through a same-directory atomic temporary file."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise ImageOutputError(f"Source image does not exist: {source}")
    if source.resolve() == destination.resolve():
        raise ImageOutputError("Source and destination image paths must be different")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == destination.suffix.lower():
        temp_path = destination.parent / f".enimas-{uuid.uuid4().hex}.part"
        try:
            shutil.copy2(source, temp_path)
            os.replace(temp_path, destination)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return destination

    try:
        with Image.open(source) as opened:
            opened.load()
            metadata = dict(opened.info)
            converted = opened.convert("RGB")
            _atomic_image_save(converted, destination, jpeg_quality=jpeg_quality, metadata=metadata)
    except Exception as exc:
        raise ImageOutputError(f"Could not convert {source.name}: {exc}") from exc
    return destination


def add_scale_bar(image_path: Path | str, mm_per_pixel: float, *, jpeg_quality: int = 95) -> ScaleBarSpec:
    """Embed an adaptive, physically labelled scale bar into an image."""
    path = Path(image_path)
    if not path.is_file():
        raise ScaleBarError(f"Image does not exist: {path}")
    if not is_valid_scale(mm_per_pixel):
        raise ScaleBarError("Set a valid mm/pixel calibration for the active camera and lens")

    try:
        with Image.open(path) as opened:
            opened.load()
            bits_per_sample = None
            if getattr(opened, "format", "") == "TIFF":
                bits_per_sample = getattr(opened, "tag_v2", {}).get(258)
            if isinstance(bits_per_sample, int):
                bit_depth = bits_per_sample
            elif bits_per_sample:
                bit_depth = max(int(value) for value in bits_per_sample)
            else:
                bit_depth = 8
            if opened.mode not in {"RGB", "RGBA", "L", "LA"} or bit_depth > 8:
                raise ScaleBarError(
                    f"Scale bars are not applied to {bit_depth}-bit {opened.mode} "
                    "images because that could reduce their bit depth. Export an "
                    "8-bit TIFF/JPEG copy or disable the scale bar to preserve the "
                    "original image."
                )
            metadata = dict(opened.info)
            canvas = opened.convert("RGBA")
    except ScaleBarError:
        raise
    except Exception as exc:
        raise ScaleBarError(f"Could not open image for scale bar: {exc}") from exc

    width, height = canvas.size
    spec = choose_scale_bar(width, float(mm_per_pixel))
    margin = max(18, int(round(min(width, height) * 0.012)))
    line_width = max(3, int(round(min(width, height) * 0.0025)))
    tick_height = max(12, int(round(height * 0.018)))
    font_size = max(18, min(96, int(round(width * 0.022))))
    font = _load_font(font_size)

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    text_bbox = draw.textbbox((0, 0), spec.label, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    content_width = max(spec.length_px, text_width)
    padding = max(10, line_width * 3)
    box_width = content_width + 2 * padding
    box_height = text_height + tick_height + line_width + 3 * padding
    left = max(margin, width - margin - box_width)
    top = max(margin, height - margin - box_height)
    right = min(width - margin, left + box_width)
    bottom = min(height - margin, top + box_height)
    radius = max(6, padding // 2)
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=radius,
        fill=(255, 255, 255, 220),
        outline=(0, 0, 0, 230),
        width=max(1, line_width // 2),
    )

    bar_left = left + (box_width - spec.length_px) // 2
    bar_right = bar_left + spec.length_px
    bar_y = bottom - padding - tick_height // 2
    draw.line((bar_left, bar_y, bar_right, bar_y), fill=(0, 0, 0, 255), width=line_width)
    draw.line((bar_left, bar_y - tick_height // 2, bar_left, bar_y + tick_height // 2), fill=(0, 0, 0, 255), width=line_width)
    draw.line((bar_right, bar_y - tick_height // 2, bar_right, bar_y + tick_height // 2), fill=(0, 0, 0, 255), width=line_width)
    text_x = left + (box_width - text_width) // 2
    text_y = top + padding - text_bbox[1]
    draw.text((text_x, text_y), spec.label, font=font, fill=(0, 0, 0, 255))

    result = Image.alpha_composite(canvas, overlay).convert("RGB")
    try:
        _atomic_image_save(result, path, jpeg_quality=jpeg_quality, metadata=metadata)
    except Exception as exc:
        raise ScaleBarError(f"Could not save scale bar: {exc}") from exc
    return spec
