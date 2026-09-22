"""Hardware-independent orchestration for reliable focus-stack capture."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import cv2


LOGGER = logging.getLogger(__name__)


class StackCaptureError(RuntimeError):
    """A required stack frame could not be captured or saved."""


class StackCaptureCancelled(StackCaptureError):
    """The user cancelled stack capture before the sequence completed."""


@dataclass(frozen=True)
class DirectCaptureResult:
    """Outcome and diagnostics for a coordinated direct camera capture."""

    image: object | None
    reason: str = ""
    read_count: int = 0
    lock_wait_s: float = 0.0
    detail: str = ""


def wait_for_new_preview_frame(
    snapshot: Callable[[], tuple[int, object | None]],
    start_counter: int,
    *,
    discard_frames: int,
    timeout_s: float,
    stop_check: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> object | None:
    """Return only a preview frame produced after the caller's counter snapshot."""
    target_counter = start_counter + max(1, int(discard_frames) + 1)
    deadline = clock() + max(0.0, float(timeout_s))
    while clock() < deadline:
        if stop_check is not None and stop_check():
            return None
        current_counter, current_img = snapshot()
        if current_counter >= target_counter and current_img is not None:
            return current_img.copy()
        sleeper(0.02)
    if stop_check is not None and stop_check():
        return None
    current_counter, current_img = snapshot()
    if current_counter >= target_counter and current_img is not None:
        return current_img.copy()
    return None


def remaining_direct_discard_frames(
    start_counter: int,
    current_counter: int,
    discard_frames: int,
) -> int:
    """Do not repeat preview-frame discards when falling back to direct reads."""
    configured_discards = max(0, int(discard_frames))
    preview_frames = max(0, int(current_counter) - int(start_counter))
    return max(0, configured_discards - preview_frames)


