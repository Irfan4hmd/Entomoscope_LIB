import sys
import threading
import time
import types
import unittest
from unittest import mock

import numpy
from PyQt5.QtWidgets import QApplication  # Loads the supported PyQt sip module.

sys.modules.setdefault("ArducamSDK", types.ModuleType("ArducamSDK"))

try:
    import arducam_config_parser  # noqa: F401
except ModuleNotFoundError:
    sys.modules["arducam_config_parser"] = types.ModuleType("arducam_config_parser")
    fake_arducam_camera = types.ModuleType("Arducam.Arducam")
    fake_arducam_camera.ArducamCamera = type("ArducamCamera", (), {})
    sys.modules["Arducam.Arducam"] = fake_arducam_camera
    fake_arducam_convert = types.ModuleType("Arducam.ImageConvert")
    fake_arducam_convert.convert_image_arducam = (
        lambda image, _config, _color_mode: image
    )
    sys.modules["Arducam.ImageConvert"] = fake_arducam_convert

try:
    import googleapiclient  # noqa: F401
except ModuleNotFoundError:
    fake_zenodo = types.ModuleType("Tools.zenodo_uploader")
    fake_zenodo.Uploader = type("Uploader", (), {})
    sys.modules["Tools.zenodo_uploader"] = fake_zenodo
try:
    import ultralytics  # noqa: F401
except ModuleNotFoundError:
    fake_ultralytics = types.ModuleType("ultralytics")
    fake_ultralytics.YOLO = mock.Mock
    sys.modules["ultralytics"] = fake_ultralytics
    fake_ml_models = types.ModuleType("Tools.ml_models")
    fake_ml_models.Predictor = type("Predictor", (), {})
    fake_ml_models.load_models = lambda *_args, **_kwargs: None
    fake_ml_models.YOLO_MODELS = {}
    sys.modules["Tools.ml_models"] = fake_ml_models
    fake_crop = types.ModuleType("Tools.cropping_boxsegmenter")
    fake_crop.crop_image = lambda image, *_args, **_kwargs: image
    sys.modules["Tools.cropping_boxsegmenter"] = fake_crop

try:
    import refiners.solutions  # noqa: F401
except ModuleNotFoundError:
    fake_refiners = types.ModuleType("refiners")
    fake_refiners.__path__ = []
    fake_refiners_solutions = types.ModuleType("refiners.solutions")
    fake_refiners_solutions.BoxSegmenter = type("BoxSegmenter", (), {})
    sys.modules["refiners"] = fake_refiners
    sys.modules["refiners.solutions"] = fake_refiners_solutions
try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    fake_serial = types.ModuleType("serial")
    fake_serial.Serial = type("Serial", (), {})
    fake_serial_tools = types.ModuleType("serial.tools")
    fake_list_ports = types.ModuleType("serial.tools.list_ports")
    fake_list_ports.comports = lambda: []
    sys.modules["serial"] = fake_serial
    sys.modules["serial.tools"] = fake_serial_tools
    sys.modules["serial.tools.list_ports"] = fake_list_ports
try:
    import gxipy  # noqa: F401
except ModuleNotFoundError:
    sys.modules["gxipy"] = types.ModuleType("gxipy")
    fake_va_camera = types.ModuleType("VAImagingcam.VAImagingcam")
    fake_va_camera.VAImagingCamera = type("VAImagingCamera", (), {})
    sys.modules["VAImagingcam.VAImagingcam"] = fake_va_camera
    fake_va_convert = types.ModuleType("VAImagingcam.ImageConvert")
    fake_va_convert.convert_image = lambda image, _color_mode: image
    sys.modules["VAImagingcam.ImageConvert"] = fake_va_convert

from UserInterface.ui import Arducam, MainWindow, MessageBox, VAImaging


class FakeArducamWrapper:
    def __init__(self, images, preview_image=None):
        self.capture_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self.current_img = None
        self.frame_counter = 0
        self.paused = False
        self.images = list(images)
        self.preview_image = preview_image
        self.preview_timeouts = []
        self.preview_start_counters = []
        self.timeouts = []

    def get_frame_counter(self):
        return self.frame_counter

    def get_preview_fresh_image(
        self,
        start_counter,
        discard_frames,
        timeout_s,
        stop_check,
    ):
        self.preview_start_counters.append(start_counter)
        self.preview_timeouts.append(timeout_s)
        return self.preview_image

    def _take_image_unlocked(
        self,
        timeout_s,
        raise_errors=False,
        notify_error=True,
    ):
        self.timeouts.append(timeout_s)
        return self.images.pop(0) if self.images else None


class FakeVAWrapper:
    def __init__(self, images, preview_image=None):
        self.capture_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self.current_img = None
        self.frame_counter = 10
        self.paused = False
        self.images = list(images)
        self.preview_image = preview_image
        self.preview_timeouts = []
        self.preview_start_counters = []
        self.direct_calls = 0
        self.direct_timeouts = []

    def get_frame_counter(self):
        return self.frame_counter

    def get_preview_fresh_image(
        self,
        start_counter,
        discard_frames,
        timeout_s,
        stop_check,
    ):
        self.preview_start_counters.append(start_counter)
        self.preview_timeouts.append(timeout_s)
        return self.preview_image

    def _take_image_unlocked(
        self,
        timeout_s,
        raise_errors=False,
        notify_error=True,
    ):
        self.direct_calls += 1
        self.direct_timeouts.append(timeout_s)
        return self.images.pop(0) if self.images else None


