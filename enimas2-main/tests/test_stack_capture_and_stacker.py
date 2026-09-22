import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy

from Tools.stack_capture import (
    StackCaptureError,
    capture_direct_with_paused_preview,
    capture_stack_sequence,
    wait_for_new_preview_frame,
)
from Tools.stacker import HeliconStacker, Stacker


class RecordingSignal:
    def __init__(self):
        self.values = []

    def emit(self, *values):
        self.values.append(values)


class FakeFreshCamera:
    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = []
        self.frame_counter = 0

    def get_frame_counter(self):
        return self.frame_counter

    def get_image(self):
        raise AssertionError("Cached get_image must not be used for stack capture")

    def get_fresh_image(self, **kwargs):
        self.calls.append(kwargs)
        if not self.frames:
            return None
        self.frame_counter += 1
        return self.frames.pop(0)


class DirectCaptureCoordinationTests(unittest.TestCase):
    def test_each_required_frame_gets_a_full_driver_timeout_budget(self):
        now = [0.0]
        budgets = []
        frames = [
            numpy.full((3, 3, 3), value, dtype=numpy.uint8)
            for value in (1, 2, 3)
        ]

        def capture_one(timeout_s):
            budgets.append(timeout_s)
            if timeout_s < 0.75:
                return None
            now[0] += 0.8
            return frames.pop(0)

        result = capture_direct_with_paused_preview(
            threading.Lock(),
            lambda _paused: None,
            capture_one,
            discard_frames=2,
            max_attempts=1,
            timeout_s=1,
            stop_check=lambda: False,
            clock=lambda: now[0],
            sleeper=lambda duration: now.__setitem__(0, now[0] + duration),
        )

        self.assertIsNotNone(result.image)
        self.assertEqual(int(result.image[0, 0, 0]), 3)
        self.assertEqual(result.read_count, 3)
        self.assertEqual(budgets, [1, 1, 1])

    def test_inflight_preview_lock_wait_does_not_consume_driver_read_budget(self):
        now = [0.0]
        budgets = []

        class DelayedLock:
            def acquire(self, timeout):
                now[0] += min(0.1, timeout)
                return now[0] >= 0.5

            def release(self):
                return None

        result = capture_direct_with_paused_preview(
            DelayedLock(),
            lambda _paused: None,
            lambda timeout_s: (
                budgets.append(timeout_s)
                or numpy.full((3, 3, 3), 7, dtype=numpy.uint8)
            ),
            discard_frames=0,
            max_attempts=1,
            timeout_s=1,
            stop_check=lambda: False,
            clock=lambda: now[0],
            sleeper=lambda duration: now.__setitem__(0, now[0] + duration),
        )

        self.assertIsNotNone(result.image)
        self.assertAlmostEqual(result.lock_wait_s, 0.5, delta=0.06)
        self.assertEqual(len(budgets), 1)
        self.assertAlmostEqual(budgets[0], 1.0, delta=1e-6)

    def test_direct_capture_pauses_preview_discards_frames_and_resumes(self):
        capture_lock = threading.Lock()
        pause_events = []
        frames = [
            numpy.full((3, 3, 3), value, dtype=numpy.uint8)
            for value in (1, 2, 3)
        ]

        result = capture_direct_with_paused_preview(
            capture_lock,
            pause_events.append,
            lambda _remaining: frames.pop(0),
            discard_frames=2,
            max_attempts=1,
            timeout_s=1,
            stop_check=lambda: False,
        )

        self.assertEqual(int(result.image[0, 0, 0]), 3)
        self.assertEqual(result.reason, "")
        self.assertEqual(result.read_count, 3)
        self.assertEqual(pause_events, [True, False])
        self.assertFalse(capture_lock.locked())

    def test_direct_capture_waits_for_inflight_preview_then_reads_exclusively(self):
        capture_lock = threading.Lock()
        preview_paused = threading.Event()
        preview_started = threading.Event()
        pause_events = []

        def finish_inflight_preview():
            with capture_lock:
                preview_started.set()
                preview_paused.wait(timeout=1)

        preview = threading.Thread(target=finish_inflight_preview)
        preview.start()
        self.assertTrue(preview_started.wait(timeout=1))

        def set_paused(paused):
            pause_events.append(paused)
            if paused:
                preview_paused.set()

        result = capture_direct_with_paused_preview(
            capture_lock,
            set_paused,
            lambda _remaining: numpy.full((3, 3, 3), 7, dtype=numpy.uint8),
            discard_frames=0,
            max_attempts=1,
            timeout_s=1,
            stop_check=lambda: False,
        )
        preview.join(timeout=1)

        self.assertFalse(preview.is_alive())
        self.assertEqual(int(result.image[0, 0, 0]), 7)
        self.assertEqual(pause_events, [True, False])
        self.assertFalse(capture_lock.locked())

    def test_direct_capture_publishes_before_preview_resumes(self):
        events = []

        result = capture_direct_with_paused_preview(
            threading.Lock(),
            lambda paused: events.append("pause" if paused else "resume"),
            lambda _remaining: numpy.full((3, 3, 3), 8, dtype=numpy.uint8),
            discard_frames=0,
            max_attempts=1,
            timeout_s=1,
            stop_check=lambda: False,
            publish_image=lambda _image: events.append("publish"),
        )

        self.assertIsNotNone(result.image)
        self.assertEqual(events, ["pause", "publish", "resume"])

    def test_direct_capture_classifies_driver_exception(self):
        def fail(_remaining):
            raise RuntimeError("SDK failure")

        result = capture_direct_with_paused_preview(
            threading.Lock(),
            lambda _paused: None,
            fail,
            discard_frames=0,
            max_attempts=1,
            timeout_s=1,
            stop_check=lambda: False,
        )

        self.assertIsNone(result.image)
        self.assertEqual(result.reason, "driver-exception")
        self.assertEqual(result.read_count, 1)
        self.assertEqual(result.detail, "SDK failure")
    def test_direct_capture_cannot_read_when_preview_lock_does_not_drain(self):
        capture_lock = threading.Lock()
        capture_lock.acquire()
        pause_events = []
        read = mock.Mock()
        try:
            result = capture_direct_with_paused_preview(
                capture_lock,
                pause_events.append,
                read,
                discard_frames=0,
                max_attempts=1,
                timeout_s=0,
                stop_check=lambda: False,
            )
        finally:
            capture_lock.release()

        self.assertIsNone(result.image)
        self.assertEqual(result.reason, "lock-timeout")
        read.assert_not_called()
        self.assertEqual(pause_events, [True, False])

    def test_direct_capture_checks_cancellation_while_waiting_for_lock(self):
        capture_lock = threading.Lock()
        capture_lock.acquire()
        cancelled = threading.Event()
        timer = threading.Timer(0.05, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            result = capture_direct_with_paused_preview(
                capture_lock,
                lambda _paused: None,
                mock.Mock(),
                discard_frames=0,
                max_attempts=1,
                timeout_s=1,
                stop_check=cancelled.is_set,
            )
        finally:
            capture_lock.release()
            timer.cancel()

        self.assertEqual(result.reason, "cancelled")
        self.assertLess(time.monotonic() - started, 0.3)

    def test_direct_capture_reports_immediate_driver_no_frame(self):
        result = capture_direct_with_paused_preview(
            threading.Lock(),
            lambda _paused: None,
            lambda _remaining: None,
            discard_frames=2,
            max_attempts=2,
            timeout_s=1,
            stop_check=lambda: False,
            last_error=lambda: "SDK returned no image data",
        )

        self.assertIsNone(result.image)
        self.assertEqual(result.reason, "driver-no-frame")
        self.assertEqual(result.read_count, 2)
        self.assertEqual(result.detail, "SDK returned no image data")


class StackCaptureTests(unittest.TestCase):
    def test_sequence_uses_fresh_capture_saves_immediately_and_restores_distance(self):
        frames = [
            numpy.full((12, 14, 3), value, dtype=numpy.uint8)
            for value in (10, 20, 30)
        ]
        camera = FakeFreshCamera(frames)
        movements = []

        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f"frame_{index}.tiff" for index in range(3)]
            captured = capture_stack_sequence(
                camera,
                "ArducamCamera",
                paths,
                move_next=lambda: movements.append(("up", 0.25)),
                restore_distance=lambda distance: movements.append(("down", distance)),
                step_distance=0.25,
                settle_delay=0,
                discard_frames=2,
                max_attempts=3,
                timeout_s=6,
                stop_check=lambda: False,
                sleeper=lambda _seconds: None,
            )

            self.assertEqual(captured, 3)
            self.assertEqual(
                movements,
                [("up", 0.25), ("up", 0.25), ("up", 0.25), ("down", 0.75)],
            )
            self.assertEqual(len(camera.calls), 3)
            self.assertEqual(
                [call["after_counter"] for call in camera.calls],
                [0, 1, 2],
            )
            self.assertTrue(all(path.is_file() for path in paths))
            self.assertEqual(
                [int(cv2.imread(str(path))[0, 0, 0]) for path in paths],
                [10, 20, 30],
            )

    def test_capture_failure_stops_sequence_and_restores_only_actual_movement(self):
        valid = numpy.full((8, 8, 3), 44, dtype=numpy.uint8)
        camera = FakeFreshCamera([valid, None, valid])
        movements = []

        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f"frame_{index}.tiff" for index in range(3)]
            with self.assertRaisesRegex(StackCaptureError, "did not return a fresh frame"):
                capture_stack_sequence(
                    camera,
                    "ArducamCamera",
                    paths,
                    move_next=lambda: movements.append(("up", 0.4)),
                    restore_distance=lambda distance: movements.append(("down", distance)),
                    step_distance=0.4,
                    settle_delay=0,
                    discard_frames=2,
                    max_attempts=3,
                    timeout_s=6,
                    stop_check=lambda: False,
                    sleeper=lambda _seconds: None,
                )

            self.assertEqual(movements, [("up", 0.4), ("down", 0.4)])
            self.assertEqual(len(camera.calls), 2)
            self.assertTrue(paths[0].is_file())
            self.assertFalse(paths[1].exists())
            self.assertFalse(paths[2].exists())

    def test_nonempty_black_frame_is_not_rejected_by_content(self):
        camera = FakeFreshCamera([numpy.zeros((8, 8, 3), dtype=numpy.uint8)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "black.tiff"
            captured = capture_stack_sequence(
                camera,
                "ArducamCamera",
                [path],
                move_next=lambda: None,
                restore_distance=lambda _distance: None,
                step_distance=0.1,
                settle_delay=0,
                discard_frames=0,
                max_attempts=1,
                timeout_s=1,
                stop_check=lambda: False,
                sleeper=lambda _seconds: None,
            )
            self.assertEqual(captured, 1)
            self.assertEqual(int(cv2.imread(str(path)).sum()), 0)

    def test_save_failure_stops_before_motor_movement(self):
        frame = numpy.ones((8, 8, 3), dtype=numpy.uint8)
        camera = FakeFreshCamera([frame])
        movements = []
        with mock.patch("Tools.stack_capture.cv2.imwrite", return_value=False):
            with self.assertRaisesRegex(StackCaptureError, "Could not save"):
                capture_stack_sequence(
                    camera,
                    "ArducamCamera",
                    ["frame.tiff"],
                    move_next=lambda: movements.append("up"),
                    restore_distance=lambda distance: movements.append(distance),
                    step_distance=0.1,
                    settle_delay=0,
                    discard_frames=0,
                    max_attempts=1,
                    timeout_s=1,
                    stop_check=lambda: False,
                    sleeper=lambda _seconds: None,
                )
        self.assertEqual(movements, [])

    def test_user_cancellation_does_not_restart_stage_motion(self):
        frame = numpy.ones((8, 8, 3), dtype=numpy.uint8)
        camera = FakeFreshCamera([frame, frame])
        stopped = [False]
        movements = []

        def move_next():
            movements.append("up")
            stopped[0] = True

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(StackCaptureError, "cancelled"):
                capture_stack_sequence(
                    camera,
                    "ArducamCamera",
                    [Path(directory) / "one.tiff", Path(directory) / "two.tiff"],
                    move_next=move_next,
                    restore_distance=lambda distance: movements.append(("down", distance)),
                    step_distance=0.1,
                    settle_delay=0,
                    discard_frames=0,
                    max_attempts=1,
                    timeout_s=1,
                    stop_check=lambda: stopped[0],
                    sleeper=lambda _seconds: None,
                )

        # Abort means stop immediately. The UI invalidates the axis reference
        # rather than starting an automatic return movement after Abort.
        self.assertEqual(movements, ["up"])

    def test_cancellation_after_capture_does_not_start_stage_motion(self):
        frame = numpy.ones((8, 8, 3), dtype=numpy.uint8)
        stopped = [False]
        movements = []

        class CancellingCamera(FakeFreshCamera):
            def get_fresh_image(self, **kwargs):
                result = super().get_fresh_image(**kwargs)
                stopped[0] = True
                return result

        camera = CancellingCamera([frame])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(StackCaptureError, "cancelled"):
                capture_stack_sequence(
                    camera,
                    "ArducamCamera",
                    [Path(directory) / "one.tiff"],
                    move_next=lambda: movements.append("up"),
                    restore_distance=lambda distance: movements.append(("down", distance)),
                    step_distance=0.1,
                    settle_delay=0,
                    discard_frames=0,
                    max_attempts=1,
                    timeout_s=1,
                    stop_check=lambda: stopped[0],
                    sleeper=lambda _seconds: None,
                )

        self.assertEqual(movements, [])

    def test_abort_during_return_is_reported_as_cancellation(self):
        frame = numpy.ones((8, 8, 3), dtype=numpy.uint8)
        camera = FakeFreshCamera([frame])
        stopped = [False]

        def restore_distance(_distance):
            stopped[0] = True

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                StackCaptureError,
                "cancelled while returning",
            ):
                capture_stack_sequence(
                    camera,
                    "ArducamCamera",
                    [Path(directory) / "one.tiff"],
                    move_next=lambda: None,
                    restore_distance=restore_distance,
                    step_distance=0.1,
                    settle_delay=0,
                    discard_frames=0,
                    max_attempts=1,
                    timeout_s=1,
                    stop_check=lambda: stopped[0],
                    sleeper=lambda _seconds: None,
                )

    def test_abort_during_final_upward_move_is_not_reported_as_success(self):
        frame = numpy.ones((8, 8, 3), dtype=numpy.uint8)
        camera = FakeFreshCamera([frame])
        stopped = [False]

        def move_next():
            stopped[0] = True

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                StackCaptureError,
                "cancelled during stage movement",
            ):
                capture_stack_sequence(
                    camera,
                    "ArducamCamera",
                    [Path(directory) / "one.tiff"],
                    move_next=move_next,
                    restore_distance=lambda _distance: self.fail(
                        "Abort must not start an automatic return movement"
                    ),
                    step_distance=0.1,
                    settle_delay=0,
                    discard_frames=0,
                    max_attempts=1,
                    timeout_s=1,
                    stop_check=lambda: stopped[0],
                    sleeper=lambda _seconds: None,
                )

    def test_preview_wait_never_returns_unchanged_cached_frame(self):
        frame = numpy.full((4, 4, 3), 77, dtype=numpy.uint8)
        now = [0.0]

        result = wait_for_new_preview_frame(
            lambda: (9, frame),
            9,
            discard_frames=0,
            timeout_s=0.05,
            clock=lambda: now[0],
            sleeper=lambda duration: now.__setitem__(0, now[0] + duration),
        )

        self.assertIsNone(result)

    def test_preview_wait_honors_discard_count_and_returns_a_copy(self):
        frame = numpy.full((4, 4, 3), 88, dtype=numpy.uint8)
        counter = [4]
        now = [0.0]

        def snapshot():
            counter[0] += 1
            return counter[0], frame

        result = wait_for_new_preview_frame(
            snapshot,
            4,
            discard_frames=2,
            timeout_s=1,
            clock=lambda: now[0],
            sleeper=lambda duration: now.__setitem__(0, now[0] + duration),
        )

        self.assertIsNotNone(result)
        self.assertEqual(counter[0], 7)
        result[0, 0, 0] = 0
        self.assertEqual(int(frame[0, 0, 0]), 88)

    def test_preview_wait_checks_once_more_at_the_timeout_boundary(self):
        frame = numpy.full((4, 4, 3), 99, dtype=numpy.uint8)
        now = [0.0]

        def snapshot():
            # Model Hunter's VA camera: the required third frame is published
            # while the final polling sleep crosses the timeout boundary.
            counter = 13 if now[0] >= 0.05 else 12
            return counter, frame

        result = wait_for_new_preview_frame(
            snapshot,
            10,
            discard_frames=2,
            timeout_s=0.05,
            clock=lambda: now[0],
            sleeper=lambda duration: now.__setitem__(0, now[0] + duration),
        )

        self.assertIsNotNone(result)
        self.assertEqual(int(result[0, 0, 0]), 99)


