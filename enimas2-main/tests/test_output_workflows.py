import csv
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.modules.setdefault("ArducamSDK", types.ModuleType("ArducamSDK"))

from PyQt5.QtWidgets import QApplication, QWidget

import constants
import main
from UserInterface.Image_save_settings import ImageSettingsDialog
from UserInterface.method_selection import MethodDialog
from UserInterface.ui import DirectoryManager, Lens, LensDialog, MainWindow
from plugins.stack_export.plugin import (
    Plugin as StackExportPlugin,
    scan_exclusion_root,
)
from Tools.image_output import (
    ScaleBarError,
    add_scale_bar,
    calculate_mm_per_pixel,
    choose_scale_bar,
    convert_image_file,
    normalize_image_extension,
)
from Tools.stack_export import (
    StackExportError,
    discover_stacked_images,
    export_stacked_images,
    safe_export_stem,
    sanitize_site_id,
)


class ImageOutputTests(unittest.TestCase):
    def test_final_extension_is_canonical_and_restricted(self):
        self.assertEqual(normalize_image_extension(".TIFF"), "tiff")
        self.assertEqual(normalize_image_extension("jpeg"), "jpg")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            normalize_image_extension("png")

    def test_calibration_requires_positive_finite_values(self):
        self.assertAlmostEqual(calculate_mm_per_pixel(2.0, 800), 0.0025)
        for known, pixels in ((0, 10), (1, 0), (float("nan"), 10)):
            with self.subTest(known=known, pixels=pixels):
                with self.assertRaises(ScaleBarError):
                    calculate_mm_per_pixel(known, pixels)

    def test_scale_bar_refuses_16_bit_rgb_tiff_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sixteen_bit_rgb.tiff"
            self.assertTrue(
                cv2.imwrite(
                    str(path),
                    numpy.full((50, 80, 3), 32000, dtype=numpy.uint16),
                )
            )
            with Image.open(path) as opened:
                self.assertEqual(opened.mode, "RGB")
                self.assertGreater(max(opened.tag_v2.get(258)), 8)
            before = path.read_bytes()
            with self.assertRaisesRegex(ScaleBarError, "16-bit RGB"):
                add_scale_bar(path, 0.0025)
            self.assertEqual(path.read_bytes(), before)

    def test_scale_bar_uses_physical_only_nice_label(self):
        spec = choose_scale_bar(4000, 0.003)
        self.assertIn(spec.length_mm, {1.0, 2.0, 5.0})
        self.assertEqual(spec.label, f"{spec.length_mm:g} mm")
        self.assertNotIn("px", spec.label)
        self.assertGreaterEqual(spec.length_px, 4000 * 0.08)
        self.assertLessEqual(spec.length_px, 4000 * 0.32)

    def test_scale_bar_can_be_saved_to_jpeg_and_tiff(self):
        with tempfile.TemporaryDirectory() as directory:
            for suffix in (".jpg", ".tiff"):
                with self.subTest(suffix=suffix):
                    path = Path(directory) / f"stacked{suffix}"
                    Image.new("RGB", (1000, 700), "gray").save(path)
                    spec = add_scale_bar(path, 0.0025)
                    self.assertTrue(path.is_file())
                    self.assertEqual(spec.label, "0.5 mm")
                    with Image.open(path) as result:
                        self.assertEqual(result.size, (1000, 700))
                        self.assertEqual(result.mode, "RGB")

    def test_scale_bar_refuses_16_bit_tiff_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sixteen_bit.tiff"
            image = Image.fromarray(
                numpy.full((50, 80), 32000, dtype=numpy.uint16), mode="I;16"
            )
            image.save(path)
            before = path.read_bytes()
            with self.assertRaisesRegex(ScaleBarError, "bit depth"):
                add_scale_bar(path, 0.0025)
            self.assertEqual(path.read_bytes(), before)

    def test_tiff_can_be_converted_to_database_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.tiff"
            destination = Path(directory) / "destination.jpg"
            Image.new("RGB", (60, 40), (10, 20, 30)).save(source)
            convert_image_file(source, destination)
            self.assertTrue(destination.is_file())
            with Image.open(destination) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (60, 40))


class SessionOutputIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_method_dialog_sets_per_session_final_format(self):
        previous_method = constants.LAST_FUSE_METHOD
        previous_format = constants.STACK_OUTPUT_EXTENSION
        try:
            constants.LAST_FUSE_METHOD = 1
            dialog = MethodDialog()
            dialog.selfmade_radio_bttn.setChecked(True)
            dialog.output_format_combo.setCurrentIndex(
                dialog.output_format_combo.findData("jpg")
            )
            dialog.ok_clicked()
            self.assertEqual(constants.STACK_OUTPUT_EXTENSION, "jpg")
            self.assertEqual(constants.IMG_EXTENSION, "tiff")
        finally:
            constants.LAST_FUSE_METHOD = previous_method
            constants.STACK_OUTPUT_EXTENSION = previous_format

    def test_main_save_config_loads_output_preferences_before_main_window_construction(self):
        previous_extension = constants.STACK_OUTPUT_EXTENSION
        previous_scalebar = constants.SHOW_SCALEBAR
        try:
            main.save_config({"stack-output-extension": "JPG", "show-scalebar": "true"})

            self.assertEqual(constants.STACK_OUTPUT_EXTENSION, "jpg")
            window = MainWindow(None, None, None)
            self.assertTrue(window.show_scalebar)
            window.hide()
            window.deleteLater()
        finally:
            constants.STACK_OUTPUT_EXTENSION = previous_extension
            constants.SHOW_SCALEBAR = previous_scalebar

    def test_main_write_config_stores_canonical_output_preferences_and_preserves_other_keys(self):
        previous_extension = constants.STACK_OUTPUT_EXTENSION
        previous_scalebar = constants.SHOW_SCALEBAR
        previous_cwd = os.getcwd()
        try:
            constants.STACK_OUTPUT_EXTENSION = "JPG"
            constants.SHOW_SCALEBAR = True
            with tempfile.TemporaryDirectory() as directory:
                try:
                    os.chdir(directory)
                    main.write_config({"unrelated": "preserved"})
                    written = main.read_config()
                finally:
                    os.chdir(previous_cwd)

            self.assertEqual(written["unrelated"], "preserved")
            self.assertEqual(written.get("stack-output-extension"), "jpg")
            self.assertEqual(written.get("show-scalebar"), "true")
        finally:
            os.chdir(previous_cwd)
            constants.STACK_OUTPUT_EXTENSION = previous_extension
            constants.SHOW_SCALEBAR = previous_scalebar

    def test_image_settings_acceptance_updates_scale_bar_runtime_preference(self):
        previous_scalebar = constants.SHOW_SCALEBAR
        main_window = types.SimpleNamespace(
            show_scalebar=False,
            current_scale=0.0025,
            save_uniform_bg=False,
            cropping=False,
            replace_color=None,
            cropping_method=None,
            proc_var_progress_label=QWidget(),
        )
        try:
            dialog = ImageSettingsDialog(main_window)
            main_window.proc_var_progress_label = dialog.statusLabel
            dialog.scalebar_checkbox.setChecked(True)
            dialog.on_ok()

            self.assertTrue(main_window.show_scalebar)
            self.assertTrue(constants.SHOW_SCALEBAR)
        finally:
            constants.SHOW_SCALEBAR = previous_scalebar

    def test_cancelled_image_settings_leave_scale_bar_runtime_preference_unchanged(self):
        previous_scalebar = constants.SHOW_SCALEBAR
        constants.SHOW_SCALEBAR = True
        main_window = types.SimpleNamespace(show_scalebar=True)
        try:
            dialog = ImageSettingsDialog(main_window)
            dialog.scalebar_checkbox.setChecked(False)
            dialog.reject()

            self.assertTrue(main_window.show_scalebar)
            self.assertTrue(constants.SHOW_SCALEBAR)
        finally:
            constants.SHOW_SCALEBAR = previous_scalebar

    def test_stack_paths_keep_raw_tiff_and_use_selected_final_extension(self):
        previous_format = constants.STACK_OUTPUT_EXTENSION
        try:
            constants.STACK_OUTPUT_EXTENSION = "jpg"
            with tempfile.TemporaryDirectory() as directory:
                manager = DirectoryManager.__new__(DirectoryManager)
                manager.get_current_folder_name = lambda create_dir=True: (
                    directory,
                    "specimen",
                )
                frames, frame_dir, final = manager.add_stack_image(3)
                self.assertTrue(all(path.endswith(".tiff") for path in frames))
                self.assertTrue(final.endswith("specimen_stacked_01.jpg"))
                self.assertTrue(Path(frame_dir).is_dir())
        finally:
            constants.STACK_OUTPUT_EXTENSION = previous_format

    def test_existing_final_without_raw_folder_advances_stack_number(self):
        previous_format = constants.STACK_OUTPUT_EXTENSION
        try:
            constants.STACK_OUTPUT_EXTENSION = "jpg"
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, "specimen_stacked_07.tiff").write_bytes(b"existing")
                manager = DirectoryManager.__new__(DirectoryManager)
                manager.get_current_folder_name = lambda create_dir=True: (
                    directory,
                    "specimen",
                )
                frames, _, final = manager.add_stack_image(2, create_dir=False)
                self.assertTrue(final.endswith("specimen_stacked_08.jpg"))
                self.assertTrue(all("Frame_08_" in path for path in frames))
                self.assertEqual(
                    Path(directory, "specimen_stacked_07.tiff").read_bytes(),
                    b"existing",
                )
        finally:
            constants.STACK_OUTPUT_EXTENSION = previous_format

    def test_cancelled_lens_dialog_does_not_apply_scale_edit(self):
        lens = Lens("Test lens", 100, [1], scale_vaimaging=0.003, scale_arducam=0.002)
        main = QWidget()
        main.all_lenses = [lens]
        main.current_lens = lens
        main.camera_type = "VAImagingCamera"
        main.default_cam_values = [1]
        dialog = LensDialog(main)
        dialog.scale_edit.setText("0.009")
        dialog.sclae_changed()
        self.assertEqual(dialog.all_lenses[0].scale_vaimaging, 0.009)
        dialog.close()
        self.assertEqual(main.all_lenses[0].scale_vaimaging, 0.003)

    def test_scan_excludes_only_destination_inside_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            descendant = source / "transfer"
            ancestor = root
            self.assertEqual(
                scan_exclusion_root(str(source), str(descendant)),
                str(descendant.resolve()),
            )
            self.assertEqual(scan_exclusion_root(str(source), str(ancestor)), "")
            self.assertEqual(scan_exclusion_root(str(source), str(source)), "")

    def test_plugin_remains_busy_until_qthread_finished(self):
        plugin = StackExportPlugin()
        plugin.run({"input": None})

        class FinishedWorker:
            deleted = False

            def deleteLater(self):
                self.deleted = True

        worker = FinishedWorker()
        try:
            plugin.worker = worker
            plugin._set_busy(True, scanning=True)
            plugin._scan_complete([Path("sample_stacked_01.tiff")])
            self.assertTrue(plugin.busy)
            self.assertFalse(plugin.scan_button.isEnabled())
            plugin._worker_finished()
            self.assertFalse(plugin.busy)
            self.assertTrue(plugin.scan_button.isEnabled())
            self.assertTrue(worker.deleted)
        finally:
            plugin.window.close()

    def test_export_plugin_ui_can_be_constructed(self):
        plugin = StackExportPlugin()
        plugin.run({"input": None})
        try:
            self.assertEqual(plugin.name, "Stacked Image Export")
            self.assertEqual(plugin.format_combo.currentData(), "keep")
            self.assertFalse(plugin.export_button.isEnabled())
        finally:
            plugin.window.close()