class CameraFreshnessTests(unittest.TestCase):
    def test_arducam_uses_preview_frame_after_requested_counter(self):
        preview = numpy.full((4, 4, 3), 9, dtype=numpy.uint8)
        camera = FakeArducamWrapper([], preview_image=preview)

        result = Arducam.get_fresh_image(
            camera,
            discard_frames=2,
            max_attempts=1,
            timeout_s=6,
            stop_check=lambda: False,
            after_counter=41,
        )

        self.assertEqual(int(result[0, 0, 0]), 9)
        self.assertEqual(camera.preview_start_counters, [41])
        self.assertAlmostEqual(camera.preview_timeouts[0], 6.0, delta=0.05)
        self.assertEqual(camera.timeouts, [])
        self.assertEqual(camera.last_fresh_capture_source, "preview")

    def test_arducam_waits_for_a_live_preview_counter_without_direct_reads(self):
        camera = FakeArducamWrapper([])
        camera.get_preview_fresh_image = types.MethodType(
            Arducam.get_preview_fresh_image, camera
        )

        def publish_frames():
            for value in (1, 2, 3):
                time.sleep(0.01)
                with camera.frame_lock:
                    camera.current_img = numpy.full(
                        (4, 4, 3), value, dtype=numpy.uint8
                    )
                    camera.frame_counter += 1

        producer = threading.Thread(target=publish_frames)
        producer.start()
        try:
            result = Arducam.get_fresh_image(
                camera,
                discard_frames=2,
                max_attempts=1,
                timeout_s=1,
                stop_check=lambda: False,
                after_counter=0,
            )
        finally:
            producer.join(timeout=1)

        self.assertEqual(int(result[0, 0, 0]), 3)
        self.assertEqual(camera.timeouts, [])
        self.assertEqual(camera.last_fresh_capture_source, "preview")

    def test_arducam_already_paused_skips_preview_and_restores_pause(self):
        images = [
            numpy.full((4, 4, 3), value, dtype=numpy.uint8)
            for value in (1, 2, 3)
        ]
        camera = FakeArducamWrapper(images)
        camera.paused = True

        result = Arducam.get_fresh_image(
            camera,
            discard_frames=2,
            max_attempts=1,
            timeout_s=1,
            stop_check=lambda: False,
        )

        self.assertEqual(int(result[0, 0, 0]), 3)
        self.assertEqual(camera.preview_timeouts, [])
        self.assertTrue(camera.paused)
        self.assertEqual(camera.last_fresh_capture_source, "direct-fallback")

    def test_arducam_preview_and_lock_wait_have_independent_bounded_phases(self):
        camera = FakeArducamWrapper([])
        camera.capture_lock.acquire()

        def delayed_preview(
            start_counter,
            discard_frames,
            timeout_s,
            stop_check,
        ):
            camera.preview_timeouts.append(timeout_s)
            time.sleep(timeout_s)
            return None

        camera.get_preview_fresh_image = delayed_preview
        started = time.monotonic()
        try:
            result = Arducam.get_fresh_image(
                camera,
                discard_frames=0,
                max_attempts=1,
                timeout_s=0.2,
                stop_check=lambda: False,
            )
        finally:
            camera.capture_lock.release()
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertAlmostEqual(camera.preview_timeouts[0], 0.2, delta=0.03)
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertLess(elapsed, 0.55)
        self.assertIn("lock-timeout", camera.last_fresh_capture_error)

    def test_inflight_preview_cannot_overwrite_newer_direct_frame(self):
        for camera_class, fake_class in (
            (Arducam, FakeArducamWrapper),
            (VAImaging, FakeVAWrapper),
        ):
            with self.subTest(camera=camera_class.__name__):
                preview_frame = numpy.full((4, 4, 3), 3, dtype=numpy.uint8)
                direct_frame = numpy.full((4, 4, 3), 9, dtype=numpy.uint8)
                camera = fake_class([])
                camera._run_flag = True
                initial_counter = camera.frame_counter
                preview_read_started = threading.Event()
                release_preview_read = threading.Event()
                call_count = 0
                call_lock = threading.Lock()

                def take_image(
                    timeout_s=1,
                    raise_errors=False,
                    notify_error=True,
                ):
                    nonlocal call_count
                    with call_lock:
                        call_count += 1
                        current_call = call_count
                    if current_call == 1:
                        preview_read_started.set()
                        release_preview_read.wait(timeout=1)
                        return preview_frame
                    return direct_frame

                def update_image(image):
                    with camera.frame_lock:
                        camera.current_img = image
                        camera.frame_counter += 1

                camera._take_image_unlocked = take_image
                camera.update_image = update_image
                preview_thread = threading.Thread(
                    target=lambda: camera_class._capture_and_publish_preview(camera)
                )
                preview_thread.start()
                self.assertTrue(preview_read_started.wait(timeout=1))

                direct_result = []
                direct_thread = threading.Thread(
                    target=lambda: direct_result.append(
                        camera_class.get_fresh_image(
                            camera,
                            discard_frames=0,
                            max_attempts=1,
                            timeout_s=1,
                            stop_check=lambda: False,
                        )
                    )
                )
                direct_thread.start()
                deadline = time.monotonic() + 1
                while not camera.paused and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertTrue(camera.paused)
                self.assertTrue(direct_thread.is_alive())

                release_preview_read.set()
                preview_thread.join(timeout=1)
                direct_thread.join(timeout=1)

                self.assertFalse(preview_thread.is_alive())
                self.assertFalse(direct_thread.is_alive())
                self.assertEqual(int(direct_result[0][0, 0, 0]), 9)
                self.assertEqual(int(camera.current_img[0, 0, 0]), 9)
                self.assertEqual(camera.frame_counter, initial_counter + 2)
    def test_arducam_pauses_preview_before_direct_fallback_and_discards_frames(self):
        images = [
            numpy.full((4, 4, 3), value, dtype=numpy.uint8)
            for value in (1, 2, 3)
        ]
        camera = FakeArducamWrapper(images)

        result = Arducam.get_fresh_image(
            camera,
            discard_frames=2,
            max_attempts=1,
            timeout_s=1,
            stop_check=lambda: False,
        )

        self.assertEqual(int(result[0, 0, 0]), 3)
        self.assertAlmostEqual(camera.preview_timeouts[0], 1.0, delta=0.05)
        self.assertEqual(len(camera.timeouts), 3)
        self.assertTrue(all(0 < timeout <= 1 for timeout in camera.timeouts))
        self.assertEqual(camera.frame_counter, 1)
        self.assertFalse(camera.paused)
        self.assertEqual(camera.last_fresh_capture_source, "direct-fallback")

    def test_arducam_fallback_does_not_repeat_preview_frames_already_discarded(self):
        final_frame = numpy.full((4, 4, 3), 19, dtype=numpy.uint8)
        camera = FakeArducamWrapper([final_frame])

        def preview_timeout_after_two_frames(
            start_counter,
            discard_frames,
            timeout_s,
            stop_check,
        ):
            camera.preview_start_counters.append(start_counter)
            camera.preview_timeouts.append(timeout_s)
            camera.frame_counter = start_counter + 2
            return None

        camera.get_preview_fresh_image = preview_timeout_after_two_frames

        result = Arducam.get_fresh_image(
            camera,
            discard_frames=2,
            max_attempts=1,
            timeout_s=2.5,
            stop_check=lambda: False,
        )

        self.assertIsNotNone(result)
        self.assertEqual(int(result[0, 0, 0]), 19)
        self.assertEqual(len(camera.timeouts), 1)
        self.assertEqual(camera.frame_counter, 3)
        self.assertEqual(camera.last_fresh_capture_source, "direct-fallback")

    def test_arducam_zero_budget_does_not_start_an_sdk_read(self):
        camera = FakeArducamWrapper(
            [numpy.ones((4, 4, 3), dtype=numpy.uint8)]
        )

        result = Arducam.get_fresh_image(
            camera,
            discard_frames=0,
            max_attempts=3,
            timeout_s=0,
            stop_check=lambda: False,
        )

        self.assertIsNone(result)
        self.assertEqual(camera.preview_timeouts, [0])
        self.assertEqual(camera.timeouts, [])
        self.assertFalse(camera.paused)

    def test_arducam_read_passes_timeout_to_camera_driver(self):
        driver = mock.Mock()
        wrapper = type("Wrapper", (), {"camera": driver, "show_error": mock.Mock()})()

        Arducam.read_image(wrapper, timeout_ms=37.8)

        driver.read.assert_called_once_with(timeout=37)

    def test_arducam_direct_driver_exception_is_preserved(self):
        driver = mock.Mock()
        driver.read.side_effect = RuntimeError("Arducam SDK failure")
        wrapper = types.SimpleNamespace(
            camera=driver,
            show_error=types.SimpleNamespace(emit=mock.Mock()),
        )
        wrapper.read_image = types.MethodType(Arducam.read_image, wrapper)

        with self.assertRaisesRegex(RuntimeError, "Arducam SDK failure"):
            Arducam._take_image_unlocked(
                wrapper, timeout_s=0.1, raise_errors=True
            )

        self.assertIn("Arducam SDK failure", wrapper.last_capture_error)
    def test_arducam_fresh_capture_reports_direct_driver_exception(self):
        camera = FakeArducamWrapper([])

        def fail(timeout_s, raise_errors=False, notify_error=True):
            raise RuntimeError("Arducam direct failure")

        camera._take_image_unlocked = fail
        result = Arducam.get_fresh_image(
            camera,
            discard_frames=0,
            max_attempts=1,
            timeout_s=1,
            stop_check=lambda: False,
        )

        self.assertIsNone(result)
        self.assertIn("driver-exception", camera.last_fresh_capture_error)
        self.assertIn("Arducam direct failure", camera.last_fresh_capture_error)
    def test_va_gives_preview_and_direct_fallback_independent_budgets(self):
        images = [
            numpy.full((4, 4, 3), value, dtype=numpy.uint8)
            for value in (4, 5, 6)
        ]
        camera = FakeVAWrapper(images)

        result = VAImaging.get_fresh_image(
            camera,
            discard_frames=2,
            max_attempts=1,
            timeout_s=6,
            stop_check=lambda: False,
        )

        self.assertAlmostEqual(camera.preview_timeouts[0], 6.0, delta=0.05)
        self.assertEqual(camera.direct_calls, 3)
        self.assertEqual(len(camera.direct_timeouts), 3)
        self.assertTrue(all(0 < timeout <= 6 for timeout in camera.direct_timeouts))
        self.assertEqual(int(result[0, 0, 0]), 6)
        self.assertEqual(camera.frame_counter, 11)
        self.assertFalse(camera.paused)
        self.assertEqual(camera.last_fresh_capture_source, "direct-fallback")

    def test_va_fallback_does_not_repeat_preview_frames_already_discarded(self):
        final_frame = numpy.full((4, 4, 3), 17, dtype=numpy.uint8)
        camera = FakeVAWrapper([final_frame])

        def preview_timeout_after_two_frames(
            start_counter,
            discard_frames,
            timeout_s,
            stop_check,
        ):
            camera.preview_start_counters.append(start_counter)
            camera.preview_timeouts.append(timeout_s)
            camera.frame_counter = start_counter + 2
            return None

        camera.get_preview_fresh_image = preview_timeout_after_two_frames

        result = VAImaging.get_fresh_image(
            camera,
            discard_frames=2,
            max_attempts=1,
            timeout_s=6,
            stop_check=lambda: False,
        )

        self.assertIsNotNone(result)
        self.assertEqual(int(result[0, 0, 0]), 17)
        self.assertEqual(camera.direct_calls, 1)
        self.assertEqual(camera.frame_counter, 13)
        self.assertEqual(camera.last_fresh_capture_source, "direct-fallback")

    def test_va_zero_budget_does_not_start_direct_fallback(self):
        camera = FakeVAWrapper(
            [numpy.ones((4, 4, 3), dtype=numpy.uint8)]
        )

        result = VAImaging.get_fresh_image(
            camera,
            discard_frames=0,
            max_attempts=3,
            timeout_s=0,
            stop_check=lambda: False,
        )

        self.assertIsNone(result)
        self.assertEqual(camera.preview_timeouts, [0])
        self.assertEqual(camera.direct_calls, 0)
        self.assertFalse(camera.paused)

    def test_va_read_passes_timeout_to_camera_driver(self):
        driver = mock.Mock()
        wrapper = type("Wrapper", (), {"camera": driver, "show_error": mock.Mock()})()

        VAImaging.read_image(wrapper, timeout_ms=42.9)

        driver.read.assert_called_once_with(timeout=42)


    def test_va_direct_driver_exception_is_preserved(self):
        driver = mock.Mock()
        driver.read.side_effect = RuntimeError("VA SDK failure")
        wrapper = types.SimpleNamespace(
            camera=driver,
            show_error=types.SimpleNamespace(emit=mock.Mock()),
        )
        wrapper.read_image = types.MethodType(VAImaging.read_image, wrapper)

        with self.assertRaisesRegex(RuntimeError, "VA SDK failure"):
            VAImaging._take_image_unlocked(
                wrapper, timeout_s=0.1, raise_errors=True
            )

        self.assertIn("VA SDK failure", wrapper.last_capture_error)

    def test_va_fresh_capture_reports_direct_driver_exception(self):
        camera = FakeVAWrapper([])

        def fail(timeout_s, raise_errors=False, notify_error=True):
            raise RuntimeError("VA direct failure")

        camera._take_image_unlocked = fail
        result = VAImaging.get_fresh_image(
            camera,
            discard_frames=0,
            max_attempts=1,
            timeout_s=1,
            stop_check=lambda: False,
        )

        self.assertIsNone(result)
        self.assertIn("driver-exception", camera.last_fresh_capture_error)
        self.assertIn("VA direct failure", camera.last_fresh_capture_error)

    def test_controlled_capture_suppresses_fatal_driver_signal_for_both_cameras(self):
        for camera_class in (Arducam, VAImaging):
            with self.subTest(camera=camera_class.__name__):
                driver = mock.Mock()
                driver.read.side_effect = RuntimeError("temporary SDK timeout")
                wrapper = types.SimpleNamespace(
                    camera=driver,
                    show_error=types.SimpleNamespace(emit=mock.Mock()),
                    paused=False,
                    nonfatal_camera_errors=False,
                )

                with mock.patch("UserInterface.ui.sleep", return_value=None):
                    camera_class.begin_autofocus_capture(wrapper)
                with self.assertRaisesRegex(RuntimeError, "temporary SDK timeout"):
                    camera_class.read_image(wrapper, timeout_ms=50)

                wrapper.show_error.emit.assert_not_called()

                camera_class.end_autofocus_capture(wrapper)
                with self.assertRaisesRegex(RuntimeError, "temporary SDK timeout"):
                    camera_class.read_image(wrapper, timeout_ms=50)

                wrapper.show_error.emit.assert_called_once_with()

    def test_autofocus_allows_a_slow_camera_capture_budget(self):
        frame = numpy.ones((4, 4, 3), dtype=numpy.uint8)

        class SlowCamera:
            def __init__(self):
                self.timeouts = []

            def get_fresh_image(self, **kwargs):
                self.timeouts.append(kwargs["timeout_s"])
                return frame if kwargs["timeout_s"] >= 3 else None

        camera = SlowCamera()
        window = types.SimpleNamespace(
            stop=False,
            arducam=camera,
            _autofocus_should_stop=lambda: False,
            _autofocus_wait=lambda _duration: True,
        )

        result = MainWindow._autofocus_get_image(
            window,
            max_retries=1,
            discard_frames=1,
        )

        self.assertIs(result, frame)
        self.assertEqual(camera.timeouts, [3.0])

    def test_autofocus_worker_routes_errors_and_cleanup_through_qt_signals(self):
        error_signal = RecordingSignal()
        finished_signal = RecordingSignal()
        window = types.SimpleNamespace(
            stop=False,
            axis=types.SimpleNamespace(lower_limit=100),
            _autofocus_prepare_capture=mock.Mock(
                side_effect=RuntimeError("camera SDK stopped")
            ),
            _autofocus_finish_capture=mock.Mock(),
            autofocus_error_signal=error_signal,
            autofocus_finished_signal=finished_signal,
            set_motor_control_state=mock.Mock(),
        )

        with mock.patch.object(MessageBox, "hide_message") as hide_message:
            MainWindow.do_autofocus_robust(window)

        self.assertEqual(len(error_signal.values), 1)
        self.assertIn("camera SDK stopped", error_signal.values[0][0])
        self.assertEqual(finished_signal.values, [()])
        hide_message.assert_not_called()
        window.set_motor_control_state.assert_not_called()

    def test_autofocus_worker_honors_shutdown_cancel_before_camera_setup(self):
        finished_signal = RecordingSignal()
        window = types.SimpleNamespace(
            stop=True,
            _autofocus_prepare_capture=mock.Mock(),
            autofocus_finished_signal=finished_signal,
        )

        MainWindow.do_autofocus_robust(window)

        window._autofocus_prepare_capture.assert_not_called()
        self.assertEqual(finished_signal.values, [()])

    def test_autofocus_ui_cleanup_runs_in_signal_handler(self):
        single_button = RecordingButton()
        single_button.enabled = False
        window = types.SimpleNamespace(
            set_motor_control_state=mock.Mock(),
            _autofocus_active=True,
            _stack_operation_active=False,
            device_selected=True,
            camera=object(),
            showing_static_image=False,
            take_single_picture_bttn=single_button,
        )
        window._update_single_capture_button = types.MethodType(
            MainWindow._update_single_capture_button,
            window,
        )

        with mock.patch.object(MessageBox, "hide_message") as hide_message:
            MainWindow._finish_autofocus_ui(window)

        hide_message.assert_called_once_with()
        self.assertFalse(window._autofocus_active)
        window.set_motor_control_state.assert_called_once_with(True)
        self.assertTrue(single_button.enabled)

    def test_autofocus_completion_does_not_touch_widgets_during_shutdown(self):
        window = types.SimpleNamespace(
            _autofocus_active=True,
            _close_pending=True,
            set_motor_control_state=mock.Mock(),
            set_camera_lens_state=mock.Mock(),
            _update_single_capture_button=mock.Mock(),
        )

        with mock.patch.object(MessageBox, "hide_message") as hide_message:
            MainWindow._finish_autofocus_ui(window)

        self.assertFalse(window._autofocus_active)
        hide_message.assert_not_called()
        window.set_motor_control_state.assert_not_called()
        window.set_camera_lens_state.assert_not_called()
        window._update_single_capture_button.assert_not_called()

    def test_queued_message_cleanup_is_ignored_during_shutdown(self):
        window = types.SimpleNamespace(_close_pending=True)

        with mock.patch.object(MessageBox, "hide_message") as hide_message:
            MainWindow._hide_operation_message(window)

        hide_message.assert_not_called()

    def test_queued_camera_error_is_ignored_during_shutdown(self):
        window = types.SimpleNamespace(
            _close_pending=True,
            isVisible=mock.Mock(return_value=True),
            close=mock.Mock(),
        )

        with mock.patch("UserInterface.ui.QMessageBox.critical") as critical:
            MainWindow.camera_connection_error(window)

        critical.assert_not_called()
        window.close.assert_not_called()