class BuiltInStackerTests(unittest.TestCase):
    @staticmethod
    def _legacy_focus_stack(images, gray_images):
        laplacians = numpy.asarray(
            [
                cv2.Laplacian(gray, cv2.CV_64F, ksize=7)
                for gray in gray_images
            ]
        )
        maxima = numpy.absolute(laplacians).max(axis=0)
        masks = (numpy.absolute(laplacians) == maxima).astype(numpy.uint8)
        output = numpy.zeros_like(images[0])
        for index, image in enumerate(images):
            output = cv2.bitwise_not(image, output, mask=masks[index])
        return 255 - output

    @staticmethod
    def _shifted_focus_stack():
        height = width = 320
        sharp = numpy.full((height, width, 3), 235, dtype=numpy.uint8)
        cv2.ellipse(sharp, (160, 160), (75, 42), 18, 0, 360, (35, 70, 125), -1)
        cv2.line(sharp, (115, 142), (205, 178), (245, 210, 50), 5)
        cv2.circle(sharp, (143, 151), 10, (30, 220, 220), -1)

        first = cv2.GaussianBlur(sharp, (0, 0), 2.2)
        first[:, :160] = sharp[:, :160]
        second_unshifted = cv2.GaussianBlur(sharp, (0, 0), 2.2)
        second_unshifted[:, 160:] = sharp[:, 160:]
        shift = numpy.float32([[1, 0, 9], [0, 1, -6]])
        second_shifted = cv2.warpAffine(
            second_unshifted,
            shift,
            (width, height),
            borderMode=cv2.BORDER_REFLECT,
        )
        return sharp, first, second_unshifted, second_shifted

    def test_alignment_transform_is_applied_to_color_and_focus_frames(self):
        _, first, second_unshifted, second_shifted = self._shifted_focus_stack()
        stacker = Stacker("", "", RecordingSignal(), RecordingSignal())

        aligned_color, aligned_gray = stacker.align_images([first, second_shifted])

        self.assertEqual(len(aligned_color), 2)
        self.assertEqual(len(aligned_gray), 2)
        color_error = numpy.mean(
            numpy.abs(
                aligned_color[1].astype(numpy.float32)
                - second_unshifted.astype(numpy.float32)
            )
        )
        self.assertLess(float(color_error), 1.0)

    def test_fixed_stack_reconstructs_shifted_two_plane_fixture(self):
        sharp, first, _, second_shifted = self._shifted_focus_stack()
        stacker = Stacker("", "", RecordingSignal(), RecordingSignal())

        _, aligned_gray = stacker.align_images([first, second_shifted])
        legacy_result = stacker.focus_stack(
            [first, second_shifted],
            aligned_gray,
        )
        stacked = stacker.stack_images([first, second_shifted])

        legacy_error = numpy.mean(
            numpy.abs(legacy_result.astype(numpy.float32) - sharp.astype(numpy.float32))
        )
        reconstruction_error = numpy.mean(
            numpy.abs(stacked.astype(numpy.float32) - sharp.astype(numpy.float32))
        )
        self.assertEqual(stacked.shape, sharp.shape)
        self.assertLess(float(reconstruction_error), 2.0)
        self.assertLess(float(reconstruction_error), float(legacy_error) * 0.3)

    def test_mismatched_frame_dimensions_are_rejected(self):
        stacker = Stacker("", "", RecordingSignal(), RecordingSignal())
        first = numpy.zeros((20, 20, 3), dtype=numpy.uint8)
        second = numpy.zeros((21, 20, 3), dtype=numpy.uint8)
        with self.assertRaisesRegex(RuntimeError, "same dimensions"):
            stacker.align_images([first, second])

    def test_streaming_focus_fusion_preserves_legacy_pixel_selection(self):
        generator = numpy.random.default_rng(1234)
        images = [
            generator.integers(0, 256, size=(40, 48, 3), dtype=numpy.uint8)
            for _ in range(4)
        ]
        gray_images = [
            cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            for image in images
        ]
        stacker = Stacker("", "", RecordingSignal(), RecordingSignal())

        expected = self._legacy_focus_stack(images, gray_images)
        actual = stacker.focus_stack(images, gray_images)

        numpy.testing.assert_array_equal(actual, expected)

    def test_run_reports_unreadable_input_without_false_success(self):
        status = RecordingSignal()
        done = RecordingSignal()
        failed = RecordingSignal()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "not_an_image.tiff").write_text("invalid", encoding="utf-8")
            output_path = Path(directory, "stacked.tiff")
            stacker = Stacker(
                directory,
                str(output_path),
                status,
                done,
                failed,
            )

            stacker.run()

            self.assertEqual(done.values, [])
            self.assertEqual(len(failed.values), 1)
            self.assertIn("Could not read stack frame", failed.values[0][0])
            self.assertFalse(output_path.exists())

    def test_run_ignores_non_image_files(self):
        status = RecordingSignal()
        done = RecordingSignal()
        failed = RecordingSignal()
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory, "frame_01.tiff")
            notes_path = Path(directory, "notes.txt")
            output_path = Path(directory, "stacked.tiff")
            self.assertTrue(
                cv2.imwrite(
                    str(image_path),
                    numpy.full((24, 24, 3), 120, dtype=numpy.uint8),
                )
            )
            notes_path.write_text("metadata", encoding="utf-8")
            stacker = Stacker(
                directory,
                str(output_path),
                status,
                done,
                failed,
            )

            stacker.run()

            self.assertEqual(len(done.values), 1)
            self.assertEqual(failed.values, [])
            self.assertTrue(output_path.is_file())