def capture_direct_with_paused_preview(
    capture_lock,
    set_paused: Callable[[bool], None],
    capture_one: Callable[[float], object | None],
    *,
    discard_frames: int,
    max_attempts: int,
    timeout_s: float,
    stop_check: Callable[[], bool] | None = None,
    last_error: Callable[[], str] | None = None,
    publish_image: Callable[[object], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> DirectCaptureResult:
    """Pause preview and perform bounded direct reads without lock contention.

    ``timeout_s`` is a per-frame SDK timeout. Lock drainage and each required
    camera frame receive independent bounded budgets so a slow in-flight
    preview read cannot consume the time needed to discard transitional frames.
    """
    frame_timeout_s = max(0.0, float(timeout_s))
    started = clock()
    lock_deadline = started + frame_timeout_s
    set_paused(True)
    try:
        if stop_check is not None and stop_check():
            return DirectCaptureResult(None, reason="cancelled")
        acquired = False
        while not acquired:
            if stop_check is not None and stop_check():
                return DirectCaptureResult(
                    None, reason="cancelled", lock_wait_s=clock() - started
                )
            remaining = lock_deadline - clock()
            if remaining <= 0:
                return DirectCaptureResult(
                    None, reason="lock-timeout", lock_wait_s=clock() - started
                )
            acquired = capture_lock.acquire(timeout=min(0.05, remaining))
        try:
            lock_wait_s = clock() - started
            read_count = 0
            attempt_count = max(1, int(max_attempts))
            frames_required = max(0, int(discard_frames)) + 1
            maximum_reads = frames_required + attempt_count - 1
            read_deadline = clock() + frame_timeout_s * maximum_reads
            completed_frames = 0
            no_frame_attempts = 0
            image = None

            while completed_frames < frames_required:
                if stop_check is not None and stop_check():
                    return DirectCaptureResult(
                        None,
                        reason="cancelled",
                        read_count=read_count,
                        lock_wait_s=lock_wait_s,
                    )
                remaining_total = read_deadline - clock()
                if remaining_total <= 0:
                    return DirectCaptureResult(
                        None,
                        reason="capture-timeout",
                        read_count=read_count,
                        lock_wait_s=lock_wait_s,
                    )

                read_count += 1
                try:
                    image = capture_one(min(frame_timeout_s, remaining_total))
                except Exception as exc:
                    return DirectCaptureResult(
                        None,
                        reason="driver-exception",
                        read_count=read_count,
                        lock_wait_s=lock_wait_s,
                        detail=str(exc),
                    )

                if image is None:
                    no_frame_attempts += 1
                    if no_frame_attempts >= attempt_count:
                        detail = str(
                            last_error() if last_error is not None else ""
                        ).strip()
                        return DirectCaptureResult(
                            None,
                            reason="driver-no-frame",
                            read_count=read_count,
                            lock_wait_s=lock_wait_s,
                            detail=detail,
                        )
                    remaining_total = read_deadline - clock()
                    if remaining_total <= 0:
                        return DirectCaptureResult(
                            None,
                            reason="capture-timeout",
                            read_count=read_count,
                            lock_wait_s=lock_wait_s,
                        )
                    sleeper(min(0.05, remaining_total))
                    continue

                completed_frames += 1

            if publish_image is not None:
                try:
                    publish_image(image)
                except Exception as exc:
                    return DirectCaptureResult(
                        None,
                        reason="publish-exception",
                        read_count=read_count,
                        lock_wait_s=lock_wait_s,
                        detail=str(exc),
                    )
            return DirectCaptureResult(
                image,
                read_count=read_count,
                lock_wait_s=lock_wait_s,
            )
        finally:
            capture_lock.release()
    finally:
        set_paused(False)


def capture_fresh_stack_frame(
    camera,
    camera_type: str,
    destination: Path | str,
    *,
    discard_frames: int,
    max_attempts: int,
    timeout_s: float,
    after_counter: int | None = None,
    stop_check: Callable[[], bool] | None = None,
):
    """Capture and immediately save one fresh frame without content heuristics."""
    get_fresh_image = getattr(camera, "get_fresh_image", None)
    if not callable(get_fresh_image):
        raise StackCaptureError("The active camera does not support fresh-frame capture")

    try:
        capture_options = dict(
            discard_frames=discard_frames,
            max_attempts=max_attempts,
            timeout_s=timeout_s,
            stop_check=stop_check,
        )
        if after_counter is not None:
            capture_options["after_counter"] = after_counter
        frame = get_fresh_image(**capture_options)
    except Exception as exc:
        raise StackCaptureError(f"Camera fresh-frame capture failed: {exc}") from exc

    if frame is None:
        if stop_check is not None and stop_check():
            raise StackCaptureCancelled("Stack capture was cancelled")
        diagnostic = str(getattr(camera, "last_fresh_capture_error", "") or "").strip()
        detail = f": {diagnostic}" if diagnostic else ""
        raise StackCaptureError(f"The camera did not return a fresh frame{detail}")
    if not hasattr(frame, "shape") or not hasattr(frame, "size") or frame.size == 0:
        raise StackCaptureError("The camera returned an invalid frame buffer")

    saved_frame = frame.copy()
    if camera_type == "VAImagingCamera":
        if len(saved_frame.shape) != 3 or saved_frame.shape[2] != 3:
            raise StackCaptureError("The VAImaging camera returned an invalid color frame")
        saved_frame = cv2.cvtColor(saved_frame, cv2.COLOR_RGB2BGR)

    destination = Path(destination)
    try:
        saved = cv2.imwrite(str(destination), saved_frame)
    except Exception as exc:
        raise StackCaptureError(f"Could not save stack frame {destination.name}: {exc}") from exc
    if not saved:
        raise StackCaptureError(f"Could not save stack frame {destination.name}")

    source = str(getattr(camera, "last_fresh_capture_source", "") or "camera")
    LOGGER.info("Saved fresh stack frame: %s (source=%s)", destination, source)
    return saved_frame


def capture_stack_sequence(
    camera,
    camera_type: str,
    destinations: Iterable[Path | str],
    *,
    move_next: Callable[[], None],
    restore_distance: Callable[[float], None],
    step_distance: float,
    settle_delay: float,
    discard_frames: int,
    max_attempts: int,
    timeout_s: float,
    stop_check: Callable[[], bool],
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Capture a complete stack and restore exactly the distance actually moved."""
    paths = [Path(path) for path in destinations]
    moved_distance = 0.0
    captured = 0
    restore_interrupted = False
    try:
        for step_index, destination in enumerate(paths):
            if stop_check():
                raise StackCaptureCancelled("Stack capture was cancelled")
            get_frame_counter = getattr(camera, "get_frame_counter", None)
            after_counter = get_frame_counter() if callable(get_frame_counter) else None
            sleeper(max(0.0, settle_delay))
            LOGGER.info(
                "Requesting stack frame %s/%s after preview counter %s",
                step_index + 1,
                len(paths),
                after_counter if after_counter is not None else "unavailable",
            )
            capture_fresh_stack_frame(
                camera,
                camera_type,
                destination,
                discard_frames=discard_frames,
                max_attempts=max_attempts,
                timeout_s=timeout_s,
                after_counter=after_counter,
                stop_check=stop_check,
            )
            captured += 1
            LOGGER.info(
                "Captured stack frame %s/%s after settling",
                step_index + 1,
                len(paths),
            )
            if stop_check():
                raise StackCaptureCancelled("Stack capture was cancelled")
            move_next()
            moved_distance += step_distance
            if stop_check():
                raise StackCaptureCancelled(
                    "Stack capture was cancelled during stage movement"
                )
    finally:
        if moved_distance and not stop_check():
            restore_distance(moved_distance)
            restore_interrupted = stop_check()
    if restore_interrupted:
        raise StackCaptureCancelled(
            "Stack capture was cancelled while returning the stage"
        )
    return captured
