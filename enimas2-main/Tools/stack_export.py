"""Safe discovery and export of final ENIMAS stacked images."""

from __future__ import annotations

import csv
import hashlib
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from Tools.image_output import SUPPORTED_IMAGE_EXTENSIONS, convert_image_file


_FINAL_STACK_PATTERN = re.compile(r"_stacked_\d+$", re.IGNORECASE)


class StackExportError(RuntimeError):
    """The stacked-image export could not be prepared safely."""


@dataclass
class ExportRecord:
    source: str
    exported: str
    status: str
    message: str = ""


@dataclass
class ExportSummary:
    discovered: int
    exported: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: bool = False
    manifest_path: Path | None = None
    records: list[ExportRecord] = field(default_factory=list)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def is_final_stacked_image(path: Path | str) -> bool:
    path = Path(path)
    return path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS and bool(_FINAL_STACK_PATTERN.search(path.stem))


def discover_stacked_images(
    source_root: Path | str,
    *,
    recursive: bool = True,
    exclude_root: Path | str | None = None,
) -> list[Path]:
    """Find final stack outputs without descending into raw Stack_Frames folders."""
    source_root = Path(source_root).expanduser()
    if not source_root.is_dir():
        raise StackExportError(f"Source folder does not exist: {source_root}")
    source_root = source_root.resolve()
    excluded = Path(exclude_root).resolve() if exclude_root else None
    results = []

    for current, directories, files in os.walk(source_root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            directory
            for directory in directories
            if "_stack_frames_" not in directory.lower()
            and not (current_path / directory).is_symlink()
            and not (excluded and _is_within(current_path / directory, excluded))
        )
        for filename in sorted(files):
            path = current_path / filename
            if path.is_symlink() or (excluded and _is_within(path, excluded)):
                continue
            if is_final_stacked_image(path):
                results.append(path)
        if not recursive:
            break
    return results


def sanitize_site_id(site_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(site_id or "").strip())
    return cleaned.strip("._-")[:80]


def safe_export_stem(site_id: str, source_stem: str, *, max_length: int = 220) -> str:
    """Build a Windows-safe component with a stable hash when truncation is needed."""
    prefix = sanitize_site_id(site_id)
    combined = f"{prefix}__{source_stem}" if prefix else source_stem
    combined = combined.rstrip(" .") or "stacked_image"
    if len(combined) <= max_length:
        return combined
    suffix = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:10]
    return f"{combined[: max_length - len(suffix) - 2].rstrip(' .')}__{suffix}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identical_destination(candidate_file: Path, desired: Path) -> Path | None:
    """Return any existing base/numbered destination with identical bytes."""
    candidates = []
    if desired.is_file():
        candidates.append(desired)
    prefix = f"{desired.stem}__"
    try:
        siblings = sorted(desired.parent.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        siblings = []
    for sibling in siblings:
        if (
            sibling.is_file()
            and sibling.suffix.lower() == desired.suffix.lower()
            and sibling.stem.startswith(prefix)
            and sibling.stem[len(prefix):].isdigit()
        ):
            candidates.append(sibling)

    candidate_size = candidate_file.stat().st_size
    candidate_hash = None
    for existing in candidates:
        if existing.stat().st_size != candidate_size:
            continue
        if candidate_hash is None:
            candidate_hash = _sha256(candidate_file)
        if _sha256(existing) == candidate_hash:
            return existing
    return None


def _unique_destination(destination: Path) -> Path:
    if not destination.exists():
        return destination
    for index in range(2, 10000):
        candidate = destination.with_name(f"{destination.stem}__{index}{destination.suffix}")
        if not candidate.exists():
            return candidate
    raise StackExportError(f"Too many filename conflicts for {destination.name}")


def _write_manifest(destination_root: Path, records: Iterable[ExportRecord]) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    final_path = destination_root / f"enimas_export_manifest_{timestamp}.csv"
    temp_path = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.part")
    try:
        with temp_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(("source", "exported", "status", "message"))
            for record in records:
                writer.writerow((record.source, record.exported, record.status, record.message))
        os.replace(temp_path, final_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return final_path


def export_stacked_images(
    images: Iterable[Path | str],
    destination_root: Path | str,
    *,
    output_format: str = "keep",
    site_id: str = "",
    jpeg_quality: int = 95,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ExportSummary:
    """Export a fixed image list without overwriting any existing file."""
    image_paths = [Path(path).resolve() for path in images]
    destination_root = Path(destination_root).expanduser()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination_root = destination_root.resolve()
    if output_format not in {"keep", "jpeg"}:
        raise StackExportError(f"Unsupported export format: {output_format}")

    required = 0
    for path in image_paths:
        try:
            if path.is_file():
                required += path.stat().st_size
        except OSError:
            # The per-file loop will record a useful failure if a source was
            # removed or became unreadable after scanning.
            continue
    free = shutil.disk_usage(destination_root).free
    safety_margin = min(100 * 1024 * 1024, max(10 * 1024 * 1024, required // 10))
    if free < required + safety_margin:
        raise StackExportError("The destination does not have enough free disk space for this export")

    summary = ExportSummary(discovered=len(image_paths))
    total = len(image_paths)
    for index, source in enumerate(image_paths, start=1):
        if cancelled is not None and cancelled():
            summary.cancelled = True
            break
        if not source.is_file() or not is_final_stacked_image(source):
            summary.failed += 1
            summary.records.append(
                ExportRecord(
                    str(source),
                    "",
                    "failed",
                    "Source file is missing or is not a final stacked image",
                )
            )
            continue

        suffix = ".jpg" if output_format == "jpeg" else source.suffix.lower()
        stem_limit = 220
        if os.name == "nt":
            # Stay below the traditional MAX_PATH boundary even when long-path
            # support is disabled. Leave extra room for filesystem operations.
            stem_limit = min(stem_limit, 240 - len(str(destination_root)) - 1 - len(suffix))
            if stem_limit < 32:
                raise StackExportError(
                    "The destination folder path is too long for safe export on Windows"
                )
        base_name = safe_export_stem(site_id, source.stem, max_length=stem_limit)
        desired = destination_root / f"{base_name}{suffix}"
        temp = destination_root / f".enimas-export-{uuid.uuid4().hex}{suffix}"
        try:
            convert_image_file(source, temp, jpeg_quality=jpeg_quality)
            identical = _identical_destination(temp, desired)
            if identical is not None:
                temp.unlink()
                summary.skipped += 1
                summary.records.append(
                    ExportRecord(str(source), str(identical), "already_present")
                )
                final_path = identical
            else:
                final_path = _unique_destination(desired)
                os.replace(temp, final_path)
                summary.exported += 1
                summary.records.append(ExportRecord(str(source), str(final_path), "exported"))
        except Exception as exc:
            temp.unlink(missing_ok=True)
            summary.failed += 1
            summary.records.append(ExportRecord(str(source), "", "failed", str(exc)))
        if progress is not None:
            progress(index, total, source.name)

    summary.manifest_path = _write_manifest(destination_root, summary.records)
    return summary
