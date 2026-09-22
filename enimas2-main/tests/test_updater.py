import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from Tools import build_update_manifest
from Tools import enimas_updater as updater
from Tools import health_check


if os.name == "nt":
    # The desktop sandbox exposes C:\tmp as the disposable writable test volume.
    windows_test_tmp = Path(os.environ.get("ENIMAS_TEST_TMP", r"C:\tmp"))
    windows_test_tmp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(windows_test_tmp)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_entry(path: str, destination: str, data: bytes) -> dict:
    return {
        "path": path,
        "destination": destination,
        "size": len(data),
        "sha256": digest(data),
    }


def make_manifest(
    entries=None,
    *,
    version="1.3.0",
    removed=None,
    requirements_hash="",
    system_files=None,
    archive_files=None,
):
    updater_data = b"standalone updater"
    files = list(entries or [])
    if not any(entry["path"] == "Tools/enimas_updater.py" for entry in files):
        files.append(
            file_entry(
                "Tools/enimas_updater.py",
                "src/Tools/enimas_updater.py",
                updater_data,
            )
        )
    updater_entry = next(entry for entry in files if entry["path"] == "Tools/enimas_updater.py")
    system_entries = list(system_files or [])
    if archive_files is None:
        by_path = {entry["path"]: {key: entry[key] for key in ("path", "size", "sha256")} for entry in files}
        for entry in system_entries:
            by_path[entry["path"]] = {key: entry[key] for key in ("path", "size", "sha256")}
        archive_files = list(by_path.values())
    return {
        "schema_version": 1,
        "version": version,
        "source_ref": f"v{version}",
        "repository_raw_url": "https://example.invalid/project/-/raw/{ref}",
        "release_date": "2026-07-20",
        "release_notes": "Safe updater test release",
        "requirements_sha256": requirements_hash,
        "system_update_required": False,
        "updater": {
            "path": "Tools/enimas_updater.py",
            "sha256": updater_entry["sha256"],
        },
        "files": files,
        "archive_files": list(archive_files),
        "system_files": system_entries,
        "removed": list(removed or []),
    }


def stage_external_updater(paths: updater.UpdatePaths, plan: dict) -> None:
    updater_payload = b"standalone updater"
    staged = (
        paths.staging
        / plan["target_version"]
        / "files"
        / "src"
        / "Tools"
        / "enimas_updater.py"
    )
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(updater_payload)
    if not any(
        operation.get("destination", "").casefold()
        == "src/tools/enimas_updater.py"
        for operation in plan["operations"]
    ):
        destination = paths.install_root / "src" / "Tools" / "enimas_updater.py"
        plan["operations"].append(
            {
                "kind": "replace" if destination.exists() else "add",
                "path": "Tools/enimas_updater.py",
                "destination": "src/Tools/enimas_updater.py",
                "sha256": digest(updater_payload),
                "size": len(updater_payload),
                "locally_modified": False,
                "status": "pending",
            }
        )
    candidate = paths.bin / plan["target_version"] / "enimas_updater.py"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(updater_payload)
    plan["external_updater"] = str(candidate)