class RecordingSignal:
    def __init__(self):
        self.values = []

    def emit(self, *values):
        self.values.append(values)


class RecordingButton:
    def __init__(self):
        self.enabled = None

    def setEnabled(self, enabled):
        self.enabled = enabled

    def isEnabled(self):
        return bool(self.enabled)


class CameraWorkerLifecycleTests(unittest.TestCase):
    def test_update_ui_state_reuses_the_single_running_camera_worker(self):
        class Widget:
            def setEnabled(self, _enabled):
                return None

            def setText(self, _text):
                return None

        existing_worker = object()
        lens = types.SimpleNamespace(scale=None, label_str="Test lens")
        window = types.SimpleNamespace(
            camera=object(),
            camera_type="ArducamCamera",
            arducam=existing_worker,
            device_selected=True,
            showing_static_image=False,
            current_lens=lens,
            current_scale=0.01,
            video_label=object(),
            show_connection_error=object(),
            motor_reference_button=Widget(),
            take_single_picture_bttn=Widget(),
            motor_stop_button=Widget(),
            post_proc_bttn=Widget(),
            orientation_button=Widget(),
            device_menu=Widget(),
            connection_status_label=Widget(),
            proc_var_progress_label=Widget(),
            lens_info_label=Widget(),
            is_entomoscope_connected=lambda: True,
            set_motor_control_state=lambda _enabled: None,
            set_stack_setting_state=lambda _enabled: None,
            set_camera_lens_state=lambda _enabled: None,
        )
        window._update_single_capture_button = types.MethodType(
            MainWindow._update_single_capture_button,
            window,
        )

        with mock.patch(
            "UserInterface.ui.Arducam",
            return_value=object(),
        ):
            MainWindow.update_ui_state(window)
            MainWindow.update_ui_state(window)

        self.assertIs(window.arducam, existing_worker)

    def test_update_ui_state_keeps_single_capture_disabled_during_stack(self):
        class Widget:
            def __init__(self):
                self.enabled = None

            def setEnabled(self, enabled):
                self.enabled = enabled

            def setText(self, _text):
                return None

        single_button = Widget()
        window = types.SimpleNamespace(
            camera=object(),
            camera_type="ArducamCamera",
            arducam=object(),
            device_selected=True,
            showing_static_image=False,
            _stack_operation_active=True,
            _autofocus_active=False,
            current_lens=types.SimpleNamespace(scale=None, label_str="Test lens"),
            current_scale=0.01,
            motor_reference_button=Widget(),
            take_single_picture_bttn=single_button,
            motor_stop_button=Widget(),
            post_proc_bttn=Widget(),
            orientation_button=Widget(),
            device_menu=Widget(),
            connection_status_label=Widget(),
            proc_var_progress_label=Widget(),
            lens_info_label=Widget(),
            is_entomoscope_connected=lambda: True,
            set_motor_control_state=lambda _enabled: None,
            set_stack_setting_state=lambda _enabled: None,
            set_camera_lens_state=lambda _enabled: None,
        )
        window._update_single_capture_button = types.MethodType(
            MainWindow._update_single_capture_button,
            window,
        )

        MainWindow.update_ui_state(window)

        self.assertFalse(single_button.enabled)