class HeliconStackerTests(unittest.TestCase):
    def test_nonzero_exit_reports_failure_without_false_success(self):
        status = RecordingSignal()
        done = RecordingSignal()
        failed = RecordingSignal()
        stacker = HeliconStacker(
            "HeliconFocus.exe",
            "frames",
            "stacked.tiff",
            status,
            done,
            failed,
        )

        with mock.patch("Tools.stacker.subprocess.run") as run:
            run.return_value.returncode = 1
            stacker.run()

        self.assertEqual(done.values, [])
        self.assertEqual(len(failed.values), 1)
        self.assertIn("did not create", failed.values[0][0])

    def test_jpeg_output_uses_safe_argument_list_and_quality(self):
        status = RecordingSignal()
        done = RecordingSignal()
        failed = RecordingSignal()
        stacker = HeliconStacker(
            r"C:\Program Files\Helicon\HeliconFocus.exe",
            r"C:\Images With Spaces\frames",
            r"C:\Images With Spaces\sample_stacked_01.jpg",
            status,
            done,
            failed,
        )

        with mock.patch("Tools.stacker.subprocess.run") as run, mock.patch(
            "Tools.stacker.os.path.isfile", return_value=True
        ):
            run.return_value.returncode = 0
            stacker.run()

        command = run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertIn("-j:95", command)
        self.assertIn(r"-save:C:\Images With Spaces\sample_stacked_01.jpg", command)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(len(done.values), 1)
        self.assertEqual(failed.values, [])

    def test_launch_exception_reports_failure_without_false_success(self):
        status = RecordingSignal()
        done = RecordingSignal()
        failed = RecordingSignal()
        stacker = HeliconStacker(
            "HeliconFocus.exe",
            "frames",
            "stacked.tiff",
            status,
            done,
            failed,
        )

        with mock.patch(
            "Tools.stacker.subprocess.run",
            side_effect=OSError("launch failed"),
        ):
            stacker.run()

        self.assertEqual(done.values, [])
        self.assertEqual(len(failed.values), 1)
        self.assertIn("launch failed", failed.values[0][0])


if __name__ == "__main__":
    unittest.main()
