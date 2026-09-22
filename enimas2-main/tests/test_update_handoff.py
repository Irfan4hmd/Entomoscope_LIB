import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Tools import enimas_updater as updater
from Tools.update_manager import UpdateController


if os.name == "nt":
    windows_test_tmp = Path(os.environ.get("ENIMAS_TEST_TMP", r"C:\tmp"))
    windows_test_tmp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(windows_test_tmp)


class UpdateHandoffTests(unittest.TestCase):
    def _write_app_lock(self, paths, pid, marker):
        paths.updates.mkdir(parents=True, exist_ok=True)
        paths.app_lock.write_text(
            json.dumps({"pid": pid, "process_marker": marker}),
            encoding="utf-8",
        )

    def test_verified_stale_app_owner_is_finished(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = updater.UpdatePaths(Path(directory))
            self._write_app_lock(paths, 1234, "same-process")
            alive = [True]

            def is_alive(_pid):
                return alive[0]

            def terminate(_pid, exit_code=42, expected_marker=None):
                self.assertEqual(exit_code, 42)
                self.assertEqual(expected_marker, "same-process")
                alive[0] = False

            with mock.patch.object(updater, "pid_is_alive", side_effect=is_alive), mock.patch.object(
                updater, "process_start_marker", return_value="same-process"
            ), mock.patch.object(updater, "_terminate_process", side_effect=terminate) as mocked:
                updater._wait_for_update_handoff(
                    paths, 1234, orderly_timeout=0, forced_timeout=0
                )

            mocked.assert_called_once_with(
                1234, exit_code=42, expected_marker="same-process"
            )
            self.assertFalse(paths.app_lock.exists())

    def test_pid_reuse_or_unverified_owner_is_never_terminated(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = updater.UpdatePaths(Path(directory))
            self._write_app_lock(paths, 1234, "original-process")
            with mock.patch.object(updater, "pid_is_alive", return_value=True), mock.patch.object(
                updater, "process_start_marker", return_value="different-process"
            ), mock.patch.object(updater, "_terminate_process") as mocked:
                with self.assertRaisesRegex(updater.UpdateError, "could not be verified"):
                    updater._wait_for_update_handoff(
                        paths, 1234, orderly_timeout=0, forced_timeout=0
                    )
            mocked.assert_not_called()
            self.assertTrue(paths.app_lock.exists())

    def test_normal_exit_never_uses_forced_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = updater.UpdatePaths(Path(directory))
            with mock.patch.object(updater, "pid_is_alive", return_value=False), mock.patch.object(
                updater, "_terminate_process"
            ) as mocked:
                updater._wait_for_update_handoff(
                    paths, 1234, orderly_timeout=0, forced_timeout=0
                )
            mocked.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows process handoff integration test")
    def test_windows_verified_child_process_is_really_terminated(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            marker = updater.process_start_marker(child.pid)
            self.assertTrue(marker)
            with tempfile.TemporaryDirectory() as directory:
                paths = updater.UpdatePaths(Path(directory))
                self._write_app_lock(paths, child.pid, marker)
                updater._wait_for_update_handoff(
                    paths, child.pid, orderly_timeout=0, forced_timeout=2
                )
                self.assertFalse(paths.app_lock.exists())
            child.wait(timeout=5)
            self.assertFalse(updater.pid_is_alive(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows process identity integration test")
    def test_windows_termination_rechecks_marker_on_open_handle(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            with self.assertRaisesRegex(updater.UpdateError, "identity changed"):
                updater._terminate_process(
                    child.pid, exit_code=42, expected_marker="not-this-process"
                )
            self.assertTrue(updater.pid_is_alive(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    def test_manual_bootstrap_keeps_non_forcing_wait(self):
        source = inspect.getsource(updater.bootstrap_update)
        self.assertIn("_wait_for_pid(app_pid, timeout=300)", source)
        self.assertNotIn("_wait_for_update_handoff", source)


class MainUpdateExitTests(unittest.TestCase):
    def test_update_exit_releases_lock_before_repeat_global_cleanup(self):
        source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
        update_exit = source.index("if rtn == 42:")
        release_lock = source.index("release_app_lock(install_root)", update_exit)
        immediate_exit = source.index("os._exit(42)", release_lock)
        repeat_cleanup = source.index("cleanup_resources()", immediate_exit)
        self.assertLess(update_exit, release_lock)
        self.assertLess(release_lock, immediate_exit)
        self.assertLess(immediate_exit, repeat_cleanup)


class UpdateBannerTests(unittest.TestCase):
    def test_banner_is_high_contrast_and_explicit(self):
        init_source = inspect.getsource(UpdateController.__init__)
        available_source = inspect.getsource(UpdateController._check_succeeded)
        self.assertIn("background-color:#ffbf00", init_source)
        self.assertIn("font-weight:700", init_source)
        self.assertIn("border:2px solid #6b3d00", init_source)
        self.assertIn("setMinimumHeight(30)", init_source)
        self.assertIn("UPDATE AVAILABLE: ENIMAS", available_source)


if __name__ == "__main__":
    unittest.main()