class SingleCaptureTests(unittest.TestCase):
    def test_single_capture_starts_a_worker_instead_of_reading_on_gui_thread(self):
        class Camera:
            def get_fresh_image(self, **_kwargs):
                raise AssertionError("camera read must not run on the GUI thread")

        single_button = RecordingButton()
        single_button.enabled = True
        window = types.SimpleNamespace(
            arducam=Camera(),
            camera=object(),
            camera_type="ArducamCamera",
            device_selected=True,
            showing_static_image=False,
            _stack_operation_active=False,
            _autofocus_active=False,
            _lens_reposition_active=False,
            _single_capture_active=False,
            take_single_picture_bttn=single_button,
            statusBar=types.SimpleNamespace(showMessage=lambda *_args: None),
            _capture_single_image_worker=mock.Mock(),
        )
        thread = mock.Mock()

        with mock.patch("UserInterface.ui.Thread", return_value=thread):
            MainWindow.take_single_picture(window)

        self.assertTrue(window._single_capture_active)
        self.assertIs(window._single_capture_thread, thread)
        thread.start.assert_called_once_with()
        window._capture_single_image_worker.assert_not_called()

    def test_single_capture_saves_a_new_frame_instead_of_cached_preview(self):
        cached = numpy.full((4, 4, 3), 1, dtype=numpy.uint8)
        fresh = numpy.full((4, 4, 3), 9, dtype=numpy.uint8)

        class Camera:
            def __init__(self):
                self.events = []

            def get_frame_counter(self):
                return 17

            def get_image(self):
                return cached

            def get_fresh_image(self, **kwargs):
                self.events.append(("fresh", kwargs))
                return fresh

            def begin_single_capture(self):
                self.events.append(("begin", {}))

            def end_single_capture(self):
                self.events.append(("end", {}))

        camera = Camera()
        directory_manager = types.SimpleNamespace(
            add_image=lambda **_kwargs: ("single.tiff", "."),
            current_dir_mode=0,
        )
        window = types.SimpleNamespace(
            arducam=camera,
            camera_type="ArducamCamera",
            directory_manager=directory_manager,
            _stack_operation_active=False,
            _autofocus_active=False,
            stop=False,
            _close_pending=False,
            single_capture_ready_signal=RecordingSignal(),
            single_capture_error_signal=RecordingSignal(),
            statusBar=types.SimpleNamespace(showMessage=lambda *_args: None),
        )
        saved = []

        with (
            mock.patch(
                "UserInterface.ui.cv2.imwrite",
                side_effect=lambda _path, image: saved.append(image.copy()) or False,
            ),
            mock.patch("UserInterface.ui.QMessageBox.critical"),
        ):
            MainWindow._capture_single_image_worker(window)
            captured = window.single_capture_ready_signal.values[0][0]
            MainWindow._save_single_picture(window, captured)

        self.assertEqual(len(saved), 1)
        self.assertEqual(int(saved[0][0, 0, 0]), 9)
        self.assertEqual(camera.events[0][0], "begin")
        self.assertEqual(camera.events[1][0], "fresh")
        self.assertEqual(camera.events[1][1]["after_counter"], 17)
        self.assertEqual(camera.events[-1][0], "end")

    def test_va_single_capture_no_frame_returns_without_color_conversion(self):
        class Camera:
            def get_frame_counter(self):
                return 3

            def get_image(self):
                return None

            def get_fresh_image(self, **_kwargs):
                return None

            def begin_single_capture(self):
                return None

            def end_single_capture(self):
                return None

        directory_calls = []
        window = types.SimpleNamespace(
            arducam=Camera(),
            camera_type="VAImagingCamera",
            directory_manager=types.SimpleNamespace(
                add_image=lambda **kwargs: directory_calls.append(kwargs),
            ),
            _stack_operation_active=False,
            _autofocus_active=False,
            stop=False,
            _close_pending=False,
            single_capture_ready_signal=RecordingSignal(),
            single_capture_error_signal=RecordingSignal(),
            statusBar=types.SimpleNamespace(showMessage=lambda *_args: None),
        )

        caught = None
        with mock.patch("UserInterface.ui.cv2.cvtColor") as convert:
            try:
                MainWindow._capture_single_image_worker(window)
            except Exception as exc:  # The old VA path called cvtColor(None, ...).
                caught = exc

        self.assertIsNone(caught)
        convert.assert_not_called()
        self.assertEqual(window.single_capture_ready_signal.values, [])
        self.assertEqual(len(window.single_capture_error_signal.values), 1)
        self.assertEqual(directory_calls, [])

    def test_single_capture_is_rejected_while_autofocus_is_active(self):
        reads = []
        window = types.SimpleNamespace(
            arducam=types.SimpleNamespace(
                get_image=lambda: reads.append("cached"),
                get_fresh_image=lambda **_kwargs: reads.append("fresh"),
            ),
            camera_type="ArducamCamera",
            _stack_operation_active=False,
            _autofocus_active=True,
            statusBar=types.SimpleNamespace(showMessage=lambda *_args: None),
        )

        MainWindow.take_single_picture(window)

        self.assertEqual(reads, [])

    def test_single_capture_is_rejected_while_saved_focus_move_is_active(self):
        reads = []
        window = types.SimpleNamespace(
            arducam=types.SimpleNamespace(
                get_fresh_image=lambda **_kwargs: reads.append("fresh"),
            ),
            camera_type="ArducamCamera",
            _stack_operation_active=False,
            _autofocus_active=False,
            _lens_reposition_active=True,
            _single_capture_active=False,
            statusBar=types.SimpleNamespace(showMessage=lambda *_args: None),
        )

        MainWindow.take_single_picture(window)

        self.assertEqual(reads, [])

    def test_autofocus_start_marks_operation_active_and_disables_single_capture(self):
        single_button = RecordingButton()
        single_button.enabled = True
        motor_up_button = RecordingButton()
        motor_up_button.enabled = False
        motor_down_button = RecordingButton()
        motor_down_button.enabled = False
        window = types.SimpleNamespace(
            lens_approved=True,
            current_lens=types.SimpleNamespace(),
            _autofocus_active=False,
            _autofocus_thread=None,
            take_single_picture_bttn=single_button,
            motor_up_button=motor_up_button,
            motor_down_button=motor_down_button,
            set_motor_control_state=lambda _enabled: None,
            set_camera_lens_state=mock.Mock(),
            do_autofocus_robust=lambda: None,
            abort_autofocus=lambda *_args: None,
        )
        thread = mock.Mock()

        with (
            mock.patch("UserInterface.ui.MessageBox.information"),
            mock.patch("UserInterface.ui.QApplication.processEvents"),
            mock.patch("UserInterface.ui.Thread", return_value=thread),
        ):
            MainWindow.autofocus_clicked(window)

        self.assertTrue(window._autofocus_active)
        self.assertFalse(single_button.enabled)
        self.assertFalse(motor_up_button.enabled)
        self.assertFalse(motor_down_button.enabled)
        window.set_camera_lens_state.assert_called_once_with(False)
        self.assertIs(window._autofocus_thread, thread)
        thread.start.assert_called_once_with()

    def test_saved_focus_worker_uses_signals_for_gui_cleanup(self):
        finished = RecordingSignal()
        failed = RecordingSignal()
        window = types.SimpleNamespace(
            stop=False,
            axis_length=100.0,
            current_lens=types.SimpleNamespace(
                lenght=20.0,
                base_focus_height=10.0,
            ),
            move_motor_to=mock.Mock(),
            lens_reposition_finished_signal=finished,
            lens_reposition_error_signal=failed,
            set_motor_control_state=mock.Mock(),
        )

        with mock.patch.object(MessageBox, "hide_message") as hide_message:
            MainWindow.move_to_focus_hight(window)

        window.move_motor_to.assert_called_once_with(70.0, wait=True)
        self.assertEqual(failed.values, [])
        self.assertEqual(finished.values, [()])
        hide_message.assert_not_called()
        window.set_motor_control_state.assert_not_called()

    def test_saved_focus_move_is_tracked_and_blocks_capture_controls(self):
        lens = types.SimpleNamespace(
            name="Test lens",
            lenght=20.0,
            label_str="Test lens label",
            scale=None,
            settings=[1, 2, 3, 4, 5],
            base_focus_height=None,
            base_focus_height_arducam_old=9.0,
            base_focus_height_arducam=10.0,
            base_focus_height_vaimaging=11.0,
        )
        single_button = RecordingButton()
        single_button.enabled = True
        window = types.SimpleNamespace(
            current_lens=object(),
            current_device_name="Entomoscope PIs",
            camera_type="ArducamCamera",
            axis_length=100.0,
            lens_info_label=types.SimpleNamespace(setText=mock.Mock()),
            set_settings=mock.Mock(),
            set_new_limit=mock.Mock(),
            dump_lenses_to_file=mock.Mock(),
            approve_lens=mock.Mock(return_value=True),
            set_motor_control_state=mock.Mock(),
            set_camera_lens_state=mock.Mock(),
            _update_single_capture_button=mock.Mock(),
            move_to_focus_hight=mock.Mock(),
            motor_stop=mock.Mock(),
            take_single_picture_bttn=single_button,
            _lens_reposition_active=False,
            stop=True,
        )
        thread = mock.Mock()

        with (
            mock.patch(
                "UserInterface.ui.lens_scale_for_camera",
                return_value=0.01,
            ),
            mock.patch("UserInterface.ui.MessageBox.information"),
            mock.patch("UserInterface.ui.QApplication.processEvents"),
            mock.patch("UserInterface.ui.Thread", return_value=thread) as make_thread,
        ):
            MainWindow.set_current_lens(window, lens)

        self.assertTrue(window._lens_reposition_active)
        self.assertFalse(window.stop)
        self.assertIs(window._lens_reposition_thread, thread)
        make_thread.assert_called_once_with(
            target=window.move_to_focus_hight,
            args=(70.0,),
        )
        thread.start.assert_called_once_with()
        window.set_motor_control_state.assert_called_once_with(False)
        window.set_camera_lens_state.assert_called_once_with(False)
        window._update_single_capture_button.assert_called_once_with()

    def test_lens_change_is_rejected_before_mutation_during_image_operations(self):
        lens = types.SimpleNamespace(
            name="New lens",
            lenght=20.0,
            label_str="New lens label",
            scale=None,
            settings=[1, 2, 3, 4, 5],
            base_focus_height=None,
            base_focus_height_arducam_old=None,
            base_focus_height_arducam=None,
            base_focus_height_vaimaging=None,
        )

        for active_name in ("_autofocus_active", "_stack_operation_active"):
            with self.subTest(active_name=active_name):
                original_lens = object()
                state = {
                    "_autofocus_active": False,
                    "_stack_operation_active": False,
                    "_lens_reposition_active": False,
                    "_single_capture_active": False,
                    "_manual_move_active": False,
                }
                state[active_name] = True
                window = types.SimpleNamespace(
                    current_lens=original_lens,
                    current_device_name="Entomoscope PIs",
                    camera_type="ArducamCamera",
                    lens_info_label=types.SimpleNamespace(setText=mock.Mock()),
                    set_settings=mock.Mock(),
                    set_new_limit=mock.Mock(),
                    dump_lenses_to_file=mock.Mock(),
                    statusBar=types.SimpleNamespace(showMessage=mock.Mock()),
                    **state,
                )

                MainWindow.set_current_lens(window, lens)

                self.assertIs(window.current_lens, original_lens)
                window.set_settings.assert_not_called()
                window.set_new_limit.assert_not_called()
                window.dump_lenses_to_file.assert_not_called()
                window.statusBar.showMessage.assert_called_once()

    def test_single_capture_button_stays_disabled_during_saved_focus_move(self):
        single_button = RecordingButton()
        window = types.SimpleNamespace(
            device_selected=True,
            camera=object(),
            showing_static_image=False,
            _stack_operation_active=False,
            _autofocus_active=False,
            _lens_reposition_active=True,
            _single_capture_active=False,
            take_single_picture_bttn=single_button,
        )

        MainWindow._update_single_capture_button(window)

        self.assertFalse(single_button.enabled)

    def test_single_capture_button_stays_disabled_during_manual_stage_move(self):
        single_button = RecordingButton()
        window = types.SimpleNamespace(
            device_selected=True,
            camera=object(),
            showing_static_image=False,
            _stack_operation_active=False,
            _autofocus_active=False,
            _lens_reposition_active=False,
            _single_capture_active=False,
            _manual_move_active=True,
            take_single_picture_bttn=single_button,
        )

        MainWindow._update_single_capture_button(window)

        self.assertFalse(single_button.enabled)

    def test_saved_focus_finish_restores_previous_control_states(self):
        previously_disabled = RecordingButton()
        previously_disabled.enabled = True
        previously_enabled = RecordingButton()
        previously_enabled.enabled = False
        window = types.SimpleNamespace(
            _lens_reposition_active=True,
            _close_pending=False,
            _lens_reposition_control_states=[
                (previously_disabled, False),
                (previously_enabled, True),
            ],
            _update_single_capture_button=mock.Mock(),
        )

        with mock.patch.object(MessageBox, "hide_message"):
            MainWindow._finish_lens_reposition_ui(window)

        self.assertFalse(window._lens_reposition_active)
        self.assertFalse(previously_disabled.enabled)
        self.assertTrue(previously_enabled.enabled)
        self.assertEqual(window._lens_reposition_control_states, [])
        window._update_single_capture_button.assert_called_once_with()

    def test_saved_focus_completion_does_not_touch_widgets_during_shutdown(self):
        button = RecordingButton()
        button.enabled = False
        window = types.SimpleNamespace(
            _lens_reposition_active=True,
            _close_pending=True,
            _lens_reposition_control_states=[(button, True)],
            _update_single_capture_button=mock.Mock(),
        )

        with mock.patch.object(MessageBox, "hide_message") as hide_message:
            MainWindow._finish_lens_reposition_ui(window)

        self.assertFalse(window._lens_reposition_active)
        self.assertFalse(button.enabled)
        hide_message.assert_not_called()
        window._update_single_capture_button.assert_not_called()