def make_legacy_install(root: Path, version: str = "1.0.7") -> Path:
    """Create the known pre-manifest ENIMAS layout used by 1.0.7-1.2.x."""

    required_payloads = {
        "ENIMAS.bat": b"@echo off\r\n",
        "src/config.txt": f"version={version}\nbase-directory=C:\\Users\\test\\Pictures\\ENIMAS\n".encode(),
        "src/constants.py": b"# legacy release; config.txt owns the version\n",
        "src/main.py": b"print('legacy main')\n",
        "src/requirements.txt": b"example-package==1.0\n",
        "src/UserInterface/ui.py": b"# legacy UI\n",
        "src/venv/Scripts/python.exe": b"legacy venv python marker",
    }
    for relative, data in required_payloads.items():
        destination = root / Path(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return root


def stage_plan_payloads(
    paths: updater.UpdatePaths,
    plan: dict,
    release_payloads: dict[str, bytes],
) -> Path:
    stage = paths.staging / plan["target_version"]
    for operation in plan["operations"]:
        if operation["kind"] == "remove":
            continue
        data = release_payloads[operation["path"]]
        destination = stage / "files" / Path(*operation["destination"].split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    stage_external_updater(paths, plan)
    plan["phase"] = "ready"
    plan_path = stage / "plan.json"
    updater.atomic_write_json(plan_path, plan)
    return plan_path


class ManifestAndPlanningTests(unittest.TestCase):
    def test_version_comparison_is_semantic(self):
        self.assertTrue(updater.is_newer_version("1.10.0", "1.9.9"))
        self.assertFalse(updater.is_newer_version("1.2.4", "1.2.4"))
        self.assertFalse(updater.is_newer_version("1.2.4", "1.3.0"))
        for invalid in ("1.2", "v1.2.3", "1.2.3-beta", "one.two.three"):
            with self.subTest(invalid=invalid), self.assertRaises(updater.ManifestError):
                updater.parse_version(invalid)

    def test_known_legacy_versions_are_supported_without_installed_manifest(self):
        for version in ("1.0.7", "1.1.0", "1.2.0", "1.2.4"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                root = make_legacy_install(Path(directory), version)
                self.assertEqual(
                    updater.validate_incremental_installation(root),
                    version,
                )

    def test_older_incomplete_and_manifest_era_installs_require_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_legacy_install(Path(directory), "1.0.6")
            with self.assertRaisesRegex(updater.RepairRequiredError, "older than"):
                updater.validate_incremental_installation(root)
            self.assertEqual(
                updater.main(["--install-root", str(root), "migration-status"]),
                updater.EXIT_REPAIR_REQUIRED,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = make_legacy_install(Path(directory), "1.0.7")
            (root / "src" / "UserInterface" / "ui.py").unlink()
            with self.assertRaisesRegex(updater.RepairRequiredError, "incomplete"):
                updater.validate_incremental_installation(root)

        with tempfile.TemporaryDirectory() as directory:
            root = make_legacy_install(Path(directory), "1.3.0")
            with self.assertRaisesRegex(updater.RepairRequiredError, "release record"):
                updater.validate_incremental_installation(root)

    def test_manifest_era_install_requires_matching_release_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_legacy_install(Path(directory), "1.3.0")
            manifest = make_manifest(version="1.3.0")
            paths = updater.UpdatePaths(root)
            paths.updates.mkdir(parents=True)
            updater.atomic_write_json(paths.installed_manifest, manifest)
            self.assertEqual(updater.validate_incremental_installation(root), "1.3.0")
            (root / "src" / "config.txt").write_text("version=1.3.1\n", encoding="utf-8")
            with self.assertRaisesRegex(updater.RepairRequiredError, "does not match"):
                updater.validate_incremental_installation(root)

    def test_unsupported_legacy_bootstrap_stops_before_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_legacy_install(Path(directory), "1.0.6")
            with (
                mock.patch.object(updater, "fetch_manifest", return_value=make_manifest()),
                mock.patch.object(updater, "prepare_update") as prepare,
            ):
                with self.assertRaises(updater.RepairRequiredError):
                    updater.bootstrap_update("https://example.invalid/manifest.json", root)
            prepare.assert_not_called()

    def test_manifest_rejects_missing_unsafe_duplicate_and_wrong_updater(self):
        valid = make_manifest()
        updater.validate_manifest(valid)
        cpu_index = json.loads(json.dumps(valid))
        cpu_index["pip_download_args"] = list(updater.ALLOWED_PIP_DOWNLOAD_ARGS)
        updater.validate_manifest(cpu_index)
        unsafe_index = json.loads(json.dumps(valid))
        unsafe_index["pip_download_args"] = [
            "--extra-index-url",
            "https://example.invalid/untrusted",
        ]
        with self.assertRaisesRegex(updater.ManifestError, "PyTorch CPU wheel index"):
            updater.validate_manifest(unsafe_index)
        missing = dict(valid)
        missing.pop("files")
        with self.assertRaises(updater.ManifestError):
            updater.validate_manifest(missing)
        unsafe = json.loads(json.dumps(valid))
        unsafe["files"][0]["destination"] = "../outside.py"
        with self.assertRaises(updater.ManifestError):
            updater.validate_manifest(unsafe)
        duplicate = json.loads(json.dumps(valid))
        duplicate["files"].append(dict(duplicate["files"][0]))
        with self.assertRaises(updater.ManifestError):
            updater.validate_manifest(duplicate)
        wrong = json.loads(json.dumps(valid))
        wrong["updater"]["sha256"] = "0" * 64
        with self.assertRaises(updater.ManifestError):
            updater.validate_manifest(wrong)
        preserved = json.loads(json.dumps(valid))
        preserved["files"].append(file_entry("config.txt", "src/config.txt", b"bad"))
        with self.assertRaises(updater.ManifestError):
            updater.validate_manifest(preserved)
        case_alias = json.loads(json.dumps(valid))
        case_alias["files"].append(
            file_entry("Tools/other.py", "SRC/tools/ENIMAS_UPDATER.PY", b"alias")
        )
        with self.assertRaises(updater.ManifestError):
            updater.validate_manifest(case_alias)
        unsafe_system = json.loads(json.dumps(valid))
        unsafe_system["system_files"] = [
            {"path": "Tools/admin.exe", "size": 3, "sha256": digest(b"bad")}
        ]
        with self.assertRaises(updater.ManifestError):
            updater.validate_manifest(unsafe_system)
        collision = json.loads(json.dumps(valid))
        collision["removed"] = ["src/example.enimas-update-tombstone"]
        with self.assertRaises(updater.ManifestError):
            updater.validate_manifest(collision)

    def test_system_maintenance_manifest_is_rejected_by_normal_prepare(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            manifest = make_manifest()
            manifest["system_update_required"] = True
            with self.assertRaises(updater.MaintenanceRequiredError):
                updater.prepare_update(manifest, root)

    def test_stale_prepared_system_maintenance_plan_is_rejected_before_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "constants.py").write_text(
                'APP_VERSION = "1.2.4"\n', encoding="utf-8"
            )
            paths = updater.UpdatePaths(root)
            stage = paths.staging / "1.3.0"
            stage.mkdir(parents=True)
            manifest = make_manifest()
            manifest["system_update_required"] = True
            plan = {
                "schema_version": 1,
                "target_version": "1.3.0",
                "current_version": "1.2.4",
                "manifest": manifest,
                "operations": [],
                "dependencies_changed": False,
                "phase": "ready",
            }
            plan_path = stage / "plan.json"
            updater.atomic_write_json(plan_path, plan)
            with self.assertRaises(updater.MaintenanceRequiredError):
                updater.apply_update(plan_path, root)
            self.assertFalse(paths.journal.exists())

    def test_diff_hashes_live_files_and_preserves_unknown_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "constants.py").write_text('APP_VERSION = "1.2.4"\n', encoding="utf-8")
            target = b"released code"
            managed = root / "src" / "managed.py"
            managed.write_bytes(target)
            unknown = root / "src" / "custom-user-file.txt"
            unknown.write_bytes(b"preserve")
            manifest = make_manifest([file_entry("managed.py", "src/managed.py", target)])
            plan = updater.calculate_update_plan(manifest, root)
            self.assertFalse(any(op["destination"] == "src/managed.py" for op in plan["operations"]))
            managed.write_bytes(b"modified code")
            modified_manifest = make_manifest(
                [file_entry("managed.py", "src/managed.py", b"modified codf")]
            )
            plan = updater.calculate_update_plan(modified_manifest, root)
            self.assertTrue(any(op["destination"] == "src/managed.py" for op in plan["operations"]))
            self.assertTrue(unknown.exists())

    def test_only_explicit_or_previously_managed_files_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "old.py").write_bytes(b"old")
            (root / "src" / "custom.py").write_bytes(b"custom")
            old = make_manifest([file_entry("old.py", "src/old.py", b"old")], version="1.2.4")
            new = make_manifest()
            plan = updater.calculate_update_plan(new, root, old)
            removals = {op["destination"] for op in plan["operations"] if op["kind"] == "remove"}
            self.assertIn("src/old.py", removals)
            self.assertNotIn("src/custom.py", removals)

    def test_case_only_destination_change_does_not_schedule_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "Name.py").write_bytes(b"same")
            old = make_manifest(
                [file_entry("Name.py", "src/Name.py", b"same")],
                version="1.2.4",
            )
            new = make_manifest([file_entry("name.py", "src/name.py", b"same")])
            plan = updater.calculate_update_plan(new, root, old)
            removals = [op for op in plan["operations"] if op["kind"] == "remove"]
            self.assertEqual(removals, [])

    def test_destination_rejects_an_existing_reparse_component(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "link").mkdir(parents=True)
            with mock.patch.object(
                updater,
                "_is_reparse_point",
                side_effect=lambda path: path.name == "link",
            ):
                with self.assertRaises(updater.ManifestError):
                    updater.safe_destination(root, "src/link/managed.py")

    def test_manifest_builder_preservation_rules_and_changelog_selection(self):
        self.assertFalse(build_update_manifest.is_managed("config.txt"))
        self.assertFalse(build_update_manifest.is_managed("models/classification/custom.pt"))
        self.assertFalse(build_update_manifest.is_managed("camera-driver/vendor/setup.exe"))
        self.assertTrue(build_update_manifest.is_system_file("camera-driver/vendor/setup.exe"))
        self.assertTrue(build_update_manifest.is_managed("Tools/enimas_updater.py"))
        self.assertIn(
            "src/git/Git-2.50.1-64-bit.exe",
            build_update_manifest.LEGACY_REMOVALS,
        )
        self.assertIn("src/update.bat", build_update_manifest.LEGACY_REMOVALS)
        changelog = "## [1.3.0] - 2026-07-20\nnew\n\n## [1.2.4]\nold\n"
        self.assertIn("new", build_update_manifest.extract_release_notes(changelog, "1.3.0"))
        self.assertNotIn("old", build_update_manifest.extract_release_notes(changelog, "1.3.0"))
        pointer = (
            b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:" + b"a" * 64 + b"\nsize 123\n"
        )
        self.assertEqual(build_update_manifest.lfs_pointer_metadata(pointer), ("a" * 64, 123))

    def test_manifest_builder_materializes_and_verifies_lfs_payload(self):
        payload = b"hydrated LFS payload"
        pointer = (
            b"version https://git-lfs.github.com/spec/v1\n"
            + b"oid sha256:"
            + digest(payload).encode("ascii")
            + b"\nsize "
            + str(len(payload)).encode("ascii")
            + b"\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "driver.bin").write_bytes(payload)
            git_path_result = subprocess.CompletedProcess([], 1, stdout="", stderr="")
            with mock.patch.object(
                build_update_manifest, "committed_blob_bytes", return_value=pointer
            ), mock.patch.object(
                build_update_manifest.subprocess, "run", return_value=git_path_result
            ):
                self.assertEqual(
                    build_update_manifest.committed_file_bytes(root, "driver.bin"),
                    payload,
                )
                (root / "driver.bin").write_bytes(b"wrong")
                with self.assertRaises(RuntimeError):
                    build_update_manifest.committed_file_bytes(root, "driver.bin")

    def test_health_check_runs_runtime_import_smoke_and_propagates_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "UserInterface").mkdir()
            (root / "Tools").mkdir()
            (root / "main.py").write_text("import definitely_missing_dependency\n", encoding="utf-8")
            (root / "constants.py").write_text('APP_VERSION = "1.3.0"\n', encoding="utf-8")
            (root / "UserInterface" / "ui.py").write_text("", encoding="utf-8")
            (root / "Tools" / "enimas_updater.py").write_text("", encoding="utf-8")
            (root / "Tools" / "update_manager.py").write_text("", encoding="utf-8")
            (root / "lenses.json").write_text('{"lenses": []}', encoding="utf-8")
            failed = mock.Mock(returncode=1, stderr="missing dependency", stdout="")
            with mock.patch.object(
                health_check.subprocess, "run", return_value=failed
            ) as runtime:
                with self.assertRaises(health_check.HealthCheckError):
                    health_check.run_health_check(root, "1.3.0")
            self.assertEqual(
                runtime.call_args.kwargs["env"]["ENIMAS_HEALTH_CHECK"], "1"
            )

    def test_preparing_target_updater_does_not_replace_stable_updater(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = updater.UpdatePaths(root)
            stable = paths.bin / "enimas_updater.py"
            stable.parent.mkdir(parents=True)
            stable.write_bytes(b"print('stable')\n")
            target = b"print('target')\n"
            manifest = make_manifest()
            entry = next(
                item for item in manifest["files"] if item["path"] == "Tools/enimas_updater.py"
            )
            entry["size"] = len(target)
            entry["sha256"] = digest(target)
            manifest["updater"]["sha256"] = digest(target)
            manifest["archive_files"] = [
                {
                    "path": item["path"],
                    "size": item["size"],
                    "sha256": item["sha256"],
                }
                for item in manifest["files"]
            ]
            staging = paths.staging / "1.3.0" / "files"
            staged = staging / "src" / "Tools" / "enimas_updater.py"
            staged.parent.mkdir(parents=True)
            staged.write_bytes(target)
            external = updater._copy_updater_to_external_location(
                paths, manifest, staging
            )
            self.assertEqual(stable.read_bytes(), b"print('stable')\n")
            self.assertEqual(external.read_bytes(), target)
            self.assertNotEqual(external, stable)


class TransactionTests(unittest.TestCase):
    def _root(self, directory: str) -> Path:
        root = make_legacy_install(Path(directory), "1.2.4")
        (root / "src" / "Tools").mkdir(parents=True, exist_ok=True)
        (root / "src" / "constants.py").write_text('APP_VERSION = "1.2.4"\n', encoding="utf-8")
        return root

    def _legacy_transaction(self, directory: str):
        root = make_legacy_install(Path(directory), "1.0.7")
        user_payloads = {
            "src/config.txt": b"version=1.0.7\ncustom-setting=yes\n",
            "src/lenses.json": b'{"custom-lens": true}',
            "src/plugins/measurement/plugin_m_config.json": b'{"unit": "mm"}',
            "src/models/classification/custom/model.pt": b"custom classifier",
            "src/plugins/custom_lab/plugin.py": b"custom plugin",
            "src/venv/custom-marker.bin": b"existing environment",
        }
        for relative, data in user_payloads.items():
            destination = root / Path(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        (root / "src" / "update.bat").write_bytes(b"obsolete updater")
        release_payloads = {
            "constants.py": b'APP_VERSION = "1.3.0"\n',
            "main.py": b"print('new main')\n",
            "plugins/measurement/builtin.py": b"new built-in plugin",
            "Tools/enimas_updater.py": b"standalone updater",
        }
        manifest = make_manifest(
            [
                file_entry("constants.py", "src/constants.py", release_payloads["constants.py"]),
                file_entry("main.py", "src/main.py", release_payloads["main.py"]),
                file_entry(
                    "plugins/measurement/builtin.py",
                    "src/plugins/measurement/builtin.py",
                    release_payloads["plugins/measurement/builtin.py"],
                ),
            ],
            removed=["src/update.bat"],
        )
        paths = updater.UpdatePaths(root)
        plan = updater.calculate_update_plan(manifest, root)
        plan_path = stage_plan_payloads(paths, plan, release_payloads)
        return root, paths, plan_path, user_payloads

    def test_legacy_1_0_7_migration_preserves_user_data_and_establishes_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root, paths, plan_path, user_payloads = self._legacy_transaction(directory)
            with mock.patch.object(updater, "_run_health_check"):
                updater.apply_update(plan_path, root)
            self.assertEqual((root / "src" / "main.py").read_bytes(), b"print('new main')\n")
            self.assertFalse((root / "src" / "update.bat").exists())
            for relative, expected in user_payloads.items():
                self.assertEqual(
                    (root / Path(*relative.split("/"))).read_bytes(),
                    expected,
                )
            self.assertEqual(updater.read_json(paths.installed_manifest)["version"], "1.3.0")

    def test_legacy_1_0_7_health_failure_restores_code_cleanup_and_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root, paths, plan_path, user_payloads = self._legacy_transaction(directory)
            with mock.patch.object(
                updater,
                "_run_health_check",
                side_effect=updater.UpdateError("simulated health failure"),
            ):
                with self.assertRaises(updater.UpdateError):
                    updater.apply_update(plan_path, root)
            self.assertEqual((root / "src" / "main.py").read_bytes(), b"print('legacy main')\n")
            self.assertEqual((root / "src" / "update.bat").read_bytes(), b"obsolete updater")
            self.assertFalse((root / "src" / "plugins" / "measurement" / "builtin.py").exists())
            self.assertFalse(paths.installed_manifest.exists())
            for relative, expected in user_payloads.items():
                self.assertEqual(
                    (root / Path(*relative.split("/"))).read_bytes(),
                    expected,
                )

    def test_successful_apply_and_modified_file_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            old = b"locally modified"
            new = b"released replacement"
            destination = root / "src" / "managed.py"
            destination.write_bytes(old)
            manifest = make_manifest([file_entry("managed.py", "src/managed.py", new)])
            paths = updater.UpdatePaths(root)
            stage = paths.staging / "1.3.0"
            (stage / "files" / "src").mkdir(parents=True)
            (stage / "files" / "src" / "managed.py").write_bytes(new)
            (stage / "files" / "src" / "Tools" / "enimas_updater.py").parent.mkdir(parents=True, exist_ok=True)
            (stage / "files" / "src" / "Tools" / "enimas_updater.py").write_bytes(b"standalone updater")
            operations = [
                {
                    "kind": "replace",
                    "path": "managed.py",
                    "destination": "src/managed.py",
                    "sha256": digest(new),
                    "size": len(new),
                    "status": "pending",
                }
            ]
            plan = {
                "schema_version": 1,
                "target_version": "1.3.0",
                "current_version": "1.2.4",
                "manifest": manifest,
                "operations": operations,
                "dependencies_changed": False,
                "phase": "ready",
            }
            stage_external_updater(paths, plan)
            plan_path = stage / "plan.json"
            updater.atomic_write_json(plan_path, plan)
            with mock.patch.object(updater, "_run_health_check"):
                updater.apply_update(plan_path, root)
            self.assertEqual(destination.read_bytes(), new)
            backup = paths.backups / "1.2.4-before-1.3.0" / "src" / "managed.py"
            self.assertEqual(backup.read_bytes(), old)
            self.assertEqual(updater.read_json(paths.installed_manifest)["version"], "1.3.0")
            self.assertTrue(paths.latest_backup.is_file())

    def test_health_failure_rolls_back_replacement_addition_and_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            (root / "src" / "replace.py").write_bytes(b"old")
            (root / "src" / "remove.py").write_bytes(b"remove me")
            paths = updater.UpdatePaths(root)
            stage = paths.staging / "1.3.0"
            (stage / "files" / "src").mkdir(parents=True)
            (stage / "files" / "src" / "replace.py").write_bytes(b"new")
            (stage / "files" / "src" / "add.py").write_bytes(b"added")
            manifest = make_manifest(
                [
                    file_entry("replace.py", "src/replace.py", b"new"),
                    file_entry("add.py", "src/add.py", b"added"),
                ],
                removed=["src/remove.py"],
            )
            old_manifest = make_manifest(
                [
                    file_entry("replace.py", "src/replace.py", b"old"),
                    file_entry("remove.py", "src/remove.py", b"remove me"),
                ],
                version="1.2.4",
            )
            updater.atomic_write_json(paths.installed_manifest, old_manifest)
            stable_updater = paths.bin / "enimas_updater.py"
            stable_updater.parent.mkdir(parents=True)
            stable_updater.write_bytes(b"old updater")
            operations = [
                {"kind": "replace", "path": "replace.py", "destination": "src/replace.py", "sha256": digest(b"new"), "size": 3, "status": "pending"},
                {"kind": "add", "path": "add.py", "destination": "src/add.py", "sha256": digest(b"added"), "size": 5, "status": "pending"},
                {"kind": "remove", "destination": "src/remove.py", "status": "pending"},
            ]
            plan = {"schema_version": 1, "target_version": "1.3.0", "current_version": "1.2.4", "manifest": manifest, "operations": operations, "dependencies_changed": False, "phase": "ready"}
            stage_external_updater(paths, plan)
            plan_path = stage / "plan.json"
            updater.atomic_write_json(plan_path, plan)
            with mock.patch.object(updater, "_run_health_check", side_effect=updater.UpdateError("bad health")):
                with self.assertRaises(updater.UpdateError):
                    updater.apply_update(plan_path, root)
            self.assertEqual((root / "src" / "replace.py").read_bytes(), b"old")
            self.assertFalse((root / "src" / "add.py").exists())
            self.assertEqual((root / "src" / "remove.py").read_bytes(), b"remove me")
            self.assertEqual(updater.read_json(paths.result)["status"], "rolled_back")
            self.assertEqual(updater.read_json(paths.installed_manifest)["version"], "1.2.4")
            self.assertEqual(stable_updater.read_bytes(), b"old updater")

    def test_tampered_prepared_plan_cannot_target_preserved_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            config = root / "src" / "config.txt"
            config.write_bytes(b"user configuration")
            target = b"released file"
            manifest = make_manifest(
                [file_entry("managed.py", "src/managed.py", target)]
            )
            paths = updater.UpdatePaths(root)
            stage = paths.staging / "1.3.0"
            (stage / "files" / "src").mkdir(parents=True)
            (stage / "files" / "src" / "managed.py").write_bytes(target)
            plan = {
                "schema_version": 1,
                "target_version": "1.3.0",
                "current_version": "1.2.4",
                "manifest": manifest,
                "operations": [
                    {
                        "kind": "add",
                        "path": "managed.py",
                        "destination": "src/config.txt",
                        "sha256": digest(target),
                        "size": len(target),
                        "status": "pending",
                    }
                ],
                "dependencies_changed": False,
                "phase": "ready",
            }
            stage_external_updater(paths, plan)
            plan_path = stage / "plan.json"
            updater.atomic_write_json(plan_path, plan)
            with self.assertRaises(updater.UpdateError):
                updater.apply_update(plan_path, root)
            self.assertEqual(config.read_bytes(), b"user configuration")
            self.assertFalse(paths.journal.exists())

    def test_recovery_is_idempotent_after_backed_up_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            paths = updater.UpdatePaths(root)
            destination = root / "src" / "file.py"
            destination.write_bytes(b"new")
            interrupted_temp = destination.with_name(destination.name + ".update.tmp")
            interrupted_temp.write_bytes(b"incomplete replacement")
            backup_root = paths.backups / "crash"
            (backup_root / "src").mkdir(parents=True)
            (backup_root / "src" / "file.py").write_bytes(b"old")
            journal = {
                "phase": "applying_files",
                "backup_root": str(backup_root),
                "dependencies_changed": False,
                "operations": [{"kind": "replace", "destination": "src/file.py", "status": "backed_up", "existed": True}],
            }
            updater.atomic_write_json(paths.journal, journal)
            self.assertTrue(updater.recover_interrupted_update(root))
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertFalse(interrupted_temp.exists())
            self.assertFalse(updater.recover_interrupted_update(root))

    def test_duplicate_update_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = updater.UpdatePaths(Path(directory))
            with updater.UpdateLock(paths):
                with self.assertRaises(updater.UpdateActiveError):
                    with updater.UpdateLock(paths):
                        pass

    def test_partially_written_lock_is_treated_as_active_then_reclaimed_when_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = updater.UpdatePaths(Path(directory))
            paths.updates.mkdir()
            paths.lock.write_text("{", encoding="utf-8")
            with self.assertRaises(updater.UpdateActiveError):
                with updater.UpdateLock(paths):
                    pass
            old = updater.time.time() - updater.LOCK_INITIALIZATION_GRACE_SECONDS - 1
            os.utime(paths.lock, (old, old))
            with updater.UpdateLock(paths):
                self.assertTrue(paths.lock.exists())

    def test_disk_preflight_rejects_insufficient_space(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            plan = {"operations": [{"kind": "add", "destination": "src/a", "size": 1000}]}
            mocked = mock.Mock(free=1)
            with mock.patch.object(updater.shutil, "disk_usage", return_value=mocked):
                with self.assertRaises(updater.UpdateError):
                    updater._preflight_disk_space(updater.UpdatePaths(root), plan)

    def test_app_lock_rejects_a_second_process(self):
        with tempfile.TemporaryDirectory() as directory:
            code = (
                "import sys; from Tools.enimas_updater import acquire_app_lock, release_app_lock; "
                "root=sys.argv[1]; print(acquire_app_lock(root), flush=True); input(); release_app_lock(root)"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", code, directory],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "True")
                self.assertFalse(updater.acquire_app_lock(directory))
            finally:
                child.communicate("\n", timeout=10)
            self.assertTrue(updater.acquire_app_lock(directory))
            updater.release_app_lock(directory)

    def test_direct_startup_fails_closed_during_incomplete_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            paths = updater.UpdatePaths(root)
            paths.updates.mkdir(exist_ok=True)
            updater.atomic_write_json(
                paths.install_journal,
                {"phase": "source_activated", "target_version": "1.3.0"},
            )
            with self.assertRaises(updater.UpdateActiveError):
                updater.ensure_installation_is_ready(root)
            updater.atomic_write_json(paths.install_journal, {"phase": "complete"})
            updater.ensure_installation_is_ready(root)

    def test_requirements_hash_skips_pip_when_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            requirements = b"package==1.0\n"
            (root / "src" / "requirements.lock").write_bytes(requirements)
            manifest = make_manifest(requirements_hash=digest(requirements))
            self.assertFalse(updater._requirements_changed(manifest, root))
            manifest["requirements_sha256"] = "f" * 64
            self.assertTrue(updater._requirements_changed(manifest, root))

    def test_legacy_missing_lock_dependency_failure_leaves_application_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_legacy_install(Path(directory), "1.0.7")
            original_main = (root / "src" / "main.py").read_bytes()
            target_lock = b"example-package==2.0 --hash=sha256:" + b"a" * 64 + b"\n"
            manifest = make_manifest(
                [file_entry("requirements.lock", "src/requirements.lock", target_lock)],
                requirements_hash=digest(target_lock),
            )
            plan = updater.calculate_update_plan(manifest, root)
            self.assertTrue(plan["dependencies_changed"])
            paths = updater.UpdatePaths(root)
            staging_files = paths.staging / "1.3.0" / "files"
            staged_lock = staging_files / "src" / "requirements.lock"
            staged_lock.parent.mkdir(parents=True)
            staged_lock.write_bytes(target_lock)
            with mock.patch.object(
                updater.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1),
            ):
                with self.assertRaisesRegex(updater.UpdateError, "installed ENIMAS was not changed"):
                    updater._prepare_dependency_wheelhouse(
                        paths,
                        plan,
                        staging_files,
                        lambda *_args: None,
                    )
            self.assertEqual((root / "src" / "main.py").read_bytes(), original_main)
            self.assertFalse(paths.journal.exists())

    def test_dependency_rollback_snapshot_needs_no_git_and_reuses_target_wheels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_legacy_install(Path(directory), "1.0.7")
            target_lock = b"example-package==2.0 --hash=sha256:" + b"a" * 64 + b"\n"
            manifest = make_manifest(
                [file_entry("requirements.lock", "src/requirements.lock", target_lock)],
                requirements_hash=digest(target_lock),
            )
            manifest["pip_download_args"] = list(updater.ALLOWED_PIP_DOWNLOAD_ARGS)
            plan = updater.calculate_update_plan(manifest, root)
            paths = updater.UpdatePaths(root)
            staging_files = paths.staging / "1.3.0" / "files"
            staged_lock = staging_files / "src" / "requirements.lock"
            staged_lock.parent.mkdir(parents=True)
            staged_lock.write_bytes(target_lock)
            results = [
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [
                            {"name": "refiners", "version": "0.4.0"},
                            {"name": "torch", "version": "2.3.1+cpu"},
                            {"name": "pip", "version": "22.2.2"},
                        ]
                    ),
                    stderr="",
                ),
                subprocess.CompletedProcess([], 0),
            ]
            with (
                mock.patch.object(updater.subprocess, "run", side_effect=results) as run,
                mock.patch.object(updater, "_write_wheelhouse_lock"),
            ):
                updater._prepare_dependency_wheelhouse(
                    paths,
                    plan,
                    staging_files,
                    lambda *_args: None,
                )
            snapshot = (
                paths.staging
                / "1.3.0"
                / "previous-requirements-freeze.txt"
            ).read_text(encoding="utf-8")
            self.assertEqual(snapshot, "refiners==0.4.0\ntorch==2.3.1+cpu\n")
            self.assertNotIn("git+", snapshot)
            previous_command = run.call_args_list[2].args[0]
            self.assertIn("--find-links", previous_command)
            self.assertIn(str(paths.staging / "1.3.0" / "wheelhouse"), previous_command)
            self.assertIn(updater.PYTORCH_CPU_INDEX_URL, previous_command)

    def test_dependency_sync_removes_packages_absent_from_target_lock(self):
        allowed = {"target-package"}
        inventories = [
            {"pip", "setuptools", "target-package", "obsolete-package"},
            {"pip", "setuptools", "target-package"},
        ]
        with (
            mock.patch.object(updater, "_installed_package_names", side_effect=inventories),
            mock.patch.object(
                updater.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run,
        ):
            updater._synchronize_dependency_environment(
                Path("python.exe"),
                allowed,
                Path("."),
            )
        self.assertIn("obsolete-package", run.call_args.args[0])

    def test_authoritative_lock_includes_local_wheel_distribution_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = root / "vendor"
            vendor.mkdir()
            wheel = vendor / "custom_file_name-1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as bundle:
                bundle.writestr(
                    "custom_file_name-1.0.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: actual-package-name\nVersion: 1.0\n",
                )
            requirements = root / "requirements.lock"
            requirements.write_text(
                "regular_pkg==2.0 \\\n"
                "    --hash=sha256:" + "a" * 64 + "\n"
                "./vendor/custom_file_name-1.0-py3-none-any.whl \\\n"
                "    --hash=sha256:" + digest(wheel.read_bytes()) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                updater._locked_requirement_names(requirements),
                {"regular-pkg", "actual-package-name"},
            )

    def test_support_rollback_restores_latest_known_good_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            destination = root / "src" / "managed.py"
            destination.write_bytes(b"old")
            manifest = make_manifest([file_entry("managed.py", "src/managed.py", b"new")])
            paths = updater.UpdatePaths(root)
            stage = paths.staging / "1.3.0"
            (stage / "files" / "src").mkdir(parents=True)
            (stage / "files" / "src" / "managed.py").write_bytes(b"new")
            plan = {
                "schema_version": 1,
                "target_version": "1.3.0",
                "current_version": "1.2.4",
                "manifest": manifest,
                "operations": [{"kind": "replace", "path": "managed.py", "destination": "src/managed.py", "sha256": digest(b"new"), "size": 3, "status": "pending"}],
                "dependencies_changed": False,
                "phase": "ready",
            }
            stage_external_updater(paths, plan)
            plan_path = stage / "plan.json"
            updater.atomic_write_json(plan_path, plan)
            with mock.patch.object(updater, "_run_health_check"):
                updater.apply_update(plan_path, root)
                self.assertEqual(destination.read_bytes(), b"new")
                updater.rollback_latest_update(root)
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(updater.read_json(paths.result)["status"], "support_rollback")
            self.assertFalse(paths.latest_backup.exists())

    def test_interrupted_support_rollback_is_completed_by_startup_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            destination = root / "src" / "managed.py"
            destination.write_bytes(b"old")
            manifest = make_manifest([file_entry("managed.py", "src/managed.py", b"new")])
            paths = updater.UpdatePaths(root)
            stage = paths.staging / "1.3.0"
            (stage / "files" / "src").mkdir(parents=True)
            (stage / "files" / "src" / "managed.py").write_bytes(b"new")
            plan = {
                "schema_version": 1,
                "target_version": "1.3.0",
                "current_version": "1.2.4",
                "manifest": manifest,
                "operations": [{"kind": "replace", "path": "managed.py", "destination": "src/managed.py", "sha256": digest(b"new"), "size": 3, "status": "pending"}],
                "dependencies_changed": False,
                "phase": "ready",
            }
            stage_external_updater(paths, plan)
            plan_path = stage / "plan.json"
            updater.atomic_write_json(plan_path, plan)
            with mock.patch.object(updater, "_run_health_check"):
                updater.apply_update(plan_path, root)
            pointer = updater.read_json(paths.latest_backup)
            snapshot = updater.read_json(Path(pointer["journal"]))
            snapshot["transaction_kind"] = "support_rollback"
            snapshot["phase"] = "support_rollback"
            snapshot["support_previous_version"] = "1.2.4"
            snapshot["support_updated_version"] = "1.3.0"
            updater.atomic_write_json(paths.journal, snapshot)
            with mock.patch.object(updater, "_run_health_check"):
                self.assertTrue(updater.recover_interrupted_update(root))
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(
                updater.read_json(paths.journal)["phase"], "support_rollback_complete"
            )

    def test_dependency_rollback_removes_target_only_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            paths = updater.UpdatePaths(root)
            venv_python = root / "src" / "venv" / "Scripts" / "python.exe"
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_bytes(b"python")
            stage = paths.staging / "deps"
            (stage / "previous-wheelhouse").mkdir(parents=True)
            (stage / "previous-requirements-freeze.txt").write_text(
                "old-package==1.0\n", encoding="utf-8"
            )
            (stage / "previous-requirements.lock").write_text(
                "old-package==1.0 --hash=sha256:" + "a" * 64 + "\n",
                encoding="utf-8",
            )
            journal = {
                "dependencies_changed": True,
                "dependencies_installed": True,
                "phase": "applying_files",
                "plan_path": str(stage / "plan.json"),
            }
            results = [
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess(
                    [], 0, stdout="old-package==1.0\nnew-package==2.0\n", stderr=""
                ),
                subprocess.CompletedProcess([], 0),
            ]
            with mock.patch.object(updater.subprocess, "run", side_effect=results) as run:
                updater._restore_previous_dependencies(paths, journal)
            self.assertIn("new-package", run.call_args_list[-1].args[0])

    def test_rollback_lock_uses_wheel_metadata_not_direct_freeze_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            wheel = wheelhouse / "example_pkg-1.2.3-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as bundle:
                bundle.writestr(
                    "example_pkg-1.2.3.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: example-pkg\nVersion: 1.2.3\n",
                )
            lock = root / "rollback.lock"
            updater._write_wheelhouse_lock(wheelhouse, lock)
            contents = lock.read_text(encoding="utf-8")
            self.assertIn("example-pkg==1.2.3", contents)
            self.assertIn(updater.sha256_file(wheel), contents)
            self.assertNotIn("file://", contents)

    def test_wheel_identity_ignores_vendored_dist_info_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            wheel = wheelhouse / "setuptools-83.0.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as bundle:
                bundle.writestr(
                    "setuptools-83.0.0.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: setuptools\nVersion: 83.0.0\n",
                )
                bundle.writestr(
                    "setuptools/_vendor/packaging-26.0.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: packaging\nVersion: 26.0\n",
                )
            lock = root / "rollback.lock"
            updater._write_wheelhouse_lock(wheelhouse, lock)
            self.assertIn("setuptools==83.0.0", lock.read_text(encoding="utf-8"))
            self.assertEqual(updater._wheel_name(wheel), "setuptools")


class DownloadHandler(BaseHTTPRequestHandler):
    payload = b""
    interrupted = False
    ranges = []

    def do_GET(self):
        range_header = self.headers.get("Range")
        start = int(range_header.split("=")[1].split("-")[0]) if range_header else 0
        type(self).ranges.append(start)
        body = type(self).payload[start:]
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Length", str(len(body)))
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{len(type(self).payload)-1}/{len(type(self).payload)}")
        self.end_headers()
        if not type(self).interrupted and not range_header:
            type(self).interrupted = True
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.close_connection = True
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
            return
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class DownloadAndPreservationTests(unittest.TestCase):
    def test_interrupted_download_resumes_with_range_and_verifies_hash(self):
        payload = (b"ENIMAS" * 20000) + b"end"
        DownloadHandler.payload = payload
        DownloadHandler.interrupted = False
        DownloadHandler.ranges = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), DownloadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "payload.bin"
                url = f"http://127.0.0.1:{server.server_port}/payload"
                with self.assertRaises(updater.UpdateError):
                    updater._download_with_resume(url, target, len(payload), digest(payload), attempts=1)
                self.assertTrue(Path(str(target) + ".part").exists())
                updater._download_with_resume(url, target, len(payload), digest(payload), attempts=2)
                self.assertEqual(target.read_bytes(), payload)
                self.assertTrue(any(offset > 0 for offset in DownloadHandler.ranges))
        finally:
            server.shutdown()
            server.server_close()

    def test_corrupt_completed_download_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "payload.bin"
            target.write_bytes(b"wrong")
            with mock.patch.object(updater, "_open_url", side_effect=OSError("offline")):
                with self.assertRaises(updater.UpdateError):
                    updater._download_with_resume("https://example.invalid/file", target, 5, digest(b"right"), attempts=1)
            self.assertFalse(target.exists())

    def test_complete_verified_part_is_promoted_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "payload.bin"
            part = Path(str(target) + ".part")
            part.write_bytes(b"complete")
            with mock.patch.object(updater, "_open_url", side_effect=AssertionError("network used")):
                updater._download_with_resume(
                    "https://example.invalid/file",
                    target,
                    len(b"complete"),
                    digest(b"complete"),
                )
            self.assertEqual(target.read_bytes(), b"complete")
            self.assertFalse(part.exists())

    def test_complete_archive_part_is_promoted_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "release.zip"
            part = Path(str(archive) + ".part")
            with zipfile.ZipFile(part, "w") as bundle:
                bundle.writestr("project/file.txt", b"complete")
            with mock.patch.object(updater, "_open_url", side_effect=AssertionError("network used")):
                updater._download_archive_with_resume(
                    "https://example.invalid/release.zip", archive
                )
            self.assertTrue(archive.is_file())
            self.assertFalse(part.exists())

    def test_cancellation_before_download_keeps_installation_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "payload.bin"
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(updater.CancelledError):
                updater._download_with_resume(
                    "https://example.invalid/file",
                    target,
                    1,
                    digest(b"x"),
                    cancel_event=cancelled,
                )
            self.assertFalse(target.exists())

    def test_user_data_and_unmanaged_plugin_files_round_trip(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as backup_directory:
            root = Path(directory)
            (root / "src" / "plugins" / "measurement").mkdir(parents=True)
            (root / "src" / "plugins" / "custom").mkdir(parents=True)
            (root / "src" / "config.txt").write_text("user-config", encoding="utf-8")
            shipped = root / "src" / "plugins" / "measurement" / "plugin.py"
            shipped.write_text("modified shipped code", encoding="utf-8")
            custom = root / "src" / "plugins" / "measurement" / "my_extension.py"
            custom.write_text("custom", encoding="utf-8")
            external = root / "src" / "plugins" / "custom" / "plugin.py"
            external.write_text("external", encoding="utf-8")
            custom_model = root / "src" / "models" / "cropping" / "custom.onnx"
            custom_model.parent.mkdir(parents=True)
            custom_model.write_bytes(b"custom model")
            custom_sidecar = custom_model.with_suffix(".txt")
            custom_sidecar.write_text("labels and preprocessing metadata", encoding="utf-8")
            custom_config = custom_model.with_suffix(".cfg")
            custom_config.write_text("custom-model-config", encoding="utf-8")
            manifest = make_manifest([file_entry("plugins/measurement/plugin.py", "src/plugins/measurement/plugin.py", b"release")])
            backup = updater.backup_user_data(root, Path(backup_directory), managed_manifest=manifest)
            (root / "src" / "config.txt").write_text("default", encoding="utf-8")
            custom.unlink()
            external.unlink()
            custom_model.unlink()
            custom_sidecar.unlink()
            custom_config.unlink()
            updater.restore_user_data(root, backup)
            self.assertEqual((root / "src" / "config.txt").read_text(encoding="utf-8"), "user-config")
            self.assertEqual(custom.read_text(encoding="utf-8"), "custom")
            self.assertEqual(external.read_text(encoding="utf-8"), "external")
            self.assertEqual(custom_model.read_bytes(), b"custom model")
            self.assertEqual(
                custom_sidecar.read_text(encoding="utf-8"),
                "labels and preprocessing metadata",
            )
            self.assertEqual(custom_config.read_text(encoding="utf-8"), "custom-model-config")
            self.assertFalse((backup / "src" / "plugins" / "measurement" / "plugin.py").exists())

    def test_root_only_legacy_recovery_data_round_trips(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as backup_directory:
            root = Path(directory)
            (root / "config_backup.txt").write_bytes(b"legacy user config")
            (root / "lenses.json").write_bytes(b'{"legacy": true}')
            (root / "venv_backup" / "Scripts").mkdir(parents=True)
            (root / "venv_backup" / "Scripts" / "python.exe").write_bytes(b"legacy environment")

            backup = updater.backup_user_data(root, Path(backup_directory))
            updater.restore_user_data(root, backup)

            self.assertEqual((root / "src" / "config.txt").read_bytes(), b"legacy user config")
            self.assertEqual((root / "src" / "lenses.json").read_bytes(), b'{"legacy": true}')
            self.assertEqual(
                (root / "src" / "venv" / "Scripts" / "python.exe").read_bytes(),
                b"legacy environment",
            )

    def test_live_user_data_takes_priority_over_stale_legacy_root_backup(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as backup_directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "config.txt").write_bytes(b"current config")
            (root / "config_backup.txt").write_bytes(b"stale config")

            backup = updater.backup_user_data(root, Path(backup_directory))

            self.assertEqual((backup / "src" / "config.txt").read_bytes(), b"current config")

    def test_custom_plugin_and_model_reparse_points_are_rejected(self):
        for relative in (
            Path("src/plugins/custom/plugin.py"),
            Path("src/models/cropping/custom.onnx"),
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                candidate = root / relative
                candidate.parent.mkdir(parents=True)
                candidate.write_bytes(b"external target")

                with mock.patch.object(
                    updater,
                    "_is_reparse_point",
                    side_effect=lambda path, blocked=candidate: Path(path) == blocked,
                ):
                    with self.assertRaises(updater.UpdateError):
                        updater.backup_user_data(root, root / "backup")

    def test_disk_preflight_tree_size_rejects_reparse_before_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            linked_directory = root / "venv" / "external"
            linked_directory.mkdir(parents=True)
            (linked_directory / "large.bin").write_bytes(b"outside")

            with mock.patch.object(
                updater,
                "_is_reparse_point",
                side_effect=lambda path: Path(path) == linked_directory,
            ):
                with self.assertRaises(updater.UpdateError):
                    updater._tree_size(root / "venv")

    def test_repair_does_not_resurrect_a_deleted_builtin_plugin(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as backup_directory:
            root = Path(directory)
            plugin = root / "src" / "plugins" / "measurement" / "deleted_builtin.py"
            plugin.parent.mkdir(parents=True)
            plugin.write_text("old shipped code", encoding="utf-8")
            old = make_manifest(
                [file_entry("plugins/measurement/deleted_builtin.py", plugin.relative_to(root).as_posix(), b"old shipped code")],
                version="1.2.4",
            )
            target = make_manifest()
            merged = updater._merge_managed_manifests(old, target)
            backup = updater.backup_user_data(root, Path(backup_directory), managed_manifest=merged)
            self.assertFalse((backup / plugin.relative_to(root)).exists())

    def test_repair_preserves_custom_classifier_but_not_deleted_builtin_classifier(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as backup_directory:
            root = Path(directory)
            model_root = root / "src" / "models" / "classification"
            model_root.mkdir(parents=True)
            builtin = model_root / "deleted_builtin.onnx"
            builtin.write_bytes(b"old shipped model")
            custom = model_root / "custom_classifier.onnx"
            custom.write_bytes(b"user model")
            custom_sidecar = model_root / "custom_classifier.labels.txt"
            custom_sidecar.write_text("user labels", encoding="utf-8")
            old = make_manifest(
                [
                    file_entry(
                        "models/classification/deleted_builtin.onnx",
                        builtin.relative_to(root).as_posix(),
                        b"old shipped model",
                    )
                ],
                version="1.2.4",
            )
            target = make_manifest(
                removed=[builtin.relative_to(root).as_posix()],
            )
            merged = updater._merge_managed_manifests(old, target)
            backup = updater.backup_user_data(
                root,
                Path(backup_directory),
                managed_manifest=merged,
            )
            self.assertFalse((backup / builtin.relative_to(root)).exists())
            self.assertEqual((backup / custom.relative_to(root)).read_bytes(), b"user model")
            self.assertEqual(
                (backup / custom_sidecar.relative_to(root)).read_text(encoding="utf-8"),
                "user labels",
            )

    def test_fresh_or_repair_refuses_to_race_a_running_application(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(updater.acquire_app_lock(root))
            try:
                with self.assertRaises(updater.UpdateActiveError):
                    updater.prepare_fresh_or_repair_install(
                        "https://example.invalid/manifest.json",
                        root,
                        repair=True,
                    )
            finally:
                updater.release_app_lock(root)

    def test_normal_update_plan_never_touches_preserved_paths_or_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "venv").mkdir(parents=True)
            (root / "src" / "models" / "classification").mkdir(parents=True)
            (root / "src" / "config.txt").write_bytes(b"config-marker")
            (root / "src" / "venv" / "marker.bin").write_bytes(b"venv-marker")
            (root / "src" / "models" / "classification" / "custom.pt").write_bytes(b"model-marker")
            manifest = make_manifest()
            plan = updater.calculate_update_plan(manifest, root)
            destinations = {operation["destination"] for operation in plan["operations"]}
            self.assertFalse(any(path.startswith("src/venv/") for path in destinations))
            self.assertFalse(any(path.startswith("src/models/classification/") for path in destinations))
            self.assertNotIn("src/config.txt", destinations)
            self.assertEqual((root / "src" / "venv" / "marker.bin").read_bytes(), b"venv-marker")

    def test_archive_extraction_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape.txt", "bad")
            with self.assertRaises(updater.ManifestError):
                updater._safe_extract_archive(archive, Path(directory) / "out")

    def test_release_validation_rejects_tampered_system_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "Tools").mkdir()
            (source / "Tools" / "enimas_updater.py").write_bytes(b"standalone updater")
            (source / "camera-driver").mkdir()
            (source / "camera-driver" / "setup.exe").write_bytes(b"tampered")
            manifest = make_manifest(
                system_files=[
                    {
                        "path": "camera-driver/setup.exe",
                        "size": len(b"trusted"),
                        "sha256": digest(b"trusted"),
                    }
                ]
            )
            with self.assertRaises(updater.UpdateError):
                updater._validate_extracted_release(source, manifest)


class ProtectedControlPlaneTests(unittest.TestCase):
    def _same_release_manifests(self):
        release_files = {
            "Tools/enimas_updater.py": b"print('protected updater')\n",
            "install.bat": b"@echo off\r\nexit /b 0\r\n",
            "Tools/protected_install_dispatcher.bat": b"@echo off\r\nrem dispatcher\r\n",
            "camera-driver/driver.bin": b"trusted driver",
            "camera-driver/settings.ini": b"trusted settings",
        }
        archive_entries = [
            {"path": path, "size": len(data), "sha256": digest(data)}
            for path, data in release_files.items()
        ]
        manifest = make_manifest(
            system_files=archive_entries[-2:],
            archive_files=archive_entries,
        )
        manifest["files"][0]["size"] = len(release_files["Tools/enimas_updater.py"])
        manifest["files"][0]["sha256"] = digest(release_files["Tools/enimas_updater.py"])
        manifest["updater"]["sha256"] = digest(release_files["Tools/enimas_updater.py"])
        manifest["system_update_required"] = True
        manifest = updater.validate_manifest(manifest)
        control_manifest = json.loads(json.dumps(manifest))
        control_manifest["system_files"] = []
        control_manifest["system_update_required"] = False
        control_manifest = updater.validate_manifest(control_manifest)
        return release_files, manifest, control_manifest

    @contextlib.contextmanager
    def _same_release_workspace(self, release_files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            for relative, data in release_files.items():
                target = source / Path(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            program_data = root / "ProgramData"
            control = program_data / "ENIMAS" / "admin"
            control.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"ProgramData": str(program_data)}):
                yield source, control

    @contextlib.contextmanager
    def _active_same_release(self, release_files, active_manifest):
        with self._same_release_workspace(release_files) as (source, control):
            updater._stage_privileged_control_plane(source, active_manifest, control)
            updater.activate_control_plane(control)
            version_root = control / "versions" / active_manifest["source_ref"]
            yield source, control, version_root

    def _assert_invalid_current_pointer_preserves_version(
        self,
        pointer_payload,
        *,
        write_bytes=False,
    ):
        release_files, manifest, _control_manifest = self._same_release_manifests()
        with self._same_release_workspace(release_files) as (source, control):
            version_root = control / "versions" / manifest["source_ref"]
            version_root.mkdir(parents=True)
            sentinel = version_root / "existing-state.bin"
            sentinel.write_bytes(b"must remain untouched")
            current = control / "current.version"
            if write_bytes:
                current.write_bytes(pointer_payload)
            else:
                current.write_text(pointer_payload, encoding="utf-8")

            with self.assertRaises(updater.UpdateError):
                updater._stage_privileged_control_plane(source, manifest, control)

            self.assertEqual(sentinel.read_bytes(), b"must remain untouched")
            self.assertFalse((control / "pending.version").exists())

    def test_invalid_utf8_current_version_is_rejected_without_mutation(self):
        self._assert_invalid_current_pointer_preserves_version(b"v1.3.0\xff\n", write_bytes=True)

    def test_empty_or_unsafe_current_version_is_rejected_without_mutation(self):
        for pointer in ("", "../v1.3.0\n", "v1.3.0/other\n", "v1.3.0\nv1.2.0\n"):
            with self.subTest(pointer=pointer):
                self._assert_invalid_current_pointer_preserves_version(pointer)

    def test_unreadable_current_version_is_rejected_without_mutation(self):
        release_files, manifest, _control_manifest = self._same_release_manifests()
        with self._same_release_workspace(release_files) as (source, control):
            version_root = control / "versions" / manifest["source_ref"]
            version_root.mkdir(parents=True)
            sentinel = version_root / "existing-state.bin"
            sentinel.write_bytes(b"must remain untouched")
            current = control / "current.version"
            current.write_text(manifest["source_ref"] + "\n", encoding="utf-8")
            path_class = type(current)
            real_read_text = path_class.read_text
            resolved_current = current.resolve()

            def unreadable(path, *args, **kwargs):
                if Path(path).resolve() == resolved_current:
                    raise PermissionError("simulated unreadable current.version")
                return real_read_text(path, *args, **kwargs)

            with mock.patch.object(path_class, "read_text", new=unreadable):
                with self.assertRaisesRegex(
                    updater.UpdateError,
                    "Current protected installer version is unreadable",
                ):
                    updater._stage_privileged_control_plane(source, manifest, control)

            self.assertEqual(sentinel.read_bytes(), b"must remain untouched")
            self.assertFalse((control / "pending.version").exists())

    def test_dangling_current_pointer_is_not_treated_as_pristine_bootstrap(self):
        release_files, manifest, _control_manifest = self._same_release_manifests()
        with self._same_release_workspace(release_files) as (source, control):
            current = (control / "current.version").resolve()
            version_root = control / "versions" / manifest["source_ref"]
            path_class = type(current)
            real_read_text = path_class.read_text
            real_is_reparse = updater._is_reparse_point

            def dangling_pointer(path, *args, **kwargs):
                if Path(path).resolve() == current:
                    raise FileNotFoundError("simulated dangling current.version")
                return real_read_text(path, *args, **kwargs)

            def mark_pointer_as_reparse(path):
                return Path(path).resolve() == current or real_is_reparse(path)

            with (
                mock.patch.object(path_class, "read_text", new=dangling_pointer),
                mock.patch.object(
                    updater,
                    "_is_reparse_point",
                    side_effect=mark_pointer_as_reparse,
                ),
            ):
                with self.assertRaisesRegex(
                    updater.UpdateError,
                    "Current protected installer version is unreadable",
                ):
                    updater._stage_privileged_control_plane(source, manifest, control)

            self.assertFalse(version_root.exists())
            self.assertFalse((control / "pending.version").exists())

    def test_same_release_pointer_requires_existing_directory_without_mutation(self):
        release_files, manifest, _control_manifest = self._same_release_manifests()
        for version_state in ("missing", "regular-file"):
            with self.subTest(version_state=version_state):
                with self._same_release_workspace(release_files) as (source, control):
                    version_root = control / "versions" / manifest["source_ref"]
                    if version_state == "regular-file":
                        version_root.parent.mkdir(parents=True)
                        version_root.write_bytes(b"must remain untouched")
                    (control / "current.version").write_text(
                        manifest["source_ref"] + "\n",
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(
                        updater.UpdateError,
                        "Active protected installer version directory is invalid",
                    ):
                        updater._stage_privileged_control_plane(source, manifest, control)

                    self.assertFalse((control / "pending.version").exists())
                    if version_state == "missing":
                        self.assertFalse(version_root.exists())
                    else:
                        self.assertEqual(version_root.read_bytes(), b"must remain untouched")

    def test_same_release_pointer_rejects_reparse_version_directory_without_mutation(self):
        release_files, manifest, _control_manifest = self._same_release_manifests()
        with self._active_same_release(release_files, manifest) as (
            source,
            control,
            version_root,
        ):
            manifest_path = version_root / "maintenance_manifest.json"
            manifest_before = manifest_path.read_bytes()
            real_is_reparse = updater._is_reparse_point
            resolved_version_root = version_root.resolve()

            def mark_version_as_reparse(path):
                return Path(path).resolve() == resolved_version_root or real_is_reparse(path)

            with mock.patch.object(updater, "_is_reparse_point", side_effect=mark_version_as_reparse):
                with self.assertRaisesRegex(
                    updater.UpdateError,
                    "Active protected installer version directory is invalid",
                ):
                    updater._stage_privileged_control_plane(source, manifest, control)

            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertFalse((control / "pending.version").exists())

    def test_absent_current_version_allows_pristine_bootstrap(self):
        release_files, manifest, _control_manifest = self._same_release_manifests()
        with self._same_release_workspace(release_files) as (source, control):
            version_root = control / "versions" / manifest["source_ref"]
            updater._stage_privileged_control_plane(source, manifest, control)

            self.assertFalse((control / "current.version").exists())
            self.assertEqual(
                (control / "pending.version").read_text(encoding="utf-8").strip(),
                manifest["source_ref"],
            )
            updater._validate_control_version(version_root, manifest)

    def test_absent_current_version_rebuilds_partial_real_inactive_version(self):
        release_files, manifest, _control_manifest = self._same_release_manifests()
        with self._same_release_workspace(release_files) as (source, control):
            version_root = control / "versions" / manifest["source_ref"]
            version_root.mkdir(parents=True)
            stale = version_root / "partial-copy.bin"
            stale.write_bytes(b"interrupted first stage")

            updater._stage_privileged_control_plane(source, manifest, control)

            self.assertFalse(stale.exists())
            self.assertFalse((control / "current.version").exists())
            self.assertEqual(
                (control / "pending.version").read_text(encoding="utf-8").strip(),
                manifest["source_ref"],
            )
            updater._validate_control_version(version_root, manifest)
            stored = updater.validate_manifest(
                json.loads(
                    (version_root / "maintenance_manifest.json").read_text(encoding="utf-8")
                )
            )
            self.assertEqual(stored["system_files"], manifest["system_files"])

    def test_same_release_control_only_version_promotes_to_full_payload(self):
        release_files, manifest, control_manifest = self._same_release_manifests()
        with self._active_same_release(release_files, control_manifest) as (
            source,
            control,
            version_root,
        ):
            self.assertEqual(
                (control / "current.version").read_text(encoding="utf-8").strip(),
                manifest["source_ref"],
            )
            try:
                updater._stage_privileged_control_plane(source, manifest, control)
            except updater.UpdateError as exc:
                self.fail(f"same-release promotion raised UpdateError: {exc}")

            for entry in manifest["system_files"]:
                payload = version_root / "payloads" / Path(*entry["path"].split("/"))
                self.assertEqual(payload.stat().st_size, entry["size"])
                self.assertEqual(updater.sha256_file(payload), entry["sha256"])
            stored = updater.validate_manifest(
                json.loads((version_root / "maintenance_manifest.json").read_text(encoding="utf-8"))
            )
            self.assertEqual(stored["system_files"], manifest["system_files"])

    def test_same_release_promotion_activation_and_repeat_are_composed_idempotently(self):
        release_files, manifest, control_manifest = self._same_release_manifests()
        with self._same_release_workspace(release_files) as (source, control):
            version_root = control / "versions" / manifest["source_ref"]
            manifest_path = version_root / "maintenance_manifest.json"
            updater._stage_privileged_control_plane(source, control_manifest, control)
            updater.activate_control_plane(control)

            for _attempt in range(2):
                updater._stage_privileged_control_plane(source, manifest, control)
                self.assertEqual(
                    (control / "pending.version").read_text(encoding="utf-8").strip(),
                    manifest["source_ref"],
                )
                updater.activate_control_plane(control)
                self.assertEqual(
                    (control / "current.version").read_text(encoding="utf-8").strip(),
                    manifest["source_ref"],
                )
                self.assertFalse((control / "pending.version").exists())
                updater._validate_control_version(version_root, manifest)
                stored = updater.validate_manifest(
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                )
                self.assertEqual(stored["system_files"], manifest["system_files"])
                for entry in manifest["system_files"]:
                    payload = version_root / "payloads" / Path(*entry["path"].split("/"))
                    self.assertEqual(updater.sha256_file(payload), entry["sha256"])

    def test_same_release_full_version_is_not_downgraded_by_control_only_stage(self):
        release_files, manifest, control_manifest = self._same_release_manifests()
        with self._active_same_release(release_files, manifest) as (
            source,
            control,
            version_root,
        ):
            manifest_path = version_root / "maintenance_manifest.json"
            manifest_before = manifest_path.read_bytes()
            updater._stage_privileged_control_plane(source, control_manifest, control)

            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            stored = updater.validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
            self.assertEqual(stored["system_files"], manifest["system_files"])
            for entry in manifest["system_files"]:
                payload = version_root / "payloads" / Path(*entry["path"].split("/"))
                self.assertEqual(updater.sha256_file(payload), entry["sha256"])

    def test_same_release_control_only_stage_rejects_changed_system_archive_metadata(self):
        release_files, manifest, control_manifest = self._same_release_manifests()
        changed_control = json.loads(json.dumps(control_manifest))
        changed_path = manifest["system_files"][0]["path"]
        changed_entry = next(
            entry for entry in changed_control["archive_files"] if entry["path"] == changed_path
        )
        changed_entry["size"] = len(b"changed system payload")
        changed_entry["sha256"] = digest(b"changed system payload")
        changed_control = updater.validate_manifest(changed_control)

        with self._active_same_release(release_files, manifest) as (source, control, _version_root):
            with self.assertRaisesRegex(
                updater.UpdateError,
                f"Protected system payload metadata changed.*{changed_path}",
            ):
                updater._stage_privileged_control_plane(source, changed_control, control)

    def test_interrupted_same_release_promotion_can_be_retried(self):
        release_files, manifest, control_manifest = self._same_release_manifests()
        with self._active_same_release(release_files, control_manifest) as (
            source,
            control,
            version_root,
        ):
            manifest_path = version_root / "maintenance_manifest.json"
            control_manifest_bytes = manifest_path.read_bytes()
            real_copy = updater._copy_verified_release_file
            interrupted_path = manifest["system_files"][1]["path"]
            failed_once = False

            def interrupt_second_payload(source_root, relative_text, destination, metadata):
                nonlocal failed_once
                if relative_text == interrupted_path and not failed_once:
                    failed_once = True
                    raise updater.UpdateError("simulated interrupted promotion")
                return real_copy(source_root, relative_text, destination, metadata)

            with mock.patch.object(
                updater,
                "_copy_verified_release_file",
                side_effect=interrupt_second_payload,
            ):
                with self.assertRaisesRegex(updater.UpdateError, "simulated interrupted promotion"):
                    updater._stage_privileged_control_plane(source, manifest, control)

            self.assertEqual(manifest_path.read_bytes(), control_manifest_bytes)
            updater._stage_privileged_control_plane(source, manifest, control)
            updater._validate_control_version(version_root, manifest)
            stored = updater.validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
            self.assertEqual(stored["system_files"], manifest["system_files"])

    def test_same_release_promotion_rejects_changed_control_metadata(self):
        release_files, manifest, control_manifest = self._same_release_manifests()
        with self._active_same_release(release_files, control_manifest) as (
            source,
            control,
            version_root,
        ):
            changed = json.loads(json.dumps(manifest))
            changed_dispatcher = b"@echo off\r\nrem changed dispatcher\r\n"
            release_files["Tools/protected_install_dispatcher.bat"] = changed_dispatcher
            (source / "Tools" / "protected_install_dispatcher.bat").write_bytes(changed_dispatcher)
            dispatcher_entry = next(
                entry
                for entry in changed["archive_files"]
                if entry["path"] == "Tools/protected_install_dispatcher.bat"
            )
            dispatcher_entry["size"] = len(changed_dispatcher)
            dispatcher_entry["sha256"] = digest(changed_dispatcher)
            changed = updater.validate_manifest(changed)

            with self.assertRaisesRegex(
                updater.UpdateError,
                "Protected control-plane metadata changed.*protected_install_dispatcher.bat",
            ):
                updater._stage_privileged_control_plane(source, changed, control)
            stored = updater.validate_manifest(
                json.loads((version_root / "maintenance_manifest.json").read_text(encoding="utf-8"))
            )
            self.assertEqual(stored["system_files"], [])

    def test_same_release_promotion_rejects_corrupt_active_control_file(self):
        release_files, manifest, control_manifest = self._same_release_manifests()
        with self._active_same_release(release_files, control_manifest) as (
            source,
            control,
            version_root,
        ):
            (version_root / "install.bat").write_bytes(b"corrupt active installer")
            with self.assertRaisesRegex(
                updater.UpdateError,
                "Protected control-plane candidate is invalid: install.bat",
            ):
                updater._stage_privileged_control_plane(source, manifest, control)
            stored = updater.validate_manifest(
                json.loads((version_root / "maintenance_manifest.json").read_text(encoding="utf-8"))
            )
            self.assertEqual(stored["system_files"], [])

    def test_same_release_complete_stage_remains_idempotent(self):
        release_files, manifest, _control_manifest = self._same_release_manifests()
        with self._active_same_release(release_files, manifest) as (
            source,
            control,
            version_root,
        ):
            manifest_path = version_root / "maintenance_manifest.json"
            manifest_before = manifest_path.read_bytes()
            with (
                mock.patch.object(
                    updater,
                    "_copy_verified_release_file",
                    side_effect=AssertionError("complete version recopied"),
                ),
                mock.patch.object(
                    updater,
                    "atomic_write_json",
                    side_effect=AssertionError("complete manifest rewritten"),
                ),
            ):
                updater._stage_privileged_control_plane(source, manifest, control)

            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            updater._validate_control_version(version_root, manifest)
            self.assertEqual(
                (control / "pending.version").read_text(encoding="utf-8").strip(),
                manifest["source_ref"],
            )

    def test_prepare_system_cli_failure_is_written_to_update_log(self):
        with tempfile.TemporaryDirectory() as directory:
            install_root = Path(directory) / "ENIMAS"
            control_root = Path(directory) / "ProgramData" / "ENIMAS" / "admin"
            failure = "simulated privileged payload download failure"

            for handler in list(updater.LOGGER.handlers):
                updater.LOGGER.removeHandler(handler)
                handler.close()

            with mock.patch.object(
                updater,
                "prepare_system_maintenance",
                side_effect=updater.UpdateError(failure),
            ):
                result = updater.main(
                    [
                        "--install-root",
                        str(install_root),
                        "prepare-system",
                        "--control-root",
                        str(control_root),
                        "--fresh",
                    ]
                )

            self.assertEqual(result, updater.EXIT_ERROR)
            log_path = updater.UpdatePaths(install_root.resolve()).log
            self.assertTrue(log_path.is_file())
            self.assertIn(failure, log_path.read_text(encoding="utf-8"))

    def test_control_only_bootstrap_accepts_application_release_and_hashes_every_payload(self):
        updater_payload = b"print('protected updater')\n"
        installer_payload = b"@echo off\r\nexit /b 0\r\n"
        dispatcher_payload = b"@echo off\r\nrem dispatcher\r\n"
        driver_payload = b"driver"
        release_files = {
            "Tools/enimas_updater.py": updater_payload,
            "install.bat": installer_payload,
            "Tools/protected_install_dispatcher.bat": dispatcher_payload,
            "camera-driver/example.bin": driver_payload,
        }
        archive_entries = [
            {"path": path, "size": len(data), "sha256": digest(data)}
            for path, data in release_files.items()
        ]
        manifest = make_manifest(
            system_files=[archive_entries[-1]],
            archive_files=archive_entries,
        )
        manifest["files"][0]["size"] = len(updater_payload)
        manifest["files"][0]["sha256"] = digest(updater_payload)
        manifest["updater"]["sha256"] = digest(updater_payload)

        def download(_url, destination, size, expected_hash, **_kwargs):
            relative = next(
                path
                for path, data in release_files.items()
                if len(data) == size and digest(data) == expected_hash
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(release_files[relative])

        with tempfile.TemporaryDirectory() as directory:
            program_data = Path(directory) / "ProgramData"
            control = program_data / "ENIMAS" / "admin"
            control.mkdir(parents=True)
            with (
                mock.patch.dict(os.environ, {"ProgramData": str(program_data)}),
                mock.patch.object(updater, "fetch_manifest", return_value=manifest),
                mock.patch.object(updater, "_download_with_resume", side_effect=download) as fetched,
                mock.patch.object(updater, "_stage_privileged_control_plane") as staged,
                mock.patch.object(
                    updater,
                    "_download_archive_with_resume",
                    side_effect=AssertionError("full archive downloaded"),
                ),
            ):
                updater.prepare_system_maintenance(
                    "https://example.invalid/manifest.json",
                    control,
                    control_only=True,
                )
            self.assertEqual(fetched.call_count, len(release_files) - 1)
            source_root, staged_manifest, staged_control = staged.call_args.args
            self.assertEqual(staged_manifest["system_files"], [])
            self.assertFalse(staged_manifest["system_update_required"])
            self.assertEqual(staged_control, control.resolve())
            for relative, data in release_files.items():
                if relative.startswith("camera-driver/"):
                    self.assertFalse((source_root / Path(*relative.split("/"))).exists())
                    continue
                self.assertEqual((source_root / Path(*relative.split("/"))).read_bytes(), data)

    def test_control_pair_is_verified_before_atomic_pointer_activation(self):
        updater_payload = b"print('protected updater')\n"
        installer_payload = b"@echo off\r\nexit /b 0\r\n"
        dispatcher_payload = b"@echo off\r\nrem protected dispatcher\r\n"
        release_files = {
            "Tools/enimas_updater.py": updater_payload,
            "install.bat": installer_payload,
            "Tools/protected_install_dispatcher.bat": dispatcher_payload,
        }
        archive_entries = [
            {"path": path, "size": len(data), "sha256": digest(data)}
            for path, data in release_files.items()
        ]
        manifest = make_manifest(archive_files=archive_entries)
        manifest["files"][0]["size"] = len(updater_payload)
        manifest["files"][0]["sha256"] = digest(updater_payload)
        manifest["updater"]["sha256"] = digest(updater_payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            for relative, data in release_files.items():
                target = source / Path(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            program_data = root / "ProgramData"
            control = program_data / "ENIMAS" / "admin"
            control.mkdir(parents=True)
            (control / "current.version").write_text("v1.2.4\n", encoding="utf-8")
            (control / "install.bat").write_bytes(b"old dispatcher")
            (control / "versions" / "v1.2.4").mkdir(parents=True)
            (control / "versions" / "v1.1.0").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"ProgramData": str(program_data)}):
                updater._stage_privileged_control_plane(source, manifest, control)
                self.assertEqual(
                    (control / "current.version").read_text(encoding="utf-8").strip(),
                    "v1.2.4",
                )
                self.assertEqual(
                    (control / "pending.version").read_text(encoding="utf-8").strip(),
                    "v1.3.0",
                )
                candidate_installer = control / "versions" / "v1.3.0" / "install.bat"
                candidate_installer.write_bytes(b"tampered")
                with self.assertRaises(updater.UpdateError):
                    updater.activate_control_plane(control)
                self.assertEqual((control / "install.bat").read_bytes(), b"old dispatcher")
                candidate_installer.write_bytes(installer_payload)
                updater.activate_control_plane(control)
            self.assertEqual(
                (control / "current.version").read_text(encoding="utf-8").strip(),
                "v1.3.0",
            )
            self.assertEqual((control / "install.bat").read_bytes(), dispatcher_payload)
            self.assertTrue((control / "versions" / "v1.2.4").is_dir())
            self.assertFalse((control / "versions" / "v1.1.0").exists())


class InstallerContractTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows batch behavior")
    def test_failure_report_names_the_log_written_for_this_attempt(self):
        script = Path(__file__).resolve().parents[1] / "install.bat"
        source = script.read_text(encoding="utf-8")
        recorder_start = source.index("\n:record_installer_failure") + 1
        recorder_end = source.index("\n:record_admin_failure", recorder_start)
        recorder = source[recorder_start:recorder_end]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_root = root / "ENIMAS"
            stale_primary = install_root / "updates" / "update.log"
            stale_primary.mkdir(parents=True)
            program_data = root / "ProgramData"
            local_app_data = root / "LocalAppData"
            harness = root / "recorder-test.bat"
            harness_source = "\n".join(
                [
                    "@echo off",
                    "setlocal EnableExtensions EnableDelayedExpansion",
                    'call :record_installer_failure "current Repair failure" "5"',
                    "echo LOG=!INSTALLER_FAILURE_LOG!",
                    "exit /b 0",
                    recorder,
                    ":is_admin",
                    "exit /b 1",
                    "",
                ]
            )
            harness.write_bytes(harness_source.replace("\n", "\r\n").encode("utf-8"))
            environment = os.environ.copy()
            environment.update(
                {
                    "INSTALL_ROOT": str(install_root),
                    "CONTROL_ROOT": str(program_data / "ENIMAS" / "admin"),
                    "ProgramData": str(program_data),
                    "LOCALAPPDATA": str(local_app_data),
                    "INSTALLER_REVISION": "20260812.5",
                }
            )

            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", str(harness)],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            log_line = next(
                line for line in completed.stdout.splitlines() if line.startswith("LOG=")
            )
            reported_log = Path(log_line.removeprefix("LOG="))
            expected_fallbacks = {
                str(program_data / "ENIMAS" / "installer-fallback.log").casefold(),
                str(local_app_data / "ENIMAS" / "installer-fallback.log").casefold(),
            }
            self.assertIn(str(reported_log).casefold(), expected_fallbacks)
            self.assertTrue(reported_log.is_file())
            self.assertIn(
                "current Repair failure",
                reported_log.read_text(encoding="utf-8"),
            )
            self.assertTrue(stale_primary.is_dir())

    @unittest.skipUnless(os.name == "nt", "Windows batch behavior")
    def test_invalid_install_user_keeps_repair_failure_visible(self):
        script = Path(__file__).resolve().parents[1] / "install.bat"
        source = script.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_root = root / "ENIMAS"
            control_parent = root / "ProgramData" / "ENIMAS"
            disposable_installer = root / "install.bat"
            source = source.replace(
                r'set "INSTALL_ROOT=C:\Program Files\ENIMAS"',
                f'set "INSTALL_ROOT={install_root}"',
                1,
            )
            source = source.replace(
                r'set "CONTROL_PARENT=C:\ProgramData\ENIMAS"',
                f'set "CONTROL_PARENT={control_parent}"',
                1,
            )
            source = source.replace("\npause\n", "\nrem pause\n")
            disposable_installer.write_bytes(source.replace("\n", "\r\n").encode("utf-8"))
            environment = os.environ.copy()
            environment.update(
                {
                    "ProgramData": str(root / "ProgramData"),
                    "LOCALAPPDATA": str(root / "LocalAppData"),
                }
            )

            completed = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    str(disposable_installer),
                    "--repair",
                    "--caller-sid",
                    "invalid-sid",
                ],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            output = completed.stdout + completed.stderr
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("ENIMAS Repair did not complete", output)
            self.assertIn("identifying the ENIMAS installation owner", output)
            self.assertIn("Installer revision: 20260812.5", output)
            log_path = install_root / "updates" / "update.log"
            self.assertTrue(log_path.is_file())
            self.assertIn(
                "identifying the ENIMAS installation owner",
                log_path.read_text(encoding="utf-8"),
            )

    @unittest.skipUnless(os.name == "nt", "Windows batch behavior")
    def test_installer_displays_a_supportable_revision(self):
        script = Path(__file__).resolve().parents[1] / "install.bat"
        source = script.read_text(encoding="utf-8")
        if "--test-installer-identity" not in source:
            self.fail("install.bat has no safe installer identity test entry point")

        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", str(script), "--test-installer-identity"],
            cwd=script.parent,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertRegex(output, r"ENIMAS installer revision: \d{8}\.\d+")
        self.assertIn("official installer", output.lower())

    def test_repair_failures_are_durable_and_keep_the_user_window_open(self):
        script = (Path(__file__).resolve().parents[1] / "install.bat").read_text(encoding="utf-8")
        repair = script[script.index(":user_repair") : script.index(":user_source_setup")]
        reporter_start = script.index("\n:report_repair_failure")
        recorder_start = script.index("\n:record_installer_failure")
        elevate_start = script.index("\n:elevate_wait")
        reporter = script[reporter_start:recorder_start]
        recorder = script[recorder_start:elevate_start]

        self.assertGreaterEqual(repair.count("call :report_repair_failure"), 7)
        self.assertIn("pause", reporter)
        self.assertIn("Diagnostic log for this failure", reporter)
        self.assertIn("INSTALLER_FAILURE_LOG", reporter)
        self.assertIn("Existing ENIMAS files and user data were not removed", reporter)
        self.assertIn(r"updates\update.log", recorder)
        self.assertIn("installer-fallback.log", recorder)
        self.assertIn("call :record_admin_failure", script[: script.index(":user_repair")])
        self.assertIn(r"%CONTROL_ROOT%\install-acl.version", recorder)
        self.assertIn(":reject_reparse_point", recorder)

    def test_explicit_repair_runs_verified_system_preparation_and_refreshes_galaxy(self):
        script = (Path(__file__).resolve().parents[1] / "install.bat").read_text(encoding="utf-8")
        repair = script[script.index(":user_repair") : script.index(":user_source_setup")]
        fresh = script[script.index(":user_fresh") : script.index(":user_repair")]

        self.assertIn("call :elevate_wait --admin-prepare-fresh", repair)
        self.assertNotIn("maintenance-status", repair)
        expected_refresh = (
            'call :configure_galaxy_environment '
            '"C:\\Program Files\\Daheng Imaging\\GalaxySDK\\GenICam"'
        )
        self.assertIn(expected_refresh, repair)
        self.assertIn(expected_refresh, fresh)

    def test_protected_bootstrap_propagates_uac_cancellation(self):
        script = (Path(__file__).resolve().parents[1] / "install.bat").read_text(encoding="utf-8")
        bootstrap = script[
            script.index("\n:ensure_protected_control") : script.index("\n:select_python")
        ]
        broker = script[script.index("\n:run_elevated") : script.index("\n:require_unelevated_user")]

        self.assertIn('call :run_elevated "%~f0" "--admin-bootstrap-control"', bootstrap)
        self.assertIn("$ErrorActionPreference='Stop'", broker)
        self.assertIn("NativeErrorCode -eq 1223", broker)
        self.assertIn("exit 1223", broker)
        self.assertIn('set "CONTROL_BOOTSTRAP_EXIT=!ERRORLEVEL!"', bootstrap)
        self.assertIn("exit /b !CONTROL_BOOTSTRAP_EXIT!", bootstrap)
        self.assertLess(
            bootstrap.index('set "CONTROL_BOOTSTRAP_EXIT=!ERRORLEVEL!"'),
            bootstrap.index("One-time security preparation completed"),
        )

    def test_retry_does_not_reuse_choice_errorlevel_in_ensure_updater(self):
        script = (Path(__file__).resolve().parents[1] / "install.bat").read_text(encoding="utf-8")
        ensure = script[script.index("\n:ensure_updater") : script.index("\n:install_vcredist")]

        self.assertIn(
            "if not defined PYTHON_EXE (\n"
            "    call :select_python\n"
            "    if errorlevel 1 exit /b 1\n"
            ")",
            ensure,
        )
        self.assertNotIn(
            "if not defined PYTHON_EXE call :select_python\nif errorlevel 1 exit /b 1",
            ensure,
        )

    @unittest.skipUnless(os.name == "nt", "Windows elevation broker behavior")
    def test_elevation_broker_returns_child_exit_through_private_pipe(self):
        script = Path(__file__).resolve().parents[1] / "install.bat"
        source = script.read_text(encoding="utf-8")
        if "--test-elevation-broker" not in source:
            self.fail("install.bat has no safe private-pipe elevation broker test entry point")

        environment = {}
        for key, value in os.environ.items():
            environment[key.casefold()] = (key, value)
        environment = {key: value for key, value in environment.values()}

        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", str(script), "--test-elevation-broker"],
            cwd=script.parent,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 42, completed.stdout + completed.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows elevation broker behavior")
    def test_elevation_broker_does_not_depend_on_child_environment(self):
        script = Path(__file__).resolve().parents[1] / "install.bat"
        source = script.read_text(encoding="utf-8")
        if "--test-elevation-broker-no-env" not in source:
            self.fail("install.bat has no safe environment-independent broker test entry point")

        environment = {}
        for key, value in os.environ.items():
            environment[key.casefold()] = (key, value)
        environment = {key: value for key, value in environment.values()}

        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", str(script), "--test-elevation-broker-no-env"],
            cwd=script.parent,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 42, completed.stdout + completed.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows SID validation behavior")
    def test_user_sid_validator_accepts_domain_and_entra_user_shapes(self):
        script = Path(__file__).resolve().parents[1] / "install.bat"
        source = script.read_text(encoding="utf-8")
        if "--test-install-user-sid" not in source:
            self.fail("install.bat has no safe user-SID validation test entry point")

        cases = {
            "S-1-5-21-111111111-222222222-333333333-1001": 0,
            "S-1-12-1-111111111-222222222-333333333-444444444": 0,
            "S-1-5-18": 1,
            "S-1-12-1-1-2-3-4-invalid": 1,
        }
        for sid, expected in cases.items():
            with self.subTest(sid=sid):
                completed = subprocess.run(
                    ["cmd.exe", "/d", "/c", str(script), "--test-install-user-sid", sid],
                    cwd=script.parent,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)

    def test_pip_failure_preserves_its_exit_code_for_abort_diagnostics(self):
        script = (Path(__file__).resolve().parents[1] / "install.bat").read_text(encoding="utf-8")
        setup = script[script.index("\n:user_source_setup") : script.index("\n:admin_prepare_fresh")]

        self.assertIn('set "SETUP_EXIT=!DEPENDENCY_EXIT!"', setup)
        self.assertIn("goto user_setup_failed_known", setup)
        self.assertIn("\n:user_setup_failed_known\n", setup)

    def test_complete_galaxy_sdk_skips_vendor_installer(self):
        script = (Path(__file__).resolve().parents[1] / "install.bat").read_text(encoding="utf-8")
        system = script[script.index("\n:admin_apply_system") : script.index("\n:admin_grant_access")]
        validator = (
            'call :configure_galaxy_environment '
            '"C:\\Program Files\\Daheng Imaging\\GalaxySDK\\GenICam"'
        )
        validator_index = system.index(validator)
        installer_index = system.index('start "" /wait "%VA_INSTALLER%"')

        self.assertLess(validator_index, installer_index)
        self.assertIn(
            "if not errorlevel 1 goto galaxy_sdk_ready",
            system[validator_index:installer_index],
        )
        self.assertGreater(system.index("\n:galaxy_sdk_ready"), installer_index)

    @unittest.skipUnless(os.name == "nt", "Windows batch behavior")
    def test_galaxy_environment_validator_rejects_an_incomplete_sdk(self):
        script = Path(__file__).resolve().parents[1] / "install.bat"
        source = script.read_text(encoding="utf-8")
        if "--test-galaxy-environment" not in source:
            self.fail("install.bat has no safe Galaxy environment validation test entry point")

        with tempfile.TemporaryDirectory() as directory:
            sdk_root = Path(directory) / "Galaxy SDK"
            genicam_root = sdk_root / "GenICam"
            for relative in ("bin/Win32_i86", "bin/Win64_x64"):
                (genicam_root / relative).mkdir(parents=True)
            for relative in ("APIDll/Win32/GxIAPI.dll", "APIDll/Win64/GxIAPI.dll"):
                target = sdk_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"test dll")

            complete = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    str(script),
                    "--test-galaxy-environment",
                    str(genicam_root),
                ],
                cwd=script.parent,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(complete.returncode, 0, complete.stdout + complete.stderr)

            (sdk_root / "APIDll" / "Win32" / "GxIAPI.dll").unlink()
            incomplete = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    str(script),
                    "--test-galaxy-environment",
                    str(genicam_root),
                ],
                cwd=script.parent,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(incomplete.returncode, 0)
            self.assertIn("incomplete", (incomplete.stdout + incomplete.stderr).lower())

    def test_existing_detection_precedes_uac_and_no_destructive_source_clean(self):
        script = (Path(__file__).resolve().parents[1] / "install.bat").read_text(encoding="utf-8")
        self.assertLess(
            script.index("Existing-install detection"),
            script.index("\n:admin_prepare_fresh"),
        )
        self.assertNotIn('del /f /s /q "C:\\Program Files\\ENIMAS\\src\\*"', script)
        self.assertIn("bootstrap", script)
        self.assertIn("--repair", script)
        self.assertNotIn('/grant "*S-1-5-32-545:', script)
        self.assertIn("ENIMAS_INSTALL_USER_SID", script)
        self.assertIn(":verify_signature", script)
        self.assertIn('set "CONTROL_PARENT=C:\\ProgramData\\ENIMAS"', script)
        self.assertIn('set "CONTROL_ROOT=%CONTROL_PARENT%\\admin"', script)
        self.assertIn('set "UPDATER=%BOOTSTRAP_UPDATER%"', script)
        self.assertNotIn('set "UPDATER=%STABLE_UPDATER%"', script)
        self.assertIn("SYSTEM_PAYLOAD_ROOT", script)
        self.assertNotIn("SYSTEM_GIT", script)
        self.assertIn("--require-hashes", script)
        self.assertIn("requirements.lock", script)
        self.assertIn('if "!UPDATE_EXIT!"=="21" goto user_repair', script)
        self.assertIn('if "!UPDATE_EXIT!"=="22" goto legacy_repair_required', script)
        self.assertIn("migration-status", script)
        existing_update = script[script.index(":existing_update") : script.index(":existing_failed")]
        self.assertLess(
            existing_update.index("migration-status"),
            existing_update.index("call :ensure_protected_control"),
        )
        self.assertIn('if exist "%INSTALL_ROOT%\\src" goto damaged_install', script)
        self.assertIn('if exist "%INSTALL_ROOT%" goto damaged_install', script)
        self.assertNotIn('if exist "%INSTALL_ROOT%" goto user_fresh', script)
        self.assertNotIn('if /I "%~1"=="--fresh" goto user_fresh', script)
        self.assertIn("--admin-grant-access", script)
        self.assertIn("--admin-bootstrap-control", script)
        self.assertIn("--control-only", script)
        self.assertIn("The update process is active", script)
        self.assertIn("One-time security preparation completed", script)
        self.assertIn("install-acl.version", script)
        self.assertIn(":protect_install_tree", script)
        self.assertIn("sync-dependencies", script)
        fresh_admin = script[
            script.index("\n:admin_prepare_fresh") : script.index("\n:admin_bootstrap_control")
        ]
        bootstrap_admin = script[
            script.index("\n:admin_bootstrap_control") : script.index("\n:admin_prepare_repair")
        ]
        self.assertIn("call :ensure_system_python", fresh_admin)
        self.assertIn("call :validate_existing_system_python", bootstrap_admin)
        self.assertNotIn("call :ensure_system_python", bootstrap_admin)
        self.assertIn("full re[P]air", script)
        self.assertIn(":require_unelevated_user", script)
        self.assertIn("57F89B3977781BBE62A0FD7C5C1359BCF256DDFC98C2DB7296D5A0851B8EA863", script)
        self.assertIn('call :verify_signature "!ARDU_CAT!" "eyesDx, Inc"', script)
        elevated_system = script[
            script.index("\n:admin_apply_system") : script.index("\n:admin_grant_access")
        ]
        self.assertNotIn(r"%INSTALL_ROOT%\src", elevated_system)
        self.assertNotIn(r"src\venv", elevated_system)
        self.assertNotIn('set "VA_INSTALLER=%INSTALL_ROOT%\\src\\camera-driver', script)
        self.assertIn(
            'if "!RUNNING_PROTECTED!"=="1" set "ENIMAS_MANIFEST_URL=%CANONICAL_MANIFEST_URL%"',
            script,
        )
        dispatcher = (
            Path(__file__).resolve().parents[1]
            / "Tools"
            / "protected_install_dispatcher.bat"
        ).read_text(encoding="utf-8")
        self.assertIn("current.version", dispatcher)
        self.assertIn(r"versions\!CONTROL_VERSION!\install.bat", dispatcher)

    def test_pnputil_success_codes_are_exercised_by_batch_policy(self):
        script = Path(__file__).resolve().parents[1] / "install.bat"
        raw_script = script.read_bytes()
        self.assertNotIn(b"\n", raw_script.replace(b"\r\n", b""))
        attributes = (
            Path(__file__).resolve().parents[1] / ".gitattributes"
        ).read_text(encoding="utf-8")
        self.assertIn("*.bat -text", attributes)
        for code in (0, 259, 3010, 1641):
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", str(script), "--test-pnputil-exit", str(code)],
                cwd=script.parent,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, code)
        for code in (1, 5, 87):
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", str(script), "--test-pnputil-exit", str(code)],
                cwd=script.parent,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0, code)

    def test_update_ui_contract_is_present_without_hardware_startup_changes(self):
        source = (Path(__file__).resolve().parents[1] / "Tools" / "update_manager.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("Check for Updates", source)
        self.assertIn("QTimer.singleShot", source)
        self.assertIn("Download and install", source)
        self.assertIn("The update is downloaded and verified", source)
        self.assertIn("request_orderly_application_exit(self.window)", source)
        self.assertIn("aboutToQuit.connect(self._shutdown_workers)", source)
        self.assertIn("self.prepare_worker.wait()", source)
        main_source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertLess(
            main_source.index("_early_installation_guard()"),
            main_source.index("from serial import Serial"),
        )
        main_window_show = main_source.rindex("window.show()")
        self.assertGreater(main_window_show, main_source.index("camera, camera_type = open_camera()"))
        self.assertGreater(main_source.index("update_controller.start()"), main_window_show)

    def test_direct_main_fails_before_hardware_imports_for_incomplete_install(self):
        source_main = Path(__file__).resolve().parents[1] / "main.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copied_main = root / "src" / "main.py"
            copied_main.parent.mkdir()
            copied_main.write_bytes(source_main.read_bytes())
            updates = root / "updates"
            updates.mkdir()
            (updates / "install_state.json").write_text(
                json.dumps({"phase": "source_activated"}), encoding="utf-8"
            )
            incomplete = subprocess.run(
                [sys.executable, str(copied_main)],
                cwd=copied_main.parent,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(incomplete.returncode, 42)
            self.assertIn("not ready", incomplete.stderr)
            (updates / "install_state.json").write_text("{", encoding="utf-8")
            corrupt = subprocess.run(
                [sys.executable, str(copied_main)],
                cwd=copied_main.parent,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(corrupt.returncode, 1)
            self.assertIn("unreadable", corrupt.stderr)

    def test_headless_update_exit_runs_window_close_event_and_preserves_code_42(self):
        code = r"""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QWidget
from Tools.update_manager import request_orderly_application_exit
class Window(QWidget):
    closed = False
    def closeEvent(self, event):
        self.closed = True
        event.accept()
app = QApplication([])
window = Window()
window.show()
QTimer.singleShot(0, lambda: request_orderly_application_exit(window))
result = app.exec()
raise SystemExit(0 if result == 42 and window.closed else 1)
"""
        environment = dict(os.environ)
        environment["QT_QPA_PLATFORM"] = "offscreen"
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)

    def test_release_archive_is_tag_pinned(self):
        url = updater.release_archive_url(make_manifest())
        self.assertEqual(url, "https://example.invalid/project/-/archive/v1.3.0/project-v1.3.0.zip")

    def _write_release_archive(self, root: Path, manifest: dict, files: dict[str, bytes]) -> Path:
        stage = updater.UpdatePaths(root).staging / f"install-{manifest['version']}"
        stage.mkdir(parents=True, exist_ok=True)
        archive = stage / "release.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for relative, data in files.items():
                bundle.writestr(f"project-{manifest['source_ref']}/{relative}", data)
        return archive

    def test_repair_stages_before_activation_preserves_data_and_can_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_src = root / "src"
            (old_src / "plugins" / "measurement").mkdir(parents=True)
            (old_src / "models" / "classification").mkdir(parents=True)
            (old_src / "venv").mkdir(parents=True)
            (old_src / "config.txt").write_bytes(b"user config")
            (old_src / "lenses.json").write_bytes(b'{"user": true}')
            (old_src / "plugins" / "measurement" / "custom.py").write_bytes(b"custom plugin")
            (old_src / "plugins" / "measurement" / "builtin.py").write_bytes(b"old builtin")
            (old_src / "models" / "classification" / "user.pt").write_bytes(b"model")
            (old_src / "venv" / "marker.bin").write_bytes(b"old environment")

            release_files = {
                "Tools/enimas_updater.py": b"standalone updater",
                "Tools/update_manager.py": b"ui updater",
                "main.py": b"new main",
                "constants.py": b'APP_VERSION = "1.3.0"\n',
                "plugins/measurement/builtin.py": b"new builtin",
                "config.txt": b"default config",
                "lenses.json": b"{}",
                "install.bat": b"new installer",
                "ENIMAS.bat": b"new launcher",
            }
            entries = [
                file_entry(path, f"src/{path}", data)
                for path, data in release_files.items()
                if path not in {"config.txt", "lenses.json", "Tools/enimas_updater.py"}
            ]
            entries.append(file_entry("Tools/enimas_updater.py", "src/Tools/enimas_updater.py", b"standalone updater"))
            archive_entries = [
                {"path": path, "size": len(data), "sha256": digest(data)}
                for path, data in release_files.items()
            ]
            manifest = make_manifest(entries, archive_files=archive_entries)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self._write_release_archive(root, manifest, release_files)

            updater.prepare_fresh_or_repair_install(manifest_path.as_uri(), root, repair=True)
            self.assertEqual((root / "src" / "config.txt").read_bytes(), b"user config")
            self.assertEqual((root / "src" / "plugins" / "measurement" / "custom.py").read_bytes(), b"custom plugin")
            self.assertEqual((root / "src" / "plugins" / "measurement" / "builtin.py").read_bytes(), b"new builtin")
            self.assertEqual((root / "src" / "models" / "classification" / "user.pt").read_bytes(), b"model")
            self.assertEqual((root / "src" / "venv" / "marker.bin").read_bytes(), b"old environment")
            updater.abort_fresh_or_repair_install(root, "simulated dependency failure")
            self.assertEqual((root / "src" / "plugins" / "measurement" / "builtin.py").read_bytes(), b"old builtin")
            self.assertEqual((root / "src" / "venv" / "marker.bin").read_bytes(), b"old environment")

    def test_repair_recovers_root_only_interrupted_legacy_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config_backup.txt").write_bytes(b"recovered config")
            (root / "lenses.json").write_bytes(b'{"recovered": true}')
            (root / "venv_backup" / "Scripts").mkdir(parents=True)
            (root / "venv_backup" / "Scripts" / "python.exe").write_bytes(b"recovered environment")

            release_files = {
                "Tools/enimas_updater.py": b"standalone updater",
                "Tools/update_manager.py": b"ui updater",
                "main.py": b"new main",
                "constants.py": b'APP_VERSION = "1.3.0"\n',
                "config.txt": b"default config",
                "lenses.json": b"{}",
                "install.bat": b"new installer",
                "ENIMAS.bat": b"new launcher",
            }
            entries = [
                file_entry(path, f"src/{path}", data)
                for path, data in release_files.items()
                if path not in {"config.txt", "lenses.json", "Tools/enimas_updater.py"}
            ]
            entries.append(
                file_entry(
                    "Tools/enimas_updater.py",
                    "src/Tools/enimas_updater.py",
                    b"standalone updater",
                )
            )
            archive_entries = [
                {"path": path, "size": len(data), "sha256": digest(data)}
                for path, data in release_files.items()
            ]
            manifest = make_manifest(entries, archive_files=archive_entries)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self._write_release_archive(root, manifest, release_files)

            updater.prepare_fresh_or_repair_install(manifest_path.as_uri(), root, repair=True)

            self.assertEqual((root / "src" / "config.txt").read_bytes(), b"recovered config")
            self.assertEqual((root / "src" / "lenses.json").read_bytes(), b'{"recovered": true}')
            self.assertEqual(
                (root / "src" / "venv" / "Scripts" / "python.exe").read_bytes(),
                b"recovered environment",
            )
            updater.abort_fresh_or_repair_install(root, "simulated post-activation failure")
            self.assertFalse((root / "src").exists())
            self.assertEqual((root / "config_backup.txt").read_bytes(), b"recovered config")
            self.assertEqual((root / "lenses.json").read_bytes(), b'{"recovered": true}')
            self.assertTrue((root / "venv_backup" / "Scripts" / "python.exe").is_file())

    def test_fresh_install_refuses_root_only_legacy_recovery_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config_backup.txt").write_bytes(b"recover me")

            with self.assertRaises(updater.RepairRequiredError):
                updater.prepare_fresh_or_repair_install(
                    "https://example.invalid/manifest.json",
                    root,
                    repair=False,
                )

    def test_fresh_install_completion_writes_manifest_only_after_health(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_files = {
                "Tools/enimas_updater.py": b"standalone updater",
                "Tools/update_manager.py": b"ui updater",
                "main.py": b"new main",
                "constants.py": b'APP_VERSION = "1.3.0"\n',
                "UserInterface/ui.py": b"ui",
                "install.bat": b"new installer",
                "ENIMAS.bat": b"new launcher",
            }
            entries = [
                file_entry(path, f"src/{path}", data)
                for path, data in release_files.items()
                if path != "Tools/enimas_updater.py"
            ]
            entries.append(file_entry("Tools/enimas_updater.py", "src/Tools/enimas_updater.py", b"standalone updater"))
            archive_entries = [
                {"path": path, "size": len(data), "sha256": digest(data)}
                for path, data in release_files.items()
            ]
            manifest = make_manifest(entries, archive_files=archive_entries)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self._write_release_archive(root, manifest, release_files)
            updater.prepare_fresh_or_repair_install(manifest_path.as_uri(), root)
            paths = updater.UpdatePaths(root)
            self.assertFalse(paths.installed_manifest.exists())
            paths.journal.write_bytes(b"{corrupt interrupted transaction")
            with mock.patch.object(updater, "_run_health_check") as health:
                updater.complete_fresh_or_repair_install(root)
            health.assert_called_once()
            called_paths, called_version = health.call_args.args
            self.assertTrue(os.path.samefile(called_paths.install_root, root))
            self.assertEqual(called_version, "1.3.0")
            self.assertEqual(updater.read_json(paths.installed_manifest)["version"], "1.3.0")
            self.assertFalse(paths.journal.exists())
            install_state = updater.read_json(paths.install_journal)
            quarantine = (
                Path(install_state["backup_root"]) / "pre-repair-transaction.json"
            )
            self.assertEqual(
                quarantine.read_bytes(), b"{corrupt interrupted transaction"
            )
            self.assertFalse((root / "install.bat").exists())
            self.assertEqual((root / "src" / "install.bat").read_bytes(), b"new installer")
            self.assertEqual(updater.read_json(paths.install_journal)["phase"], "complete")
            version_stage = Path(updater.read_json(paths.install_journal)["version_stage"])
            self.assertTrue(version_stage.is_dir())
            self.assertTrue(updater.cleanup_completed_install(root))
            self.assertFalse(version_stage.exists())
            self.assertIn(
                "cleanup_completed_at", updater.read_json(paths.install_journal)
            )
            self.assertTrue(updater.cleanup_completed_install(root))

    def test_cached_archive_with_unlisted_venv_executable_is_rejected_before_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            marker = root / "src" / "old-marker.txt"
            marker.write_bytes(b"old installation")
            release_files = {"Tools/enimas_updater.py": b"standalone updater"}
            manifest = make_manifest()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self._write_release_archive(
                root,
                manifest,
                {
                    **release_files,
                    "venv/Scripts/python.exe": b"unlisted elevated payload",
                },
            )
            with self.assertRaises(updater.UpdateError):
                updater.prepare_fresh_or_repair_install(
                    manifest_path.as_uri(), root, repair=True
                )
            self.assertEqual(marker.read_bytes(), b"old installation")


if __name__ == "__main__":
    unittest.main()