class StackExportTests(unittest.TestCase):
    @staticmethod
    def _image(path, color):
        path.parent.mkdir(parents=True, exist_ok=True)
        image = numpy.full((20, 30, 3), color, dtype=numpy.uint8)
        if not cv2.imwrite(str(path), image):
            raise AssertionError(f"Could not create fixture {path}")

    def test_discovery_selects_only_final_stacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final_one = root / "A" / "sample_stacked_01.tiff"
            final_two = root / "B" / "sample_stacked_2.JPG"
            self._image(final_one, 10)
            self._image(final_two, 20)
            self._image(root / "sample_stacked_01_cropped.tiff", 30)
            self._image(root / "sample_001.tiff", 40)
            self._image(
                root / "sample_Stack_Frames_01" / "sample_stacked_99.tiff", 50
            )
            export_root = root / "transfer"
            self._image(export_root / "old_stacked_03.jpg", 60)

            found = discover_stacked_images(root, exclude_root=export_root)

            self.assertEqual(found, [final_one.resolve(), final_two.resolve()])

    def test_export_converts_prefixes_and_never_overwrites_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "one" / "sample_stacked_01.tiff"
            second = root / "two" / "sample_stacked_01.tiff"
            self._image(first, 30)
            self._image(second, 220)
            output = root / "transfer"

            summary = export_stacked_images(
                [first, second],
                output,
                output_format="jpeg",
                site_id="School 07 / North",
            )

            self.assertEqual(summary.exported, 2)
            self.assertEqual(summary.failed, 0)
            outputs = sorted(output.glob("School_07_North__sample_stacked_01*.jpg"))
            self.assertEqual(len(outputs), 2)
            self.assertTrue(any("__2.jpg" in path.name for path in outputs))
            self.assertTrue(summary.manifest_path.is_file())
            with summary.manifest_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["status"] for row in rows], ["exported", "exported"])

            repeated = export_stacked_images(
                [first, second], output, output_format="jpeg", site_id="School 07 / North"
            )
            self.assertEqual(repeated.exported, 0)
            self.assertEqual(repeated.skipped, 2)
            self.assertEqual(
                len(list(output.glob("School_07_North__sample_stacked_01*.jpg"))),
                2,
            )

    def test_cancellation_keeps_completed_files_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = []
            for index in range(3):
                path = root / f"item_stacked_{index + 1:02}.tiff"
                self._image(path, index * 40)
                images.append(path)
            progress_calls = []

            summary = export_stacked_images(
                images,
                root / "transfer",
                progress=lambda current, total, name: progress_calls.append(current),
                cancelled=lambda: bool(progress_calls),
            )

            self.assertTrue(summary.cancelled)
            self.assertEqual(summary.exported, 1)
            self.assertTrue(summary.manifest_path.is_file())

    def test_source_removed_after_scan_is_reported_without_aborting_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing_stacked_01.tiff"
            summary = export_stacked_images([missing], root / "transfer")
            self.assertEqual(summary.exported, 0)
            self.assertEqual(summary.failed, 1)
            self.assertTrue(summary.manifest_path.is_file())
            self.assertIn("Source file is missing", summary.records[0].message)

    def test_long_but_valid_windows_name_exports_with_short_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Keep the source component well below the Windows filename limit;
            # the long site ID still forces the exported stem to be shortened.
            source = root / (("x" * 140) + "_stacked_01.tiff")
            self._image(source, 75)
            output = root / "transfer"
            summary = export_stacked_images(
                [source], output, output_format="keep", site_id="S" * 80
            )
            self.assertEqual(summary.exported, 1, summary.records)
            exported = Path(summary.records[0].exported)
            self.assertTrue(exported.is_file())
            self.assertLessEqual(len(exported.name), 225)
            self.assertEqual(list(output.glob(".enimas-*")), [])

    def test_export_stem_is_bounded_and_stable(self):
        first = safe_export_stem("S" * 80, "specimen_" + "x" * 240)
        second = safe_export_stem("S" * 80, "specimen_" + "x" * 240)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 220)
        self.assertRegex(first, r"__[0-9a-f]{10}$")

    def test_site_id_is_path_safe(self):
        self.assertEqual(sanitize_site_id(" ../School: 4/West "), "School_4_West")

    def test_invalid_output_format_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(StackExportError):
                export_stacked_images([], directory, output_format="png")


if __name__ == "__main__":
    unittest.main()