class ManualStageCoordinationTests(unittest.TestCase):
    def test_manual_arrow_routes_through_coordinator_but_stack_moves_stay_synchronous(self):
        requested = []
        axis = types.SimpleNamespace(move_for=mock.Mock())
        window = types.SimpleNamespace(
            _stack_step_size=0.25,
            axis=axis,
            _request_manual_move=lambda distance: requested.append(distance),
        )

        MainWindow.motor_up(window)
        MainWindow.motor_down(window)

        self.assertEqual(requested, [-0.25, 0.25])
        axis.move_for.assert_not_called()

        MainWindow.motor_up(window, steps=0.5, wait=True)
        axis.move_for.assert_called_once_with(-0.5, wait=True)

    def test_manual_arrow_is_rejected_while_autofocus_is_stopping(self):
        start_move = mock.Mock()
        window = types.SimpleNamespace(
            _close_pending=False,
            _stack_operation_active=False,
            _autofocus_active=True,
            _lens_reposition_active=False,
            _single_capture_active=False,
            _manual_move_active=False,
            _start_manual_move=start_move,
            statusBar=types.SimpleNamespace(showMessage=mock.Mock()),
        )

        MainWindow._request_manual_move(window, 0.25)

        start_move.assert_not_called()
        window.statusBar.showMessage.assert_called_once()

    def test_manual_move_start_tracks_worker_and_blocks_capture_controls(self):
        buttons = [RecordingButton() for _ in range(8)]
        for button in buttons:
            button.enabled = True
        window = types.SimpleNamespace(
            _manual_move_active=False,
            _manual_move_control_states=[],
            _manual_move_worker=mock.Mock(),
            statusBar=types.SimpleNamespace(showMessage=mock.Mock()),
            take_single_picture_bttn=buttons[0],
            take_stack_picture_bttn=buttons[1],
            motor_up_button=buttons[2],
            motor_down_button=buttons[3],
            autofocus_button=buttons[4],
            motor_reference_button=buttons[5],
            cam_setting_bttn=buttons[6],
            change_lens_bttn=buttons[7],
            stop=True,
        )
        start_move = getattr(
            MainWindow,
            "_start_manual_move",
            lambda _self, _distance: None,
        )
        thread = mock.Mock()

        with mock.patch("UserInterface.ui.Thread", return_value=thread) as make_thread:
            start_move(window, -0.25)

        self.assertTrue(window._manual_move_active)
        self.assertFalse(window.stop)
        self.assertIs(window._manual_move_thread, thread)
        self.assertTrue(all(not button.enabled for button in buttons))
        make_thread.assert_called_once_with(
            target=window._manual_move_worker,
            args=(-0.25,),
        )
        thread.start.assert_called_once_with()

    def test_manual_move_worker_waits_for_axis_and_uses_qt_signals(self):
        finished = RecordingSignal()
        failed = RecordingSignal()
        axis = types.SimpleNamespace(move_for=mock.Mock(return_value=None))
        window = types.SimpleNamespace(
            axis=axis,
            manual_move_finished_signal=finished,
            manual_move_error_signal=failed,
        )
        worker = getattr(
            MainWindow,
            "_manual_move_worker",
            lambda _self, _distance: None,
        )

        worker(window, -0.25)

        axis.move_for.assert_called_once_with(-0.25, wait=True)
        self.assertEqual(failed.values, [])
        self.assertEqual(finished.values, [()])

    def test_manual_move_finish_restores_controls_outside_shutdown(self):
        button = RecordingButton()
        button.enabled = False
        window = types.SimpleNamespace(
            _manual_move_active=True,
            _close_pending=False,
            _manual_move_control_states=[(button, True)],
            _update_single_capture_button=mock.Mock(),
            statusBar=types.SimpleNamespace(showMessage=mock.Mock()),
            stop=True,
        )
        finish_move = getattr(
            MainWindow,
            "_finish_manual_move_ui",
            lambda _self: None,
        )

        finish_move(window)

        self.assertFalse(window._manual_move_active)
        self.assertTrue(button.enabled)
        self.assertFalse(window.stop)
        self.assertEqual(window._manual_move_control_states, [])
        window._update_single_capture_button.assert_called_once_with()

    def test_manual_move_completion_does_not_touch_widgets_during_shutdown(self):
        button = RecordingButton()
        button.enabled = False
        window = types.SimpleNamespace(
            _manual_move_active=True,
            _close_pending=True,
            _manual_move_control_states=[(button, True)],
            _update_single_capture_button=mock.Mock(),
            statusBar=types.SimpleNamespace(showMessage=mock.Mock()),
            stop=True,
        )
        finish_move = getattr(
            MainWindow,
            "_finish_manual_move_ui",
            lambda _self: None,
        )

        finish_move(window)

        self.assertFalse(window._manual_move_active)
        self.assertFalse(button.enabled)
        self.assertTrue(window.stop)
        self.assertEqual(window._manual_move_control_states, [])
        window._update_single_capture_button.assert_not_called()
        window.statusBar.showMessage.assert_not_called()


class StackUiSafetyTests(unittest.TestCase):
    def test_nonfatal_camera_mode_covers_pre_movement_and_is_restored_on_abort(self):
        events = []

        class Camera:
            controlled = False

            def begin_stack_capture(self):
                self.controlled = True
                events.append("begin")

            def end_stack_capture(self):
                self.controlled = False
                events.append("end")

        class Axis:
            z = 2.0

            def set_speed(self, _speed):
                return None

        class SpinBox:
            def value(self):
                return 50

        camera = Camera()
        window = type("Window", (), {})()
        window.stop = False
        window.axis = Axis()
        window.arducam = camera
        window.camera_type = "ArducamCamera"
        window.stack_speed_spinbox = SpinBox()
        window._stack_step_size = 0.1
        window._number_stacks = 1
        window.hide_message_signal = RecordingSignal()

        def motor_down(*, steps, wait):
            self.assertTrue(camera.controlled)
            events.append("down")

        def motor_up(*, wait):
            self.assertTrue(camera.controlled)
            events.append("up")
            window.stop = True

        window.motor_down = motor_down
        window.motor_up = motor_up
        done = RecordingSignal()
        failed = RecordingSignal()

        with (
            mock.patch("UserInterface.ui.constants.PRE_STACK_MOVEMENT_STEPS", 1),
            mock.patch("UserInterface.ui.constants.STACK_SETTLING_DELAY", 0),
            mock.patch("UserInterface.ui.sleep", return_value=None),
        ):
            MainWindow.capture_source_images_for_stacking(
                window,
                ["one.tiff"],
                done,
                failed,
            )

        self.assertEqual(events, ["begin", "down", "up", "end"])
        self.assertFalse(camera.controlled)
        self.assertEqual(done.values, [])
        self.assertEqual(len(failed.values), 1)
        self.assertIn("cancelled", failed.values[0][0])

    def test_second_stack_request_is_rejected_before_creating_paths(self):
        class StatusBar:
            def __init__(self):
                self.messages = []

            def showMessage(self, *values):
                self.messages.append(values)

        window = type("Window", (), {})()
        window._stack_operation_active = True
        window.statusBar = StatusBar()

        MainWindow.stack_pictures_clicked(window)

        self.assertEqual(len(window.statusBar.messages), 1)
        self.assertIn("already running", window.statusBar.messages[0][0])

    def test_abort_during_pre_movement_invalidates_reference_signal(self):
        movements = []

        class Axis:
            z = 2.0

            def set_speed(self, speed):
                movements.append(("speed", speed))

        class SpinBox:
            def value(self):
                return 50

        window = type("Window", (), {})()
        window.stop = False
        window.axis = Axis()
        window.stack_speed_spinbox = SpinBox()
        window._stack_step_size = 0.1
        window._number_stacks = 2
        window.hide_message_signal = RecordingSignal()
        window.motor_down = lambda *, steps, wait: movements.append(("down", steps))

        def motor_up(*, wait):
            movements.append(("up", 0.1))
            window.stop = True

        window.motor_up = motor_up
        done = RecordingSignal()
        failed = RecordingSignal()

        with (
            mock.patch("UserInterface.ui.constants.PRE_STACK_MOVEMENT_STEPS", 5),
            mock.patch("UserInterface.ui.constants.STACK_SETTLING_DELAY", 0),
            mock.patch("UserInterface.ui.sleep", return_value=None),
        ):
            MainWindow.capture_source_images_for_stacking(
                window,
                ["one.tiff", "two.tiff"],
                done,
                failed,
            )

        self.assertEqual(done.values, [])
        self.assertEqual(len(failed.values), 1)
        self.assertTrue(failed.values[0][1])
        self.assertIn("reference the axis", failed.values[0][0])
        self.assertEqual(
            [movement[0] for movement in movements].count("up"),
            1,
        )

    def test_cancellation_set_before_worker_start_prevents_all_stage_motion(self):
        movements = []
        cancelled = threading.Event()
        cancelled.set()

        class Axis:
            z = 2.0

            def set_speed(self, speed):
                movements.append(("speed", speed))

        window = type("Window", (), {})()
        window.stop = False
        window.axis = Axis()
        window.hide_message_signal = RecordingSignal()
        window.stack_speed_spinbox = mock.Mock()
        window._stack_step_size = 0.1
        window._number_stacks = 2
        window.motor_down = lambda **kwargs: movements.append(("down", kwargs))
        window.motor_up = lambda **kwargs: movements.append(("up", kwargs))
        done = RecordingSignal()
        failed = RecordingSignal()

        MainWindow.capture_source_images_for_stacking(
            window,
            ["one.tiff", "two.tiff"],
            done,
            failed,
            cancelled,
        )

        self.assertEqual(done.values, [])
        self.assertEqual(len(failed.values), 1)
        self.assertEqual(movements, [("speed", 2)])

    def test_close_waits_for_capture_worker_before_hardware_cleanup(self):
        class RunningThread:
            def is_alive(self):
                return True

        class StatusBar:
            def showMessage(self, message):
                self.message = message

        window = type("Window", (), {})()
        window._stack_capture_thread = RunningThread()
        window._active_stacker = None
        window._close_pending = False
        window.statusBar = StatusBar()
        window.close = mock.Mock()
        window.motor_stop = mock.Mock()
        event = mock.Mock()

        with mock.patch("UserInterface.ui.QMessageBox.information"), mock.patch(
            "UserInterface.ui.QTimer.singleShot"
        ) as scheduled:
            MainWindow.closeEvent(window, event)

        window.motor_stop.assert_called_once_with()
        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        scheduled.assert_called_once()
        self.assertTrue(window._close_pending)

    def test_close_waits_for_autofocus_worker_before_hardware_cleanup(self):
        class RunningThread:
            def is_alive(self):
                return True

        window = type("Window", (), {})()
        window._stack_capture_thread = None
        window._active_stacker = None
        window._autofocus_thread = RunningThread()
        window._lens_reposition_thread = None
        window._single_capture_thread = None
        window._close_pending = False
        window.statusBar = types.SimpleNamespace(showMessage=mock.Mock())
        window.close = mock.Mock()
        window.motor_stop = mock.Mock()
        window.axis = types.SimpleNamespace(__del__=mock.Mock())
        window.arducam = types.SimpleNamespace(stop=mock.Mock(), wait=mock.Mock())
        event = mock.Mock()

        with mock.patch("UserInterface.ui.QMessageBox.information"), mock.patch(
            "UserInterface.ui.QTimer.singleShot"
        ) as scheduled:
            MainWindow.closeEvent(window, event)

        window.motor_stop.assert_called_once_with()
        window.axis.__del__.assert_not_called()
        window.arducam.stop.assert_not_called()
        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        scheduled.assert_called_once()

    def test_close_waits_for_saved_focus_worker_before_hardware_cleanup(self):
        class RunningThread:
            def is_alive(self):
                return True

        window = type("Window", (), {})()
        window._stack_capture_thread = None
        window._active_stacker = None
        window._autofocus_thread = None
        window._lens_reposition_thread = RunningThread()
        window._single_capture_thread = None
        window._close_pending = False
        window.statusBar = types.SimpleNamespace(showMessage=mock.Mock())
        window.close = mock.Mock()
        window.motor_stop = mock.Mock()
        window.axis = types.SimpleNamespace(__del__=mock.Mock())
        window.arducam = types.SimpleNamespace(stop=mock.Mock(), wait=mock.Mock())
        event = mock.Mock()

        with mock.patch("UserInterface.ui.QMessageBox.information"), mock.patch(
            "UserInterface.ui.QTimer.singleShot"
        ) as scheduled:
            MainWindow.closeEvent(window, event)

        window.motor_stop.assert_called_once_with()
        window.axis.__del__.assert_not_called()
        window.arducam.stop.assert_not_called()
        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        scheduled.assert_called_once()

    def test_close_waits_for_single_capture_worker_before_hardware_cleanup(self):
        class RunningThread:
            def is_alive(self):
                return True

        window = type("Window", (), {})()
        window._stack_capture_thread = None
        window._active_stacker = None
        window._autofocus_thread = None
        window._lens_reposition_thread = None
        window._single_capture_thread = RunningThread()
        window._close_pending = False
        window.statusBar = types.SimpleNamespace(showMessage=mock.Mock())
        window.close = mock.Mock()
        window.motor_stop = mock.Mock()
        window.axis = types.SimpleNamespace(__del__=mock.Mock())
        window.arducam = types.SimpleNamespace(stop=mock.Mock(), wait=mock.Mock())
        event = mock.Mock()

        with mock.patch("UserInterface.ui.QMessageBox.information"), mock.patch(
            "UserInterface.ui.QTimer.singleShot"
        ) as scheduled:
            MainWindow.closeEvent(window, event)

        window.motor_stop.assert_called_once_with()
        window.axis.__del__.assert_not_called()
        window.arducam.stop.assert_not_called()
        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        scheduled.assert_called_once()

    def test_close_waits_for_manual_stage_worker_before_hardware_cleanup(self):
        class RunningThread:
            def is_alive(self):
                return True

        window = type("Window", (), {})()
        window._stack_capture_thread = None
        window._active_stacker = None
        window._autofocus_thread = None
        window._lens_reposition_thread = None
        window._single_capture_thread = None
        window._manual_move_thread = RunningThread()
        window._close_pending = False
        window.statusBar = types.SimpleNamespace(showMessage=mock.Mock())
        window.close = mock.Mock()
        window.motor_stop = mock.Mock()
        window.axis = types.SimpleNamespace(__del__=mock.Mock())
        window.arducam = types.SimpleNamespace(stop=mock.Mock(), wait=mock.Mock())
        event = mock.Mock()

        with mock.patch("UserInterface.ui.QMessageBox.information"), mock.patch(
            "UserInterface.ui.QTimer.singleShot"
        ) as scheduled:
            MainWindow.closeEvent(window, event)

        window.motor_stop.assert_called_once_with()
        window.axis.__del__.assert_not_called()
        window.arducam.stop.assert_not_called()
        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        scheduled.assert_called_once()

    def test_accepted_close_keeps_shutdown_protection_enabled(self):
        class SpinBox:
            def value(self):
                return 1

        window = type("Window", (), {})()
        window._stack_capture_thread = None
        window._active_stacker = None
        window._autofocus_thread = None
        window._lens_reposition_thread = None
        window._single_capture_thread = None
        window._manual_move_thread = None
        window._close_pending = False
        window.motor_stop = mock.Mock()
        window.axis = types.SimpleNamespace(__del__=mock.Mock())
        window.arducam = types.SimpleNamespace(stop=mock.Mock(), wait=mock.Mock())
        window.stack_speed_spinbox = SpinBox()
        window.number_stacks_spinbox = SpinBox()
        window.step_size_spinbox = SpinBox()
        window.directory_manager = types.SimpleNamespace(base_dir="test-output")
        event = mock.Mock()

        with mock.patch("UserInterface.ui.DETECT_POOL", None):
            MainWindow.closeEvent(window, event)

        event.accept.assert_called_once_with()
        self.assertTrue(window._close_pending)

    def test_invalid_reference_disables_motion_but_keeps_reference_available(self):
        window = type("Window", (), {})()
        window.referenced = True
        window.axis = type("Axis", (), {"_referenced": True})()
        window.motor_up_button = RecordingButton()
        window.motor_down_button = RecordingButton()
        window.autofocus_button = RecordingButton()
        window.take_stack_picture_bttn = RecordingButton()
        window.motor_reference_button = RecordingButton()

        MainWindow.invalidate_axis_reference(window)

        self.assertFalse(window.referenced)
        self.assertFalse(window.axis._referenced)
        self.assertFalse(window.motor_up_button.enabled)
        self.assertFalse(window.motor_down_button.enabled)
        self.assertFalse(window.autofocus_button.enabled)
        self.assertFalse(window.take_stack_picture_bttn.enabled)
        self.assertTrue(window.motor_reference_button.enabled)


if __name__ == "__main__":
    unittest.main()
