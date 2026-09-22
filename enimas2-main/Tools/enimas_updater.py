"""Transactional, interruption-safe updater for ENIMAS.

This module deliberately uses only the Python standard library.  Installed
copies are placed under ``<install root>/updates/bin`` and run with the system
Python, outside ``src`` and its virtual environment, so source files can be
replaced safely after ENIMAS exits.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import email.parser
import hashlib
import json
import logging
import os
import stat
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
DEFAULT_MANIFEST_URL = (
    "https://gitlab.kit.edu/kit/iai/ber/enimas2/-/raw/main/update_manifest.json"
)
DEFAULT_INSTALL_ROOT = Path(r"C:\Program Files\ENIMAS")
UPDATE_DIRNAME = "updates"
LOCK_STALE_SECONDS = 6 * 60 * 60
LOCK_INITIALIZATION_GRACE_SECONDS = 5.0
DOWNLOAD_ATTEMPTS = 3
FILE_RETRY_ATTEMPTS = 8
FILE_RETRY_DELAY = 0.35
DISK_SAFETY_BYTES = 64 * 1024 * 1024
FRESH_INSTALL_MIN_FREE_BYTES = 6 * 1024 * 1024 * 1024
MAX_RELEASE_EXTRACTED_BYTES = 5 * 1024 * 1024 * 1024
PYTORCH_CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"
ALLOWED_PIP_DOWNLOAD_ARGS = ("--extra-index-url", PYTORCH_CPU_INDEX_URL)
BOOTSTRAP_PACKAGE_NAMES = {"pip", "setuptools", "wheel"}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UPDATE_ACTIVE = 11
EXIT_NOT_AVAILABLE = 12
EXIT_RECOVERED = 13
EXIT_INSTALL_INCOMPLETE = 20
EXIT_MAINTENANCE_REQUIRED = 21
EXIT_REPAIR_REQUIRED = 22

# Releases before 1.3.0 did not write an installed manifest. The oldest
# repository-backed installation layout that can be migrated safely is 1.0.7.
# Anything older, structurally incomplete, or missing a manifest after 1.3.0
# must use the explicit data-preserving Repair path.
MIN_LEGACY_INCREMENTAL_VERSION = (1, 0, 7)
FIRST_MANIFEST_VERSION = (1, 3, 0)
LEGACY_REQUIRED_FILES = (
    "ENIMAS.bat",
    "src/config.txt",
    "src/main.py",
    "src/requirements.txt",
    "src/UserInterface/ui.py",
    "src/venv/Scripts/python.exe",
)

RESERVED_RELEASE_SUFFIXES = (
    ".update.tmp",
    ".rollback.tmp",
    ".verified.tmp",
    ".enimas-update-tombstone",
    ".enimas-rollback-tombstone",
)

ProgressCallback = Callable[[str, int, int, str], None]
LOGGER = logging.getLogger("enimas.updater")


class UpdateError(RuntimeError):
    """Base class for safe, user-facing update failures."""


class ManifestError(UpdateError):
    """Raised when a release manifest is malformed or unsafe."""


class CancelledError(UpdateError):
    """Raised when a user cancels before application begins."""


class UpdateActiveError(UpdateError):
    """Raised when another updater owns the installation lock."""


class MaintenanceRequiredError(UpdateError):
    """Raised when a release must use the explicit system-maintenance path."""


class RepairRequiredError(UpdateError):
    """Raised when an installation cannot safely use an incremental update."""


@dataclass(frozen=True)
class UpdatePaths:
    install_root: Path

    @property
    def updates(self) -> Path:
        return self.install_root / UPDATE_DIRNAME

    @property
    def staging(self) -> Path:
        return self.updates / "staging"

    @property
    def backups(self) -> Path:
        return self.updates / "backups"

    @property
    def bin(self) -> Path:
        return self.updates / "bin"

    @property
    def lock(self) -> Path:
        return self.updates / "update.lock"

    @property
    def journal(self) -> Path:
        return self.updates / "transaction.json"

    @property
    def installed_manifest(self) -> Path:
        return self.updates / "installed_manifest.json"

    @property
    def result(self) -> Path:
        return self.updates / "last_result.json"

    @property
    def log(self) -> Path:
        return self.updates / "update.log"

    @property
    def app_lock(self) -> Path:
        return self.updates / "app.lock"

    @property
    def install_journal(self) -> Path:
        return self.updates / "install_state.json"

    @property
    def latest_backup(self) -> Path:
        return self.updates / "latest_known_good.json"


def _noop_progress(_phase: str, _current: int, _total: int, _message: str) -> None:
    return


class _UpdateFileHandler(logging.Handler):
    """Append one record at a time so Windows never holds the update log open."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not self.path.parent.is_dir():
                return
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(self.format(record) + "\n")
        except Exception:
            self.handleError(record)


def configure_logging(paths: UpdatePaths, verbose: bool = False) -> None:
    paths.updates.mkdir(parents=True, exist_ok=True)
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    handlers: list[logging.Handler] = [_UpdateFileHandler(paths.log)]
    if verbose:
        handlers.append(logging.StreamHandler())
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in handlers:
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOGGER.propagate = False


def _flush_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source: Path | str, destination: Path | str) -> None:
    source = Path(source)
    destination = Path(destination)
    last_error: OSError | None = None
    if os.name == "nt":
        import ctypes

        movefile_replace_existing = 0x1
        movefile_write_through = 0x8
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move_file.restype = ctypes.c_int
        for attempt in range(FILE_RETRY_ATTEMPTS):
            if move_file(
                str(source),
                str(destination),
                movefile_replace_existing | movefile_write_through,
            ):
                return
            last_error = ctypes.WinError(ctypes.get_last_error())
            if attempt < FILE_RETRY_ATTEMPTS - 1:
                time.sleep(FILE_RETRY_DELAY)
        raise last_error
    for attempt in range(FILE_RETRY_ATTEMPTS):
        try:
            os.replace(source, destination)
            _flush_directory(destination.parent)
            return
        except OSError as exc:
            last_error = exc
            if attempt < FILE_RETRY_ATTEMPTS - 1:
                time.sleep(FILE_RETRY_DELAY)
    raise last_error or OSError(f"Could not replace {destination}")


def _durable_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".copying.tmp")
    with source.open("rb") as input_handle, temporary.open("wb") as output_handle:
        shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    shutil.copystat(source, temporary)
    _durable_replace(temporary, destination)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"State file is unreadable or corrupt: {path}") from exc


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse stable ``major.minor.patch`` versions used by the public channel."""
    if not isinstance(value, str):
        raise ManifestError("Version must be a string")
    parts = value.strip().split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ManifestError(f"Unsupported stable version: {value!r}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def is_newer_version(target: str, current: str) -> bool:
    return parse_version(target) > parse_version(current)


def _validated_pip_download_args(manifest: dict[str, Any]) -> list[str]:
    value = manifest.get("pip_download_args", [])
    if value == []:
        return []
    if value == list(ALLOWED_PIP_DOWNLOAD_ARGS):
        return list(ALLOWED_PIP_DOWNLOAD_ARGS)
    raise ManifestError(
        "pip_download_args may only select the pinned PyTorch CPU wheel index"
    )


def _safe_relative(value: str, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty relative path")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or ":" in normalized or "\x00" in normalized:
        raise ManifestError(f"Unsafe {field}: {value!r}")
    if any(part in ("", ".") for part in path.parts):
        raise ManifestError(f"Invalid {field}: {value!r}")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    for part in path.parts:
        if part.endswith((" ", ".")) or part.split(".", 1)[0].upper() in reserved:
            raise ManifestError(f"Windows-unsafe {field}: {value!r}")
    return path


def is_preserved_destination(destination: str) -> bool:
    normalized = _safe_relative(destination, "destination").as_posix().casefold()
    exact = {
        "src/config.txt",
        "src/lenses.json",
        "src/plugins/measurement/plugin_m_config.json",
    }
    prefixes = ("src/models/classification/", "src/venv/")
    return normalized in exact or normalized.startswith(prefixes)


def safe_destination(install_root: Path, destination: str) -> Path:
    relative = _safe_relative(destination, "destination")
    root = install_root.resolve()
    if install_root.is_symlink() or _is_reparse_point(install_root):
        raise ManifestError("Installation root cannot be a link or reparse point")
    unresolved = root / Path(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and (
            current.is_symlink() or _is_reparse_point(current)
        ):
            raise ManifestError(
                f"Destination crosses a link or reparse point: {destination!r}"
            )
    candidate = unresolved.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ManifestError(f"Destination escapes installation root: {destination!r}") from exc
    return candidate


def _reject_reserved_release_path(relative: PurePosixPath, label: str) -> None:
    lowered = relative.as_posix().casefold()
    if any(lowered.endswith(suffix) for suffix in RESERVED_RELEASE_SUFFIXES):
        raise ManifestError(f"{label} collides with updater transaction files: {relative}")


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ManifestError("Manifest root must be an object")
    required = {
        "schema_version",
        "version",
        "source_ref",
        "repository_raw_url",
        "release_date",
        "files",
        "archive_files",
        "system_files",
        "removed",
        "requirements_sha256",
        "system_update_required",
        "updater",
    }
    missing = sorted(required - manifest.keys())
    if missing:
        raise ManifestError(f"Manifest is missing: {', '.join(missing)}")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ManifestError(
            f"Unsupported manifest schema {manifest['schema_version']}; expected {SCHEMA_VERSION}"
        )
    parse_version(manifest["version"])
    source_ref = str(manifest["source_ref"])
    if not source_ref or any(ch.isspace() for ch in source_ref) or ".." in source_ref:
        raise ManifestError("source_ref is unsafe")
    if source_ref != f"v{manifest['version']}":
        raise ManifestError("source_ref must be the immutable release tag v<version>")
    try:
        datetime.date.fromisoformat(str(manifest["release_date"]))
    except ValueError as exc:
        raise ManifestError("release_date must use YYYY-MM-DD") from exc
    if not isinstance(manifest["system_update_required"], bool):
        raise ManifestError("system_update_required must be boolean")
    requirements_digest = str(manifest["requirements_sha256"])
    if requirements_digest and (
        len(requirements_digest) != 64
        or any(ch not in "0123456789abcdef" for ch in requirements_digest.lower())
    ):
        raise ManifestError("requirements_sha256 must be empty or a SHA-256 digest")
    raw_url = str(manifest["repository_raw_url"])
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ManifestError("repository_raw_url must be HTTPS")
    if raw_url.count("{ref}") != 1 or "{" in raw_url.replace("{ref}", "") or "}" in raw_url.replace("{ref}", ""):
        raise ManifestError("repository_raw_url must contain exactly one {ref} placeholder")
    _validated_pip_download_args(manifest)

    files = manifest["files"]
    if not isinstance(files, list):
        raise ManifestError("files must be a list")
    destinations: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ManifestError("Each files entry must be an object")
        for field in ("path", "destination", "sha256", "size"):
            if field not in entry:
                raise ManifestError(f"File entry is missing {field}")
        source_path = _safe_relative(entry["path"], "path")
        _reject_reserved_release_path(source_path, "path")
        destination = _safe_relative(entry["destination"], "destination").as_posix()
        _reject_reserved_release_path(PurePosixPath(destination), "destination")
        destination_key = destination.casefold()
        if destination_key in destinations:
            raise ManifestError(f"Duplicate destination: {destination}")
        if is_preserved_destination(destination):
            raise ManifestError(f"Manifest attempts to manage preserved data: {destination}")
        destinations.add(destination_key)
        digest = str(entry["sha256"]).lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ManifestError(f"Invalid SHA-256 for {entry['path']}")
        if not isinstance(entry["size"], int) or entry["size"] < 0:
            raise ManifestError(f"Invalid size for {entry['path']}")

    system_files = manifest["system_files"]
    if not isinstance(system_files, list):
        raise ManifestError("system_files must be a list")
    system_paths: set[str] = set()
    for entry in system_files:
        if not isinstance(entry, dict):
            raise ManifestError("Each system_files entry must be an object")
        for field in ("path", "sha256", "size"):
            if field not in entry:
                raise ManifestError(f"System file entry is missing {field}")
        system_path = _safe_relative(entry["path"], "system file path").as_posix()
        _reject_reserved_release_path(PurePosixPath(system_path), "system file path")
        system_key = system_path.casefold()
        if system_key in system_paths:
            raise ManifestError(f"Duplicate system file path: {system_path}")
        if not system_key.startswith(("camera-driver/",)):
            raise ManifestError(f"Unsupported system file path: {system_path}")
        system_paths.add(system_key)
        digest = str(entry["sha256"]).lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ManifestError(f"Invalid SHA-256 for {entry['path']}")
        if not isinstance(entry["size"], int) or entry["size"] < 0:
            raise ManifestError(f"Invalid size for {entry['path']}")

    archive_files = manifest["archive_files"]
    if not isinstance(archive_files, list):
        raise ManifestError("archive_files must be a list")
    archive_entries: dict[str, dict[str, Any]] = {}
    for entry in archive_files:
        if not isinstance(entry, dict):
            raise ManifestError("Each archive_files entry must be an object")
        for field in ("path", "sha256", "size"):
            if field not in entry:
                raise ManifestError(f"Archive file entry is missing {field}")
        archive_path = _safe_relative(entry["path"], "archive file path").as_posix()
        _reject_reserved_release_path(PurePosixPath(archive_path), "archive file path")
        archive_key = archive_path.casefold()
        if archive_key in archive_entries:
            raise ManifestError(f"Duplicate archive file path: {archive_path}")
        digest = str(entry["sha256"]).lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ManifestError(f"Invalid SHA-256 for {entry['path']}")
        if not isinstance(entry["size"], int) or entry["size"] < 0:
            raise ManifestError(f"Invalid size for {entry['path']}")
        archive_entries[archive_key] = entry

    for entry in [*files, *system_files]:
        archive_entry = archive_entries.get(str(entry["path"]).casefold())
        if archive_entry is None:
            raise ManifestError(f"Release payload is absent from archive_files: {entry['path']}")
        if (
            archive_entry["size"] != entry["size"]
            or str(archive_entry["sha256"]).lower() != str(entry["sha256"]).lower()
        ):
            raise ManifestError(f"Archive metadata disagrees for {entry['path']}")

    removed = manifest["removed"]
    if not isinstance(removed, list):
        raise ManifestError("removed must be a list")
    removed_keys: set[str] = set()
    for destination in removed:
        normalized = _safe_relative(destination, "removed destination").as_posix()
        _reject_reserved_release_path(PurePosixPath(normalized), "removed destination")
        key = normalized.casefold()
        if key in removed_keys:
            raise ManifestError(f"Duplicate removed destination: {normalized}")
        removed_keys.add(key)
        if is_preserved_destination(normalized):
            raise ManifestError(f"Manifest attempts to remove preserved data: {normalized}")
        if key in destinations:
            raise ManifestError(f"Destination cannot be installed and removed: {normalized}")

    updater = manifest["updater"]
    if not isinstance(updater, dict) or not {"path", "sha256"} <= updater.keys():
        raise ManifestError("updater must contain path and sha256")
    _safe_relative(updater["path"], "updater.path")
    if updater["sha256"] != next(
        (entry["sha256"] for entry in files if entry["path"] == updater["path"]), None
    ):
        raise ManifestError("updater SHA-256 must match its managed file entry")
    return manifest


def _open_url(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)  # nosec B310 - HTTPS validated


def fetch_manifest(url: str = DEFAULT_MANIFEST_URL, timeout: float = 15.0) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"https", "file"}:
        raise ManifestError("Manifest URL must use HTTPS or a local file URL")
    request = urllib.request.Request(url, headers={"User-Agent": "ENIMAS-Updater/1"})
    try:
        with _open_url(request, timeout) as response:
            data = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError(f"Could not download the update manifest: {exc}") from exc
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("The update manifest is not valid UTF-8 JSON") from exc
    return validate_manifest(manifest)


def release_file_url(manifest: dict[str, Any], source_path: str) -> str:
    relative = _safe_relative(source_path, "path")
    base = manifest["repository_raw_url"].format(
        ref=urllib.parse.quote(str(manifest["source_ref"]), safe="")
    ).rstrip("/")
    quoted = "/".join(urllib.parse.quote(part, safe="") for part in relative.parts)
    return f"{base}/{quoted}"


def read_local_version(install_root: Path) -> str:
    constants_path = install_root / "src" / "constants.py"
    if constants_path.is_file():
        import re

        text = constants_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if match:
            try:
                parse_version(match.group(1))
                return match.group(1)
            except ManifestError:
                pass
    config_path = install_root / "src" / "config.txt"
    if config_path.is_file():
        for line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("version="):
                value = line.split("=", 1)[1].strip()
                try:
                    parse_version(value)
                    return value
                except ManifestError:
                    break
    return "0.0.0"


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        # Access denied still proves that the process exists.
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def process_start_marker(pid: int) -> str | None:
    """Return a creation marker so PID reuse cannot retain a stale lock."""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()[21]
    except (OSError, IndexError):
        return None


def lock_owner_is_alive(payload: dict[str, Any]) -> bool:
    pid = int(payload.get("pid", 0) or 0)
    if not pid_is_alive(pid):
        return False
    recorded = payload.get("process_marker")
    current = process_start_marker(pid)
    return not recorded or not current or str(recorded) == current


def _read_lock_payload(path: Path) -> dict[str, Any] | None:
    try:
        payload = read_json(path)
    except UpdateError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return None
        if age < LOCK_INITIALIZATION_GRACE_SECONDS:
            raise UpdateActiveError("Another ENIMAS process is acquiring the update lock")
        return None
    return payload if isinstance(payload, dict) else None


class UpdateLock:
    def __init__(self, paths: UpdatePaths):
        self.paths = paths
        self.owned = False

    def acquire(self) -> None:
        self.paths.updates.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "process_marker": process_start_marker(os.getpid()),
            "created_at": time.time(),
        }
        for _ in range(2):
            try:
                fd = os.open(self.paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                self.owned = True
                return
            except FileExistsError:
                existing = _read_lock_payload(self.paths.lock) or {}
                pid = int(existing.get("pid", 0) or 0)
                if lock_owner_is_alive(existing):
                    raise UpdateActiveError(f"Another ENIMAS updater is running (PID {pid})")
                with contextlib.suppress(OSError):
                    self.paths.lock.unlink()
        raise UpdateActiveError("Could not acquire the ENIMAS update lock")

    def release(self) -> None:
        if self.owned:
            with contextlib.suppress(OSError):
                self.paths.lock.unlink()
            self.owned = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.release()


def _cancelled(cancel_event: Any) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def _download_with_resume(
    url: str,
    final_path: Path,
    expected_size: int,
    expected_sha256: str,
    *,
    timeout: float = 30.0,
    attempts: int = DOWNLOAD_ATTEMPTS,
    progress: ProgressCallback = _noop_progress,
    cancel_event: Any = None,
) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.is_file() and final_path.stat().st_size == expected_size:
        if sha256_file(final_path) == expected_sha256:
            return
        final_path.unlink()
    part_path = final_path.with_name(final_path.name + ".part")
    if part_path.is_file() and part_path.stat().st_size == expected_size:
        progress("verifying", expected_size, expected_size, final_path.name)
        if sha256_file(part_path) == expected_sha256:
            _durable_replace(part_path, final_path)
            return
        part_path.unlink()

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        if _cancelled(cancel_event):
            raise CancelledError("Update download cancelled")
        offset = part_path.stat().st_size if part_path.exists() else 0
        if offset > expected_size:
            part_path.unlink()
            offset = 0
        headers = {"User-Agent": "ENIMAS-Updater/1", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            with _open_url(request, timeout) as response:
                status = getattr(response, "status", response.getcode())
                if offset and status != 206:
                    part_path.unlink(missing_ok=True)
                    offset = 0
                    raise UpdateError("Server did not honor the resume request; restarting file")
                mode = "ab" if offset else "wb"
                downloaded = offset
                with part_path.open(mode) as handle:
                    while True:
                        if _cancelled(cancel_event):
                            raise CancelledError("Update download cancelled")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        downloaded += len(chunk)
                        progress("downloading", downloaded, expected_size, final_path.name)
                    handle.flush()
                    os.fsync(handle.fileno())
            if part_path.stat().st_size != expected_size:
                raise UpdateError(
                    f"Downloaded size mismatch for {final_path.name}: "
                    f"{part_path.stat().st_size} != {expected_size}"
                )
            progress("verifying", expected_size, expected_size, final_path.name)
            if sha256_file(part_path) != expected_sha256:
                part_path.unlink(missing_ok=True)
                raise UpdateError(f"SHA-256 verification failed for {final_path.name}")
            _durable_replace(part_path, final_path)
            return
        except CancelledError:
            raise
        except Exception as exc:  # retry network, short read, and transient filesystem failures
            last_error = exc
            LOGGER.warning("Download attempt %s/%s failed for %s: %s", attempt, attempts, url, exc)
            if attempt < attempts:
                time.sleep(min(4.0, 0.5 * (2 ** (attempt - 1))))
    raise UpdateError(f"Could not download {url}: {last_error}")


def _load_installed_manifest(paths: UpdatePaths) -> dict[str, Any] | None:
    payload = read_json(paths.installed_manifest)
    if not payload:
        return None
    try:
        return validate_manifest(payload)
    except ManifestError:
        LOGGER.warning("Ignoring invalid installed manifest at %s", paths.installed_manifest)
        return None


def validate_incremental_installation(
    install_root: Path | str,
    installed_manifest: dict[str, Any] | None = None,
) -> str:
    """Return the installed version or require a data-preserving Repair.

    ENIMAS 1.0.7 through 1.2.x predate installed manifests, so their known
    layout is checked explicitly. Manifest-era installations must retain a
    valid manifest matching the live application version; silently treating a
    damaged modern installation as legacy would weaken removal and rollback
    guarantees.
    """

    root = Path(install_root).resolve()
    paths = UpdatePaths(root)
    if installed_manifest is None:
        installed_manifest = _load_installed_manifest(paths)
    current_text = read_local_version(root)
    current = parse_version(current_text)

    if installed_manifest is not None:
        manifest_version = str(installed_manifest.get("version", ""))
        if manifest_version != current_text:
            raise RepairRequiredError(
                "The installed release record does not match the live ENIMAS version. "
                "Run install.bat --repair; user data will be preserved."
            )
        return current_text

    if current < MIN_LEGACY_INCREMENTAL_VERSION:
        minimum = ".".join(str(part) for part in MIN_LEGACY_INCREMENTAL_VERSION)
        raise RepairRequiredError(
            f"ENIMAS {current_text} is older than the supported incremental migration "
            f"baseline {minimum}. Run install.bat --repair; user data will be preserved."
        )
    if current >= FIRST_MANIFEST_VERSION:
        raise RepairRequiredError(
            f"ENIMAS {current_text} is missing its installed release record. "
            "Run install.bat --repair; user data will be preserved."
        )

    missing = [
        relative
        for relative in LEGACY_REQUIRED_FILES
        if not safe_destination(root, relative).is_file()
    ]
    if missing:
        raise RepairRequiredError(
            "The legacy ENIMAS installation is incomplete (missing: "
            + ", ".join(missing)
            + "). Run install.bat --repair; user data will be preserved."
        )
    return current_text


def calculate_update_plan(
    manifest: dict[str, Any], install_root: Path, installed_manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    manifest = validate_manifest(manifest)
    old_entries = {
        entry["destination"].casefold(): (entry["destination"], entry)
        for entry in (installed_manifest or {}).get("files", [])
    }
    operations: list[dict[str, Any]] = []
    for entry in manifest["files"]:
        destination = safe_destination(install_root, entry["destination"])
        matches = False
        if destination.is_file():
            # The installed manifest proves what was released, not that a managed
            # file has remained unmodified. Verify the live file every time.
            matches = (
                destination.stat().st_size == entry["size"]
                and sha256_file(destination) == entry["sha256"]
            )
        if not matches:
            old_pair = old_entries.get(entry["destination"].casefold())
            old_entry = old_pair[1] if old_pair else None
            locally_modified = False
            if destination.is_file() and old_entry:
                locally_modified = (
                    destination.stat().st_size != old_entry.get("size")
                    or sha256_file(destination) != old_entry.get("sha256")
                )
            operations.append(
                {
                    "kind": "replace" if destination.exists() else "add",
                    "path": entry["path"],
                    "destination": entry["destination"],
                    "sha256": entry["sha256"],
                    "size": entry["size"],
                    "locally_modified": locally_modified,
                    "status": "pending",
                }
            )

    allowed_removals = set(manifest["removed"])
    if installed_manifest:
        new_destinations = {entry["destination"].casefold() for entry in manifest["files"]}
        allowed_removals.update(
            original for key, (original, _entry) in old_entries.items() if key not in new_destinations
        )
    for destination_text in sorted(allowed_removals):
        destination = safe_destination(install_root, destination_text)
        if destination.is_file() or destination.is_symlink():
            operations.append(
                {
                    "kind": "remove",
                    "destination": destination_text,
                    "status": "pending",
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "target_version": manifest["version"],
        "current_version": read_local_version(install_root),
        "manifest": manifest,
        "operations": operations,
        "dependencies_changed": _requirements_changed(manifest, install_root),
        "created_at": time.time(),
        "phase": "prepared",
    }


def _requirements_changed(manifest: dict[str, Any], install_root: Path) -> bool:
    expected = str(manifest.get("requirements_sha256") or "")
    current = install_root / "src" / "requirements.lock"
    if not expected:
        return False
    return not current.is_file() or sha256_file(current) != expected


def _preflight_disk_space(paths: UpdatePaths, plan: dict[str, Any]) -> None:
    download_bytes = sum(
        int(op.get("size", 0)) for op in plan["operations"] if op["kind"] != "remove"
    )
    backup_bytes = 0
    for op in plan["operations"]:
        destination = safe_destination(paths.install_root, op["destination"])
        if destination.is_file():
            backup_bytes += destination.stat().st_size
    dependency_bytes = 0
    if plan.get("dependencies_changed"):
        venv = paths.install_root / "src" / "venv"
        current_environment_size = _tree_size(venv)
        # New and previous wheelhouses plus pip's temporary unpacking space.
        dependency_bytes = max(current_environment_size * 2, 1024 * 1024 * 1024)
    required = download_bytes + backup_bytes + dependency_bytes + DISK_SAFETY_BYTES
    free = shutil.disk_usage(paths.install_root).free
    if free < required:
        raise UpdateError(
            f"Not enough disk space for a safe update. Required {required / 1024**2:.0f} MB; "
            f"available {free / 1024**2:.0f} MB."
        )


def _walk_unlinked_files(root: Path) -> Iterable[Path]:
    """Yield regular files without traversing any link or Windows reparse point."""
    if root.is_symlink() or _is_reparse_point(root):
        raise UpdateError(f"Refused linked user-data path: {root}")
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            candidate = current_path / name
            if candidate.is_symlink() or _is_reparse_point(candidate):
                raise UpdateError(f"Refused linked user-data path: {candidate}")
        for name in files:
            candidate = current_path / name
            if candidate.is_symlink() or _is_reparse_point(candidate):
                raise UpdateError(f"Refused linked user-data path: {candidate}")
            if candidate.is_file():
                yield candidate


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if not path.is_dir():
        return 0
    return sum(item.stat().st_size for item in _walk_unlinked_files(path))


LEGACY_ROOT_USER_DATA = (
    ("config_backup.txt", "src/config.txt"),
    ("lenses.json", "src/lenses.json"),
    ("venv_backup", "src/venv"),
)


def _legacy_root_recovery_paths(install_root: Path) -> list[tuple[Path, str]]:
    """Return interrupted pre-transaction updater backups still needing restore."""
    recoverable: list[tuple[Path, str]] = []
    for source_text, destination_text in LEGACY_ROOT_USER_DATA:
        source = safe_destination(install_root, source_text)
        destination = safe_destination(install_root, destination_text)
        if source.exists() and not destination.exists():
            recoverable.append((source, destination_text))
    return recoverable


def _preflight_install_disk_space(paths: UpdatePaths, manifest: dict[str, Any]) -> None:
    archive_bytes = sum(int(entry["size"]) for entry in manifest["archive_files"])
    existing_source_bytes = _tree_size(paths.install_root / "src")
    existing_venv_bytes = _tree_size(paths.install_root / "src" / "venv")
    legacy_recovery_bytes = sum(
        _tree_size(source) if source.is_dir() else source.stat().st_size
        for source, _destination in _legacy_root_recovery_paths(paths.install_root)
    )
    # The source archive and extracted tree coexist. During repair the old tree
    # is retained as rollback data while preserved content is copied into a
    # separate user backup and then into the new active source.
    calculated = (
        archive_bytes * 2
        + existing_source_bytes * 2
        + legacy_recovery_bytes * 2
        + max(existing_venv_bytes, 1024 * 1024 * 1024)
        + DISK_SAFETY_BYTES
    )
    required = max(calculated, FRESH_INSTALL_MIN_FREE_BYTES)
    free = shutil.disk_usage(paths.install_root).free
    if free < required:
        raise UpdateError(
            f"Fresh installation or repair needs about {required / 1024**3:.1f} GB free "
            f"for verified staging and rollback; {free / 1024**3:.1f} GB is available"
        )


def _test_write_access(paths: UpdatePaths) -> None:
    paths.updates.mkdir(parents=True, exist_ok=True)
    for directory in (paths.install_root, paths.updates, paths.install_root / "src"):
        if not directory.is_dir():
            raise UpdateError(f"Required installation directory is missing: {directory}")
        try:
            fd, probe = tempfile.mkstemp(prefix=".enimas-write-test-", dir=directory)
            os.close(fd)
            os.unlink(probe)
        except OSError as exc:
            raise PermissionError(f"ENIMAS cannot update {directory}: {exc}") from exc


def _copy_updater_to_external_location(paths: UpdatePaths, manifest: dict[str, Any], staging_files: Path) -> Path:
    updater_entry = next(entry for entry in manifest["files"] if entry["path"] == manifest["updater"]["path"])
    staged = staging_files / Path(*PurePosixPath(updater_entry["destination"]).parts)
    if not staged.is_file():
        installed = safe_destination(paths.install_root, updater_entry["destination"])
        if installed.is_file() and sha256_file(installed) == updater_entry["sha256"]:
            staged = installed
        else:
            raise UpdateError("Verified standalone updater is unavailable")
    external = paths.bin / manifest["version"] / "enimas_updater.py"
    paths.bin.mkdir(parents=True, exist_ok=True)
    external.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(staged, external)
    if sha256_file(external) != manifest["updater"]["sha256"]:
        raise UpdateError("External updater verification failed")
    try:
        compile(external.read_text(encoding="utf-8"), str(external), "exec")
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise UpdateError(f"Standalone updater is not executable Python: {exc}") from exc
    return external


def _prepare_dependency_wheelhouse(
    paths: UpdatePaths,
    plan: dict[str, Any],
    staging_files: Path,
    progress: ProgressCallback,
) -> None:
    if not plan["dependencies_changed"]:
        return
    requirements_entry = next(
        (entry for entry in plan["manifest"]["files"] if entry["destination"] == "src/requirements.lock"),
        None,
    )
    if requirements_entry is None:
        raise UpdateError("requirements_sha256 changed without a managed requirements.lock")
    requirements = staging_files / Path(*PurePosixPath(requirements_entry["destination"]).parts)
    # Hashed lock files may reference release-local wheels. Unchanged wheels do
    # not otherwise need downloading, so materialize their verified installed
    # copies into this staged source view before pip resolves relative paths.
    for entry in plan["manifest"]["files"]:
        destination_text = str(entry["destination"])
        if not destination_text.casefold().startswith("src/vendor/"):
            continue
        staged_vendor = staging_files / Path(*PurePosixPath(destination_text).parts)
        if staged_vendor.is_file():
            if sha256_file(staged_vendor) != entry["sha256"]:
                raise UpdateError(f"Staged dependency artifact is corrupt: {destination_text}")
            continue
        installed_vendor = safe_destination(paths.install_root, destination_text)
        if not installed_vendor.is_file() or sha256_file(installed_vendor) != entry["sha256"]:
            raise UpdateError(f"Verified dependency artifact is unavailable: {destination_text}")
        staged_vendor.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(installed_vendor, staged_vendor)
        if sha256_file(staged_vendor) != entry["sha256"]:
            raise UpdateError(f"Dependency artifact changed while staging: {destination_text}")
    venv_python = paths.install_root / "src" / "venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        raise UpdateError("Existing ENIMAS virtual environment is missing; use Repair installation")
    wheelhouse = paths.staging / plan["target_version"] / "wheelhouse"
    if wheelhouse.exists():
        shutil.rmtree(wheelhouse)
    wheelhouse.mkdir(parents=True, exist_ok=True)
    progress("dependencies", 0, 1, "Preparing dependency packages")
    command = [
        str(venv_python),
        "-m",
        "pip",
        "wheel",
        "--no-cache-dir",
        "--wheel-dir",
        str(wheelhouse),
        "--require-hashes",
        "--requirement",
        str(requirements),
    ]
    extra_args = _validated_pip_download_args(plan["manifest"])
    command.extend(extra_args)
    # Local locked artifacts (for example the vendored refiners wheel) are
    # resolved relative to the staged release, never a user-controlled cwd.
    completed = subprocess.run(command, cwd=requirements.parent, check=False)
    if completed.returncode != 0:
        raise UpdateError("Could not prepare dependency packages; installed ENIMAS was not changed")
    previous_freeze = paths.staging / plan["target_version"] / "previous-requirements-freeze.txt"
    previous_snapshot = _installed_requirements_snapshot(
        venv_python, paths.install_root / "src"
    )
    atomic_write_text(previous_freeze, previous_snapshot)
    if previous_snapshot.strip():
        previous_wheelhouse = paths.staging / plan["target_version"] / "previous-wheelhouse"
        previous_wheelhouse.mkdir(parents=True, exist_ok=True)
        previous_command = [
            str(venv_python),
            "-m",
            "pip",
            "wheel",
            "--no-cache-dir",
            "--wheel-dir",
            str(previous_wheelhouse),
            "--find-links",
            str(wheelhouse),
            "--requirement",
            str(previous_freeze),
        ]
        previous_command.extend(extra_args)
        previous = subprocess.run(previous_command, cwd=paths.install_root / "src", check=False)
        if previous.returncode != 0:
            raise UpdateError("Could not stage the previous dependency environment for rollback")
        _write_wheelhouse_lock(
            previous_wheelhouse,
            paths.staging / plan["target_version"] / "previous-requirements.lock",
        )
    progress("dependencies", 1, 1, "Dependency packages ready")


def _validated_distribution_identity(name: Any, version: Any, label: str) -> tuple[str, str]:
    clean_name = str(name or "").strip()
    clean_version = str(version or "").strip()
    if (
        not clean_name
        or not clean_version
        or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for ch in clean_name
        )
        or any(ch.isspace() for ch in clean_version)
        or any(ch in "\\/;" for ch in clean_version)
    ):
        raise UpdateError(f"{label} has unsafe name/version metadata")
    return clean_name, clean_version


def _installed_requirements_snapshot(venv_python: Path, cwd: Path) -> str:
    """Record installed versions without retaining VCS or local source URLs."""
    completed = subprocess.run(
        [str(venv_python), "-m", "pip", "list", "--format=json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise UpdateError("Could not record the existing dependency environment")
    try:
        inventory = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UpdateError("Existing dependency inventory is not valid JSON") from exc
    if not isinstance(inventory, list):
        raise UpdateError("Existing dependency inventory is not a list")
    packages: dict[str, tuple[str, str]] = {}
    for entry in inventory:
        if not isinstance(entry, dict):
            raise UpdateError("Existing dependency inventory contains an invalid entry")
        name, version = _validated_distribution_identity(
            entry.get("name"), entry.get("version"), "Installed dependency"
        )
        normalized = _normalize_package_name(name)
        if normalized in BOOTSTRAP_PACKAGE_NAMES:
            continue
        existing = packages.get(normalized)
        if existing is not None and existing[1] != version:
            raise UpdateError(f"Installed dependency inventory duplicates {name}")
        packages[normalized] = (name, version)
    return "".join(
        f"{name}=={version}\n" for _key, (name, version) in sorted(packages.items())
    )


def _read_wheel_identity(wheel: Path, label: str) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(wheel) as bundle:
            metadata_names = []
            for archive_name in bundle.namelist():
                parts = PurePosixPath(archive_name).parts
                if (
                    len(parts) == 2
                    and parts[0].endswith(".dist-info")
                    and parts[1] == "METADATA"
                ):
                    metadata_names.append(archive_name)
            if len(metadata_names) != 1:
                raise UpdateError(f"{label} has invalid metadata: {wheel.name}")
            message = email.parser.BytesParser().parsebytes(bundle.read(metadata_names[0]))
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise UpdateError(f"{label} is unreadable: {wheel.name}") from exc
    return _validated_distribution_identity(
        message.get("Name"), message.get("Version"), f"{label} {wheel.name}"
    )


def _write_wheelhouse_lock(wheelhouse: Path, destination: Path) -> None:
    """Create an offline hash lock from wheel metadata, never pip-freeze URLs."""
    packages: dict[tuple[str, str], tuple[str, list[str]]] = {}
    for wheel in sorted(wheelhouse.glob("*.whl")):
        name, version = _read_wheel_identity(wheel, "Rollback wheel")
        key = (name.casefold().replace("_", "-").replace(".", "-"), version)
        display_name, hashes = packages.setdefault(key, (name, []))
        hashes.append(sha256_file(wheel))
        packages[key] = (display_name, hashes)
    if not packages:
        raise UpdateError("Previous dependency wheelhouse is empty")
    lines: list[str] = []
    continuation = chr(92)
    for (normalized_name, version), (display_name, hashes) in sorted(packages.items()):
        del normalized_name
        lines.append(f"{display_name}=={version} {continuation}")
        for index, digest in enumerate(sorted(hashes)):
            suffix = f" {continuation}" if index < len(hashes) - 1 else ""
            lines.append(f"    --hash=sha256:{digest}{suffix}")
    atomic_write_text(destination, "\n".join(lines) + "\n")


def _normalize_package_name(name: str) -> str:
    return name.strip().casefold().replace("_", "-").replace(".", "-")


def _wheel_name(wheel: Path) -> str:
    name, _version = _read_wheel_identity(wheel, "Dependency wheel")
    return _normalize_package_name(name)


def _wheelhouse_package_names(wheelhouse: Path) -> set[str]:
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise UpdateError("Dependency wheelhouse is empty")
    unexpected = [
        path.name for path in wheelhouse.iterdir() if path.is_file() and path.suffix != ".whl"
    ]
    if unexpected:
        raise UpdateError(
            "Dependency wheelhouse contains a non-wheel artifact: " + ", ".join(unexpected)
        )
    return {_wheel_name(wheel) for wheel in wheels}


def _locked_requirement_names(requirements: Path) -> set[str]:
    """Return the distributions authorized by a generated hashed lock file."""
    names: set[str] = set()
    for raw_line in requirements.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line[0].isspace():
            continue
        line = raw_line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        if line.endswith("\\"):
            line = line[:-1].rstrip()
        line = line.split(" ;", 1)[0].strip()
        token = line.split()[0]
        if token.startswith(("./", ".\\")) or token.casefold().endswith(".whl"):
            wheel = (requirements.parent / token).resolve()
            try:
                wheel.relative_to(requirements.parent.resolve())
            except ValueError as exc:
                raise UpdateError(f"Locked wheel escapes the release directory: {token}") from exc
            if not wheel.is_file():
                raise UpdateError(f"Locked wheel is missing: {token}")
            names.add(_wheel_name(wheel))
            continue
        if " @ " in line:
            display_name = line.split(" @ ", 1)[0]
        elif "===" in line:
            display_name = line.split("===", 1)[0]
        elif "==" in line:
            display_name = line.split("==", 1)[0]
        else:
            raise UpdateError(f"Unsupported requirement in authoritative lock: {line}")
        display_name = display_name.split("[", 1)[0].strip()
        if not display_name or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for ch in display_name
        ):
            raise UpdateError(f"Unsafe requirement name in authoritative lock: {line}")
        names.add(_normalize_package_name(display_name))
    if not names:
        raise UpdateError("Authoritative dependency lock is empty")
    return names


def _installed_package_names(venv_python: Path, cwd: Path) -> set[str]:
    completed = subprocess.run(
        [str(venv_python), "-m", "pip", "list", "--format=json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise UpdateError("Could not inspect the ENIMAS dependency environment")
    try:
        payload = json.loads(completed.stdout)
        names = {
            _normalize_package_name(str(item["name"]))
            for item in payload
            if isinstance(item, dict) and item.get("name")
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise UpdateError("pip returned an invalid dependency inventory") from exc
    return names


def _synchronize_dependency_environment(
    venv_python: Path,
    allowed_names: set[str],
    cwd: Path,
) -> None:
    """Remove distributions not authorized by the release lock and verify closure."""
    bootstrap_tools = {"pip", "setuptools", "wheel"}
    installed = _installed_package_names(venv_python, cwd)
    extras = sorted(installed - allowed_names - bootstrap_tools)
    if extras:
        removed = subprocess.run(
            [str(venv_python), "-m", "pip", "uninstall", "-y", *extras],
            cwd=cwd,
            check=False,
        )
        if removed.returncode != 0:
            raise UpdateError("Could not remove packages absent from the target dependency lock")
    verified = _installed_package_names(venv_python, cwd)
    missing = sorted(allowed_names - verified)
    remaining_extras = sorted(verified - allowed_names - bootstrap_tools)
    if missing or remaining_extras:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if remaining_extras:
            details.append("unexpected: " + ", ".join(remaining_extras))
        raise UpdateError(
            "Dependency environment does not match the target lock (" + "; ".join(details) + ")"
        )


def synchronize_installed_dependencies(install_root: Path | str) -> None:
    root = Path(install_root).resolve()
    requirements = root / "src" / "requirements.lock"
    venv_python = root / "src" / "venv" / "Scripts" / "python.exe"
    if not requirements.is_file() or not venv_python.is_file():
        raise UpdateError("ENIMAS dependency synchronization requires a complete source and venv")
    _synchronize_dependency_environment(
        venv_python,
        _locked_requirement_names(requirements),
        requirements.parent,
    )


def prepare_update(
    manifest: dict[str, Any],
    install_root: Path | str,
    *,
    progress: ProgressCallback = _noop_progress,
    cancel_event: Any = None,
) -> Path:
    paths = UpdatePaths(Path(install_root).resolve())
    with UpdateLock(paths):
        return _prepare_update_locked(
            manifest,
            paths.install_root,
            progress=progress,
            cancel_event=cancel_event,
        )


def _prepare_update_locked(
    manifest: dict[str, Any],
    install_root: Path | str,
    *,
    progress: ProgressCallback = _noop_progress,
    cancel_event: Any = None,
) -> Path:
    manifest = validate_manifest(manifest)
    if manifest["system_update_required"]:
        raise MaintenanceRequiredError(
            "This release changes protected system components; run install.bat --repair"
        )
    paths = UpdatePaths(Path(install_root).resolve())
    configure_logging(paths)
    _test_write_access(paths)
    installed_manifest = _load_installed_manifest(paths)
    validate_incremental_installation(paths.install_root, installed_manifest)
    plan = calculate_update_plan(manifest, paths.install_root, installed_manifest)
    if not is_newer_version(manifest["version"], plan["current_version"]):
        raise UpdateError(
            f"ENIMAS {plan['current_version']} is already current; stable is {manifest['version']}"
        )
    _preflight_disk_space(paths, plan)
    version_stage = paths.staging / manifest["version"]
    staging_files = version_stage / "files"
    staging_files.mkdir(parents=True, exist_ok=True)

    downloadable = [op for op in plan["operations"] if op["kind"] != "remove"]
    for index, op in enumerate(downloadable, start=1):
        if _cancelled(cancel_event):
            raise CancelledError("Update preparation cancelled")
        destination = staging_files / Path(*PurePosixPath(op["destination"]).parts)
        progress("files", index - 1, len(downloadable), f"Preparing {op['destination']}")
        _download_with_resume(
            release_file_url(manifest, op["path"]),
            destination,
            op["size"],
            op["sha256"],
            progress=progress,
            cancel_event=cancel_event,
        )
    progress("files", len(downloadable), len(downloadable), "All changed files verified")
    if _cancelled(cancel_event):
        raise CancelledError("Update preparation cancelled")
    _prepare_dependency_wheelhouse(paths, plan, staging_files, progress)
    external_updater = _copy_updater_to_external_location(paths, manifest, staging_files)
    plan["external_updater"] = str(external_updater)
    plan["phase"] = "ready"
    plan_path = version_stage / "plan.json"
    atomic_write_json(plan_path, plan)
    return plan_path


def _replace_with_retries(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for _ in range(FILE_RETRY_ATTEMPTS):
        try:
            _durable_replace(source, destination)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(FILE_RETRY_DELAY)
    raise UpdateError(f"Could not replace locked file {destination}: {last_error}")


def _unlink_with_retries(path: Path) -> None:
    last_error: Exception | None = None
    for _ in range(FILE_RETRY_ATTEMPTS):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(FILE_RETRY_DELAY)
    raise UpdateError(f"Could not remove locked file {path}: {last_error}")


def _move_to_tombstone(path: Path, tombstone: Path) -> None:
    """Durably make a file invisible while retaining recoverable bytes."""
    if not path.exists() and tombstone.exists():
        return
    if not path.exists():
        return
    tombstone.unlink(missing_ok=True)
    _replace_with_retries(path, tombstone)


def _cleanup_tombstones(paths: UpdatePaths, journal: dict[str, Any]) -> None:
    for operation in journal.get("operations", []):
        tombstone_text = operation.get("tombstone")
        if not tombstone_text:
            continue
        tombstone = safe_destination(paths.install_root, tombstone_text)
        try:
            _unlink_with_retries(tombstone)
        except UpdateError:
            LOGGER.warning("Inert update tombstone could not be deleted: %s", tombstone)


def _wait_for_pid(pid: int, timeout: float = 90.0) -> None:
    if pid <= 0:
        return
    deadline = time.time() + timeout
    while pid_is_alive(pid) and time.time() < deadline:
        time.sleep(0.25)
    if pid_is_alive(pid):
        raise UpdateError("ENIMAS is still running. Close it and retry the update.")


def _terminate_process(
    pid: int, exit_code: int = 42, expected_marker: str | None = None
) -> None:
    """Terminate one already-verified process after orderly shutdown has stalled."""
    if pid <= 0 or not pid_is_alive(pid):
        return
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_terminate = 0x0001
        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_terminate | process_query_limited_information | synchronize,
            False,
            pid,
        )
        if not handle:
            if not pid_is_alive(pid):
                return
            raise UpdateError(
                f"Could not finish closing the verified ENIMAS process ({ctypes.get_last_error()})"
            )
        try:
            if expected_marker is not None:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                ):
                    raise UpdateError(
                        "Could not re-verify the ENIMAS process before closing it"
                    )
                opened_marker = str(
                    (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                )
                if opened_marker != str(expected_marker):
                    raise UpdateError(
                        "ENIMAS process identity changed before shutdown; retry the update"
                    )
            if not kernel32.TerminateProcess(handle, exit_code):
                error = ctypes.get_last_error()
                if pid_is_alive(pid):
                    raise UpdateError(
                        f"Could not finish closing the verified ENIMAS process ({error})"
                    )
            kernel32.WaitForSingleObject(handle, 10_000)
        finally:
            kernel32.CloseHandle(handle)
        return

    import signal

    if expected_marker is not None and str(process_start_marker(pid)) != str(
        expected_marker
    ):
        raise UpdateError(
            "ENIMAS process identity changed before shutdown; retry the update"
        )
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise UpdateError("Could not finish closing the verified ENIMAS process") from exc


def _wait_for_update_handoff(
    paths: UpdatePaths,
    pid: int,
    *,
    orderly_timeout: float = 15.0,
    forced_timeout: float = 10.0,
) -> None:
    """Wait for an in-app update exit, recovering only a verified stale app owner.

    This force-close path is used only by ``apply --wait-pid`` after the user
    accepted an update inside ENIMAS. Manually launched installer/bootstrap
    flows keep using ``_wait_for_pid`` and never force-close the application.
    """
    try:
        _wait_for_pid(pid, timeout=orderly_timeout)
        return
    except UpdateError as exc:
        if not pid_is_alive(pid):
            return
        payload = _read_lock_payload(paths.app_lock) or {}
        owner_pid = int(payload.get("pid", 0) or 0)
        recorded_marker = payload.get("process_marker")
        current_marker = process_start_marker(pid)
        if (
            owner_pid != pid
            or not recorded_marker
            or not current_marker
            or str(recorded_marker) != str(current_marker)
        ):
            raise UpdateError(
                "ENIMAS is still running and its process identity could not be verified. "
                "Close it manually and retry the update."
            ) from exc

        LOGGER.warning(
            "Verified ENIMAS process %s remained after orderly update shutdown; "
            "finishing that process before applying the prepared update",
            pid,
        )
        _terminate_process(pid, exit_code=42, expected_marker=str(recorded_marker))
        _wait_for_pid(pid, timeout=forced_timeout)
        current_payload = _read_lock_payload(paths.app_lock) or {}
        if (
            int(current_payload.get("pid", 0) or 0) == pid
            and str(current_payload.get("process_marker")) == str(recorded_marker)
        ):
            paths.app_lock.unlink(missing_ok=True)


def _install_staged_dependencies(paths: UpdatePaths, journal: dict[str, Any], plan_path: Path) -> None:
    if not journal.get("dependencies_changed"):
        return
    stage_root = plan_path.parent
    wheelhouse = stage_root / "wheelhouse"
    requirements = stage_root / "files" / "src" / "requirements.lock"
    venv_python = paths.install_root / "src" / "venv" / "Scripts" / "python.exe"
    journal["phase"] = "installing_dependencies"
    atomic_write_json(paths.journal, journal)
    command = [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        "--require-hashes",
        "--requirement",
        str(requirements),
    ]
    completed = subprocess.run(command, cwd=requirements.parent, check=False)
    if completed.returncode != 0:
        raise UpdateError("Dependency installation failed; application files were not changed")
    _synchronize_dependency_environment(
        venv_python,
        _wheelhouse_package_names(wheelhouse),
        requirements.parent,
    )
    journal["dependencies_installed"] = True
    atomic_write_json(paths.journal, journal)


def _restore_previous_dependencies(paths: UpdatePaths, journal: dict[str, Any]) -> None:
    if not journal.get("dependencies_changed"):
        return
    phase = journal.get("phase")
    if not journal.get("dependencies_installed") and phase != "installing_dependencies":
        return
    stage_root = Path(journal["plan_path"]).parent
    freeze = stage_root / "previous-requirements-freeze.txt"
    rollback_lock = stage_root / "previous-requirements.lock"
    wheelhouse = stage_root / "previous-wheelhouse"
    venv_python = paths.install_root / "src" / "venv" / "Scripts" / "python.exe"
    if not freeze.is_file() or not venv_python.is_file():
        raise UpdateError("Previous dependency environment is unavailable for rollback")
    old_freeze = freeze.read_text(encoding="utf-8")
    if old_freeze.strip():
        if not wheelhouse.is_dir() or not rollback_lock.is_file():
            raise UpdateError("Previous dependency wheelhouse is unavailable for rollback")
        command = [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--force-reinstall",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--require-hashes",
            "--requirement",
            str(rollback_lock),
        ]
        completed = subprocess.run(command, cwd=paths.install_root / "src", check=False)
        if completed.returncode != 0:
            raise UpdateError("Automatic dependency rollback failed; Repair is required")
    current = subprocess.run(
        [str(venv_python), "-m", "pip", "freeze"],
        cwd=paths.install_root / "src",
        capture_output=True,
        text=True,
        check=False,
    )
    if current.returncode != 0:
        raise UpdateError("Could not verify the restored dependency environment")
    old_names = _freeze_package_names(old_freeze)
    extra_names = sorted(_freeze_package_names(current.stdout) - old_names)
    if extra_names:
        removed = subprocess.run(
            [str(venv_python), "-m", "pip", "uninstall", "-y", *extra_names],
            cwd=paths.install_root / "src",
            check=False,
        )
        if removed.returncode != 0:
            raise UpdateError("Could not remove target-only dependencies during rollback")
    LOGGER.info("Previous dependency environment restored")


def _freeze_package_names(payload: str) -> set[str]:
    names: set[str] = set()
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-e ", "--")):
            continue
        if " @ " in line:
            name = line.split(" @ ", 1)[0]
        else:
            name = line.split("==", 1)[0].split("===", 1)[0]
        name = name.split("[", 1)[0].strip().lower().replace("_", "-").replace(".", "-")
        if name:
            names.add(name)
    return names


def _restore_previous_manifest(paths: UpdatePaths, journal: dict[str, Any]) -> None:
    backup_root = Path(journal["backup_root"])
    previous_manifest = backup_root / "installed_manifest.before.json"
    if previous_manifest.is_file():
        temporary = paths.installed_manifest.with_name(".installed_manifest.rollback.tmp")
        _durable_copy(previous_manifest, temporary)
        _durable_replace(temporary, paths.installed_manifest)
    else:
        paths.installed_manifest.unlink(missing_ok=True)


def _restore_external_updater(paths: UpdatePaths, journal: dict[str, Any]) -> None:
    promotion = journal.get("external_updater_promotion") or {}
    if promotion.get("status") not in {"pending", "applied"}:
        return
    stable = paths.bin / "enimas_updater.py"
    backup_text = promotion.get("backup")
    if promotion.get("previous_existed") and backup_text:
        backup = Path(backup_text)
        if not backup.is_file():
            raise UpdateError("Previous standalone updater backup is missing")
        temporary = stable.with_name(".enimas_updater.rollback.tmp")
        _durable_copy(backup, temporary)
        _durable_replace(temporary, stable)
    else:
        stable.unlink(missing_ok=True)
    stable.with_name(".enimas_updater.update.tmp").unlink(missing_ok=True)
    promotion["status"] = "rolled_back"
    journal["external_updater_promotion"] = promotion
    atomic_write_json(paths.journal, journal)


def _backup_path(backup_root: Path, destination: str) -> Path:
    relative = _safe_relative(destination, "backup destination")
    return backup_root / Path(*relative.parts)


def rollback_transaction(paths: UpdatePaths, journal: dict[str, Any]) -> None:
    backup_root = Path(journal["backup_root"])
    LOGGER.warning("Rolling back ENIMAS update transaction")
    for operation in reversed(journal.get("operations", [])):
        destination = safe_destination(paths.install_root, operation["destination"])
        backup = _backup_path(backup_root, operation["destination"])
        existed = bool(operation.get("existed"))
        status = operation.get("status", "pending")
        if status == "pending":
            continue
        if existed and backup.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            tmp_restore = destination.with_name(destination.name + ".rollback.tmp")
            _durable_copy(backup, tmp_restore)
            _replace_with_retries(tmp_restore, destination)
        elif not existed:
            tombstone_text = operation.get("tombstone") or (
                operation["destination"] + ".enimas-rollback-tombstone"
            )
            operation["tombstone"] = tombstone_text
            _move_to_tombstone(
                destination,
                safe_destination(paths.install_root, tombstone_text),
            )
        with contextlib.suppress(UpdateError):
            _unlink_with_retries(destination.with_name(destination.name + ".update.tmp"))
        operation["status"] = "rolled_back"
        atomic_write_json(paths.journal, journal)
    _restore_previous_dependencies(paths, journal)
    _restore_previous_manifest(paths, journal)
    _restore_external_updater(paths, journal)
    journal["phase"] = (
        "support_rollback_files_restored"
        if journal.get("transaction_kind") == "support_rollback"
        else "rolled_back"
    )
    atomic_write_json(paths.journal, journal)
    _cleanup_tombstones(paths, journal)


def _run_health_check(paths: UpdatePaths, expected_version: str) -> None:
    venv_python = paths.install_root / "src" / "venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        raise UpdateError("Virtual environment disappeared during update")
    health_script = paths.install_root / "src" / "Tools" / "health_check.py"
    if not health_script.is_file():
        raise UpdateError("Hardware-independent health check is missing")
    completed = subprocess.run(
        [
            str(venv_python),
            str(health_script),
            "--root",
            str(paths.install_root),
            "--expected-version",
            expected_version,
        ],
        cwd=paths.install_root / "src",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise UpdateError(f"Updated ENIMAS failed its health check (exit {completed.returncode})")


def _write_result(paths: UpdatePaths, status: str, **extra: Any) -> None:
    payload = {
        "status": status,
        "timestamp": time.time(),
        "reported": False,
        "log": str(paths.log),
        **extra,
    }
    atomic_write_json(paths.result, payload)


def _restart_enimas(paths: UpdatePaths) -> None:
    launcher = paths.install_root / "hidden_launcher.vbs"
    batch = paths.install_root / "ENIMAS.bat"
    try:
        if launcher.is_file():
            subprocess.Popen(
                ["wscript.exe", str(launcher)],
                cwd=paths.install_root,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        elif batch.is_file():
            subprocess.Popen(
                ["cmd.exe", "/c", str(batch)],
                cwd=paths.install_root,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
    except OSError:
            LOGGER.exception("Could not restart ENIMAS automatically")


def _apply_update_transaction(
    plan_path: Path | str,
    install_root: Path | str,
    *,
    wait_pid: int = 0,
    fault_after: int | None = None,
) -> None:
    plan_path = Path(plan_path).resolve()
    paths = UpdatePaths(Path(install_root).resolve())
    try:
        plan_path.relative_to(paths.staging.resolve())
    except ValueError as exc:
        raise UpdateError("Prepared update plan is outside the ENIMAS staging directory") from exc
    configure_logging(paths, verbose=True)
    with UpdateLock(paths):
        _wait_for_update_handoff(paths, wait_pid)
        if paths.journal.exists():
            existing = read_json(paths.journal, {})
            if existing.get("phase") not in {"complete", "rolled_back"}:
                rollback_transaction(paths, existing)
        plan = read_json(plan_path)
        if not isinstance(plan, dict) or plan.get("phase") != "ready":
            raise UpdateError("Prepared update plan is invalid or incomplete")
        manifest = validate_manifest(plan["manifest"])
        if manifest["system_update_required"]:
            raise MaintenanceRequiredError(
                "Prepared release requires administrator system maintenance"
            )
        if plan.get("target_version") != manifest["version"]:
            raise UpdateError("Prepared plan version does not match its manifest")
        expected_plan_path = (paths.staging / manifest["version"] / "plan.json").resolve()
        if plan_path != expected_plan_path:
            raise UpdateError("Prepared update plan is not in its versioned staging directory")
        candidate_updater = Path(str(plan.get("external_updater", ""))).resolve()
        expected_candidate = (paths.bin / manifest["version"] / "enimas_updater.py").resolve()
        if candidate_updater != expected_candidate:
            raise UpdateError("Prepared standalone updater path is invalid")
        if (
            not candidate_updater.is_file()
            or sha256_file(candidate_updater) != manifest["updater"]["sha256"]
        ):
            raise UpdateError("Prepared standalone updater is missing or corrupt")
        live_version = read_local_version(paths.install_root)
        if live_version != plan["current_version"]:
            raise UpdateError(
                f"Installed ENIMAS changed after preparation ({plan['current_version']} -> "
                f"{live_version}); check for updates again"
            )
        installed_manifest = _load_installed_manifest(paths)
        validate_incremental_installation(paths.install_root, installed_manifest)
        expected_plan = calculate_update_plan(
            manifest, paths.install_root, installed_manifest
        )
        if bool(plan.get("dependencies_changed")) != bool(
            expected_plan["dependencies_changed"]
        ):
            raise UpdateError("Installed dependencies changed after update preparation")

        def operation_signature(operation: dict[str, Any]) -> tuple[Any, ...]:
            if not isinstance(operation, dict):
                raise UpdateError("Prepared update contains an invalid operation")
            kind = operation.get("kind")
            if kind not in {"add", "replace", "remove"}:
                raise UpdateError("Prepared update contains an unsupported operation")
            destination_text = str(operation.get("destination", ""))
            destination = _safe_relative(
                destination_text, "prepared operation destination"
            ).as_posix()
            if is_preserved_destination(destination):
                raise UpdateError(
                    f"Prepared update targets preserved user data: {destination}"
                )
            if kind == "remove":
                return (kind, destination.casefold())
            return (
                kind,
                destination.casefold(),
                str(operation.get("path", "")),
                int(operation.get("size", -1)),
                str(operation.get("sha256", "")).lower(),
                bool(operation.get("locally_modified")),
            )

        prepared_signatures = sorted(
            operation_signature(operation) for operation in plan.get("operations", [])
        )
        expected_signatures = sorted(
            operation_signature(operation)
            for operation in expected_plan["operations"]
        )
        if prepared_signatures != expected_signatures:
            raise UpdateError(
                "Prepared update operations no longer match the installed application"
            )
        stage_files = plan_path.parent / "files"
        backup_root = paths.backups / f"{plan['current_version']}-before-{plan['target_version']}"
        if backup_root.exists():
            shutil.rmtree(backup_root)
        backup_root.mkdir(parents=True, exist_ok=True)
        previous_manifest_backup = backup_root / "installed_manifest.before.json"
        if paths.installed_manifest.is_file():
            _durable_copy(paths.installed_manifest, previous_manifest_backup)
        stable_updater = paths.bin / "enimas_updater.py"
        previous_updater_backup = backup_root / "enimas_updater.before.py"
        previous_updater_existed = stable_updater.is_file()
        if previous_updater_existed:
            _durable_copy(stable_updater, previous_updater_backup)
        previous_known_good = read_json(paths.latest_backup, {}) or {}
        journal = {
            **plan,
            "phase": "starting",
            "backup_root": str(backup_root),
            "plan_path": str(plan_path),
            "operations": [dict(operation) for operation in plan["operations"]],
            "started_at": time.time(),
            "external_updater_promotion": {
                "status": "not_started",
                "previous_existed": previous_updater_existed,
                "backup": str(previous_updater_backup),
            },
        }
        atomic_write_json(paths.journal, journal)
        applied_count = 0
        try:
            _install_staged_dependencies(paths, journal, plan_path)
            journal["phase"] = "applying_files"
            atomic_write_json(paths.journal, journal)
            for operation in journal["operations"]:
                destination = safe_destination(paths.install_root, operation["destination"])
                existed = destination.is_file() or destination.is_symlink()
                operation["existed"] = existed
                if existed:
                    backup = _backup_path(backup_root, operation["destination"])
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    _durable_copy(destination, backup)
                    if operation.get("locally_modified"):
                        LOGGER.warning(
                            "Locally modified managed file backed up before replacement: %s",
                            operation["destination"],
                        )
                operation["status"] = "backed_up"
                atomic_write_json(paths.journal, journal)

                if operation["kind"] == "remove":
                    tombstone_text = (
                        operation["destination"] + ".enimas-update-tombstone"
                    )
                    operation["tombstone"] = tombstone_text
                    atomic_write_json(paths.journal, journal)
                    _move_to_tombstone(
                        destination,
                        safe_destination(paths.install_root, tombstone_text),
                    )
                else:
                    staged = stage_files / Path(*PurePosixPath(operation["destination"]).parts)
                    if not staged.is_file() or sha256_file(staged) != operation["sha256"]:
                        raise UpdateError(f"Staged file is missing or invalid: {operation['destination']}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temp_target = destination.with_name(destination.name + ".update.tmp")
                    _durable_copy(staged, temp_target)
                    _replace_with_retries(temp_target, destination)
                    if sha256_file(destination) != operation["sha256"]:
                        raise UpdateError(f"Installed file verification failed: {operation['destination']}")
                applied_count += 1
                operation["status"] = "applied"
                atomic_write_json(paths.journal, journal)
                if fault_after is not None and applied_count >= fault_after:
                    raise RuntimeError("Injected update interruption")

            journal["phase"] = "health_check"
            atomic_write_json(paths.journal, journal)
            _run_health_check(paths, plan["target_version"])
            journal["phase"] = "promoting_updater"
            journal["external_updater_promotion"]["status"] = "pending"
            atomic_write_json(paths.journal, journal)
            updater_temporary = stable_updater.with_name(".enimas_updater.update.tmp")
            _durable_copy(candidate_updater, updater_temporary)
            _durable_replace(updater_temporary, stable_updater)
            if sha256_file(stable_updater) != manifest["updater"]["sha256"]:
                raise UpdateError("Standalone updater promotion verification failed")
            journal["external_updater_promotion"]["status"] = "applied"
            atomic_write_json(paths.journal, journal)
            journal["phase"] = "committing_manifest"
            atomic_write_json(paths.journal, journal)
            atomic_write_json(paths.installed_manifest, plan["manifest"])
            journal["phase"] = "complete"
            journal["completed_at"] = time.time()
            atomic_write_json(paths.journal, journal)
            _cleanup_tombstones(paths, journal)
            rollback_snapshot = dict(journal)
            if plan.get("dependencies_changed"):
                dependency_backup = backup_root / "dependencies"
                dependency_backup.mkdir(parents=True, exist_ok=True)
                for name in (
                    "previous-requirements-freeze.txt",
                    "previous-requirements.lock",
                    "previous-wheelhouse",
                ):
                    source = plan_path.parent / name
                    target = dependency_backup / name
                    if source.is_dir():
                        shutil.copytree(source, target, dirs_exist_ok=True)
                    elif source.is_file():
                        shutil.copy2(source, target)
                rollback_snapshot["plan_path"] = str(dependency_backup / "plan.json")
            snapshot_path = backup_root / "rollback_journal.json"
            atomic_write_json(snapshot_path, rollback_snapshot)
            atomic_write_json(
                paths.latest_backup,
                {
                    "backup_root": str(backup_root),
                    "journal": str(snapshot_path),
                    "previous_version": plan["current_version"],
                    "updated_version": plan["target_version"],
                },
            )
            _write_result(
                paths,
                "success",
                previous_version=plan["current_version"],
                version=plan["target_version"],
            )
            previous_backup_text = previous_known_good.get("backup_root")
            if previous_backup_text:
                previous_backup = Path(str(previous_backup_text)).resolve()
                try:
                    previous_backup.relative_to(paths.backups.resolve())
                except ValueError:
                    LOGGER.warning("Ignored unsafe previous backup pointer: %s", previous_backup)
                else:
                    if previous_backup != backup_root.resolve():
                        shutil.rmtree(previous_backup, ignore_errors=True)
            shutil.rmtree(plan_path.parent, ignore_errors=True)
        except Exception as exc:
            LOGGER.exception("ENIMAS update failed")
            try:
                rollback_transaction(paths, journal)
            except Exception as rollback_exc:
                LOGGER.exception("Automatic rollback is incomplete")
                _write_result(
                    paths,
                    "recovery_required",
                    previous_version=plan["current_version"],
                    target_version=plan["target_version"],
                    error=f"{exc}; rollback failed: {rollback_exc}",
                )
                raise UpdateError(
                    f"Update failed and rollback is incomplete: {rollback_exc}"
                ) from exc
            else:
                _write_result(
                    paths,
                    "rolled_back",
                    previous_version=plan["current_version"],
                    target_version=plan["target_version"],
                    error=str(exc),
                )
            raise


def apply_update(
    plan_path: Path | str,
    install_root: Path | str,
    *,
    wait_pid: int = 0,
    restart: bool = False,
    fault_after: int | None = None,
) -> None:
    """Apply a prepared transaction and always relaunch after a UI handoff."""
    paths = UpdatePaths(Path(install_root).resolve())
    restart_safe = True
    try:
        _apply_update_transaction(
            plan_path,
            paths.install_root,
            wait_pid=wait_pid,
            fault_after=fault_after,
        )
    except Exception:
        try:
            journal = read_json(paths.journal, {}) or {}
            restart_safe = not journal or journal.get("phase") in {"complete", "rolled_back"}
        except UpdateError:
            restart_safe = False
        raise
    finally:
        if restart and restart_safe:
            _restart_enimas(paths)


def recover_interrupted_update(install_root: Path | str) -> bool:
    paths = UpdatePaths(Path(install_root).resolve())
    configure_logging(paths)
    if paths.lock.exists():
        lock_data = _read_lock_payload(paths.lock) or {}
        if lock_owner_is_alive(lock_data):
            raise UpdateActiveError("An ENIMAS update is currently running")
    journal = read_json(paths.journal)
    if not journal or journal.get("phase") in {
        "complete",
        "rolled_back",
        "support_rollback_complete",
    }:
        return False
    with UpdateLock(paths):
        if journal.get("transaction_kind") == "support_rollback":
            _finish_support_rollback(paths, journal)
        else:
            rollback_transaction(paths, journal)
            _write_result(
                paths,
                "recovered",
                previous_version=journal.get("current_version"),
                target_version=journal.get("target_version"),
            )
    return True


def ensure_installation_is_ready(install_root: Path | str) -> None:
    paths = UpdatePaths(Path(install_root).resolve())
    state = read_json(paths.install_journal, {}) or {}
    if state and state.get("phase") not in {"complete", "rolled_back"}:
        raise UpdateActiveError(
            "A fresh installation or repair is still in progress; run install.bat to continue it"
        )


def _finish_support_rollback(paths: UpdatePaths, journal: dict[str, Any]) -> None:
    rollback_transaction(paths, journal)
    previous_version = str(journal["support_previous_version"])
    updated_version = str(journal["support_updated_version"])
    _run_health_check(paths, previous_version)
    _write_result(
        paths,
        "support_rollback",
        version=previous_version,
        previous_version=updated_version,
    )
    journal["phase"] = "support_rollback_complete"
    journal["completed_at"] = time.time()
    atomic_write_json(paths.journal, journal)
    paths.latest_backup.unlink(missing_ok=True)


def rollback_latest_update(install_root: Path | str) -> None:
    """Support command: restore the latest known-good application transaction."""
    paths = UpdatePaths(Path(install_root).resolve())
    pointer = read_json(paths.latest_backup, {}) or {}
    if not pointer:
        raise UpdateError("No known-good update backup is available")
    backup_root = Path(pointer.get("backup_root", "")).resolve()
    try:
        backup_root.relative_to(paths.backups.resolve())
    except ValueError as exc:
        raise UpdateError("Known-good backup metadata is unsafe") from exc
    snapshot_path = Path(pointer.get("journal", "")).resolve()
    try:
        snapshot_path.relative_to(backup_root)
    except ValueError as exc:
        raise UpdateError("Known-good rollback journal is unsafe") from exc
    snapshot = read_json(snapshot_path, {}) or {}
    if snapshot.get("phase") != "complete" or Path(snapshot.get("backup_root", "")).resolve() != backup_root:
        raise UpdateError("Known-good rollback journal is invalid")
    app_data = _read_lock_payload(paths.app_lock) or {} if paths.app_lock.exists() else {}
    if lock_owner_is_alive(app_data):
        raise UpdateError("Close ENIMAS before running support rollback")
    with UpdateLock(paths):
        support_journal = json.loads(json.dumps(snapshot))
        support_journal["transaction_kind"] = "support_rollback"
        support_journal["phase"] = "support_rollback"
        support_journal["support_previous_version"] = pointer["previous_version"]
        support_journal["support_updated_version"] = pointer["updated_version"]
        atomic_write_json(paths.journal, support_journal)
        _finish_support_rollback(paths, support_journal)


def bootstrap_update(
    manifest_url: str,
    install_root: Path | str,
    *,
    restart: bool = True,
) -> int:
    paths = UpdatePaths(Path(install_root).resolve())
    configure_logging(paths, verbose=True)
    recover_interrupted_update(paths.install_root)
    manifest = fetch_manifest(manifest_url)
    current = validate_incremental_installation(paths.install_root)
    if not is_newer_version(manifest["version"], current):
        print(f"ENIMAS {current} is already current (stable: {manifest['version']}).")
        return EXIT_NOT_AVAILABLE

    def console_progress(phase: str, current_value: int, total: int, message: str) -> None:
        if total:
            percent = int(current_value * 100 / total)
            print(f"[{phase}] {percent:3d}% {message}")
        else:
            print(f"[{phase}] {message}")

    plan_path = prepare_update(manifest, paths.install_root, progress=console_progress)
    app_data = _read_lock_payload(paths.app_lock) or {} if paths.app_lock.exists() else {}
    app_pid = int(app_data.get("pid", 0) or 0)
    if lock_owner_is_alive(app_data):
        print("ENIMAS is running. Close it to continue the update.")
        _wait_for_pid(app_pid, timeout=300)
    apply_update(plan_path, paths.install_root, restart=restart)
    return EXIT_OK


def release_archive_url(manifest: dict[str, Any]) -> str:
    """Return the immutable GitLab source archive URL for a validated manifest."""
    validate_manifest(manifest)
    raw_url = manifest["repository_raw_url"]
    marker = "/-/raw/"
    if marker not in raw_url:
        raise ManifestError("repository_raw_url is not a supported GitLab raw URL")
    project_url = raw_url.split(marker, 1)[0].rstrip("/")
    reference = urllib.parse.quote(str(manifest["source_ref"]), safe="")
    project_name = urllib.parse.urlparse(project_url).path.rstrip("/").rsplit("/", 1)[-1]
    return f"{project_url}/-/archive/{reference}/{project_name}-{reference}.zip"


def _download_archive_with_resume(
    url: str,
    archive: Path,
    *,
    progress: ProgressCallback = _noop_progress,
    attempts: int = DOWNLOAD_ATTEMPTS,
) -> None:
    """Download a tag-pinned archive, retaining a partial file across reruns."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.is_file():
        try:
            with zipfile.ZipFile(archive) as bundle:
                if bundle.testzip() is None:
                    return
        except (OSError, zipfile.BadZipFile):
            pass
        archive.unlink(missing_ok=True)
    part = archive.with_name(archive.name + ".part")
    if part.is_file():
        try:
            valid_part = False
            with zipfile.ZipFile(part) as bundle:
                valid_part = bundle.testzip() is None
            if valid_part:
                _durable_replace(part, archive)
                return
        except (OSError, zipfile.BadZipFile):
            pass
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        offset = part.stat().st_size if part.is_file() else 0
        headers = {"User-Agent": "ENIMAS-Installer/1", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            request = urllib.request.Request(url, headers=headers)
            with _open_url(request, 60.0) as response:
                status = getattr(response, "status", response.getcode())
                if offset and status != 206:
                    part.unlink(missing_ok=True)
                    raise UpdateError("Archive server did not honor resume; restarting download")
                remaining = int(response.headers.get("Content-Length", "0") or 0)
                total = offset + remaining if remaining else 0
                downloaded = offset
                with part.open("ab" if offset else "wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        downloaded += len(chunk)
                        progress("archive", downloaded, total, "Downloading release archive")
                    handle.flush()
                    os.fsync(handle.fileno())
            _durable_replace(part, archive)
            with zipfile.ZipFile(archive) as bundle:
                invalid = bundle.testzip()
                if invalid:
                    raise UpdateError(f"Release archive is corrupt at {invalid}")
            return
        except Exception as exc:
            last_error = exc
            if archive.exists():
                archive.unlink(missing_ok=True)
            LOGGER.warning("Archive download attempt %s/%s failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(min(4.0, 0.5 * (2 ** (attempt - 1))))
    raise UpdateError(f"Could not download the release archive: {last_error}")


def _safe_extract_archive(archive: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        expanded_size = sum(info.file_size for info in members if not info.is_dir())
        free = shutil.disk_usage(destination).free
        if expanded_size > MAX_RELEASE_EXTRACTED_BYTES or expanded_size + DISK_SAFETY_BYTES > free:
            raise UpdateError("Release archive is too large to extract safely")
        for info in members:
            relative = _safe_relative(info.filename.rstrip("/"), "archive member")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise UpdateError(f"Release archive contains a symbolic link: {info.filename}")
            target = destination / Path(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise UpdateError("Release archive must contain exactly one project directory")
    return roots[0]


def _validate_extracted_release(source_root: Path, manifest: dict[str, Any]) -> None:
    expected = {entry["path"].casefold(): entry for entry in manifest["archive_files"]}
    embedded_manifest = source_root / "update_manifest.json"
    embedded_manifest.unlink(missing_ok=True)
    actual: set[str] = set()
    for source in source_root.rglob("*"):
        if source.is_file():
            actual.add(source.relative_to(source_root).as_posix().casefold())
    unexpected = sorted(actual - expected.keys())
    missing = sorted(expected.keys() - actual)
    if unexpected:
        raise UpdateError("Release archive contains unexpected files: " + ", ".join(unexpected))
    if missing:
        raise UpdateError("Release archive is missing files: " + ", ".join(missing))
    for entry in manifest["archive_files"]:
        source_text = entry["path"]
        relative = _safe_relative(source_text, "release source")
        source = source_root / Path(*relative.parts)
        if source.stat().st_size != entry["size"] or sha256_file(source) != entry["sha256"]:
            raise UpdateError(f"Release archive validation failed for {source_text}")


def _copy_verified_release_file(
    source_root: Path,
    relative_text: str,
    destination: Path,
    metadata: dict[str, Any],
) -> None:
    source = source_root / Path(*_safe_relative(relative_text, "privileged payload").parts)
    temporary = destination.with_name(destination.name + ".verified.tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, temporary)
    if temporary.stat().st_size != metadata["size"] or sha256_file(temporary) != metadata["sha256"]:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"Privileged payload changed during staging: {relative_text}")
    _durable_replace(temporary, destination)


def _is_reparse_point(path: Path) -> bool:
    """Return whether an existing Windows path redirects filesystem access."""
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _validated_privileged_control_root(control_root: Path | str) -> Path:
    """Validate the fixed, administrator-protected control-plane location."""
    candidate = Path(control_root)
    if os.name == "nt":
        expected = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "ENIMAS" / "admin"
        if os.path.normcase(os.path.abspath(candidate)) != os.path.normcase(
            os.path.abspath(expected)
        ):
            raise UpdateError("Privileged control directory is not the fixed ProgramData path")
        for protected_path in (expected.parent, expected):
            if protected_path.exists() and _is_reparse_point(protected_path):
                raise UpdateError(
                    f"Privileged control directory contains a reparse point: {protected_path}"
                )
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise UpdateError("Privileged control directory has not been created securely")
    return resolved


def _stage_privileged_control_plane(
    source_root: Path,
    manifest: dict[str, Any],
    control_root: Path,
) -> None:
    """Stage an immutable installer/updater pair without changing the active pointer."""
    control_root = _validated_privileged_control_root(control_root)
    archive_by_path = {entry["path"]: entry for entry in manifest["archive_files"]}
    version_name = str(manifest["source_ref"])
    version_root = control_root / "versions" / version_name
    current_version_path = control_root / "current.version"
    active_version: str | None
    try:
        current_version_text = current_version_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if current_version_path.is_symlink() or _is_reparse_point(current_version_path):
            raise UpdateError("Current protected installer version is unreadable") from exc
        active_version = None
    except (OSError, UnicodeError) as exc:
        raise UpdateError("Current protected installer version is unreadable") from exc
    else:
        active_version = current_version_text.strip()
        try:
            if not active_version.startswith("v"):
                raise ManifestError("current.version must name an immutable release tag")
            parse_version(active_version[1:])
        except ManifestError as exc:
            raise UpdateError("Current protected installer version is invalid") from exc
        if current_version_text not in {
            active_version,
            active_version + "\n",
            active_version + "\r\n",
        }:
            raise UpdateError("Current protected installer version is invalid")

    version_root_exists = (
        version_root.exists() or version_root.is_symlink() or _is_reparse_point(version_root)
    )
    if active_version == version_name:
        if (
            not version_root.is_dir()
            or version_root.is_symlink()
            or _is_reparse_point(version_root)
        ):
            raise UpdateError("Active protected installer version directory is invalid")
        stored_manifest = validate_manifest(
            read_json(version_root / "maintenance_manifest.json", {}) or {}
        )
        if stored_manifest["source_ref"] != version_name:
            raise UpdateError("Protected installer manifest does not match its version directory")
        _validate_control_version(version_root, stored_manifest)

        stored_archive = {entry["path"]: entry for entry in stored_manifest["archive_files"]}
        control_paths = (
            (stored_manifest["updater"]["path"], manifest["updater"]["path"]),
            ("install.bat", "install.bat"),
            (
                "Tools/protected_install_dispatcher.bat",
                "Tools/protected_install_dispatcher.bat",
            ),
        )
        for stored_path, requested_path in control_paths:
            if (
                stored_path != requested_path
                or stored_archive.get(stored_path) != archive_by_path.get(requested_path)
            ):
                raise UpdateError(
                    f"Protected control-plane metadata changed for release {version_name}: "
                    f"{requested_path}"
                )

        requested_system = {entry["path"]: entry for entry in manifest["system_files"]}
        for stored_entry in stored_manifest["system_files"]:
            if archive_by_path.get(stored_entry["path"]) != stored_entry:
                raise UpdateError(
                    "Protected system payload metadata changed for release "
                    f"{version_name}: {stored_entry['path']}"
                )
        if manifest["system_files"]:
            for stored_entry in stored_manifest["system_files"]:
                if requested_system.get(stored_entry["path"]) != stored_entry:
                    raise UpdateError(
                        "Protected system payload metadata changed for release "
                        f"{version_name}: {stored_entry['path']}"
                    )
        else:
            _validate_control_version(version_root, manifest)
            atomic_write_text(control_root / "pending.version", version_name + "\n")
            return

        stored_system = {entry["path"]: entry for entry in stored_manifest["system_files"]}
        if all(stored_system.get(path) == entry for path, entry in requested_system.items()):
            _validate_control_version(version_root, manifest)
            atomic_write_text(control_root / "pending.version", version_name + "\n")
            return

        payload_root = version_root / "payloads"
        for entry in manifest["system_files"]:
            _copy_verified_release_file(
                source_root,
                entry["path"],
                payload_root / Path(*PurePosixPath(entry["path"]).parts),
                entry,
            )
        atomic_write_json(version_root / "maintenance_manifest.json", manifest)
        _validate_control_version(version_root, manifest)
        atomic_write_text(control_root / "pending.version", version_name + "\n")
        return
    if version_root_exists:
        if (
            not version_root.is_dir()
            or version_root.is_symlink()
            or _is_reparse_point(version_root)
        ):
            raise UpdateError("Inactive protected installer version directory is invalid")
        shutil.rmtree(version_root)
    version_root.mkdir(parents=True)
    _copy_verified_release_file(
        source_root,
        manifest["updater"]["path"],
        version_root / "enimas_updater.py",
        archive_by_path[manifest["updater"]["path"]],
    )
    _copy_verified_release_file(
        source_root,
        "install.bat",
        version_root / "install.bat",
        archive_by_path["install.bat"],
    )
    _copy_verified_release_file(
        source_root,
        "Tools/protected_install_dispatcher.bat",
        version_root / "protected_install_dispatcher.bat",
        archive_by_path["Tools/protected_install_dispatcher.bat"],
    )
    payload_root = version_root / "payloads"
    if payload_root.exists():
        shutil.rmtree(payload_root)
    for entry in manifest["system_files"]:
        _copy_verified_release_file(
            source_root,
            entry["path"],
            payload_root / Path(*PurePosixPath(entry["path"]).parts),
            entry,
        )
    atomic_write_json(version_root / "maintenance_manifest.json", manifest)
    atomic_write_text(control_root / "pending.version", version_name + "\n")


def _validate_control_version(version_root: Path, manifest: dict[str, Any]) -> None:
    archive_by_path = {entry["path"]: entry for entry in manifest["archive_files"]}
    checks = [
        (version_root / "enimas_updater.py", manifest["updater"]["path"]),
        (version_root / "install.bat", "install.bat"),
        (
            version_root / "protected_install_dispatcher.bat",
            "Tools/protected_install_dispatcher.bat",
        ),
    ]
    checks.extend(
        (
            version_root / "payloads" / Path(*PurePosixPath(entry["path"]).parts),
            entry["path"],
        )
        for entry in manifest["system_files"]
    )
    for local, source_path in checks:
        metadata = archive_by_path[source_path]
        if (
            not local.is_file()
            or local.stat().st_size != metadata["size"]
            or sha256_file(local) != metadata["sha256"]
        ):
            raise UpdateError(f"Protected control-plane candidate is invalid: {source_path}")


def prepare_system_maintenance(
    manifest_url: str,
    control_root: Path | str,
    *,
    fresh: bool = False,
    control_only: bool = False,
    progress: ProgressCallback = _noop_progress,
) -> str:
    """Elevated broker path: authenticate/stage only protected system payloads."""
    control_root = _validated_privileged_control_root(control_root)
    manifest = fetch_manifest(manifest_url)
    if not fresh and not control_only and not manifest["system_update_required"]:
        raise UpdateError("Stable release does not require protected system maintenance")
    stage = control_root / "staging" / manifest["source_ref"]
    archive = stage / "release.zip"
    extracted = stage / "extracted"
    state_path = control_root / "system_maintenance_state.json"
    state = {
        "schema_version": SCHEMA_VERSION,
        "phase": "downloading",
        "source_ref": manifest["source_ref"],
        "fresh": fresh,
        "control_only": control_only,
        "started_at": time.time(),
    }
    atomic_write_json(state_path, state)
    if control_only:
        source_root = stage / "control-source"
        archive_by_path = {entry["path"]: entry for entry in manifest["archive_files"]}
        control_manifest = json.loads(json.dumps(manifest))
        control_manifest["system_files"] = []
        control_manifest["system_update_required"] = False
        required_paths = {
            manifest["updater"]["path"],
            "install.bat",
            "Tools/protected_install_dispatcher.bat",
        }
        for index, relative_text in enumerate(sorted(required_paths), start=1):
            metadata = archive_by_path.get(relative_text)
            if metadata is None:
                raise ManifestError(
                    f"Protected control file is absent from archive metadata: {relative_text}"
                )
            destination = source_root / Path(*PurePosixPath(relative_text).parts)
            _download_with_resume(
                release_file_url(manifest, relative_text),
                destination,
                metadata["size"],
                metadata["sha256"],
                progress=progress,
            )
            progress("control", index, len(required_paths), f"Verified {relative_text}")
        manifest = validate_manifest(control_manifest)
    else:
        _download_archive_with_resume(release_archive_url(manifest), archive, progress=progress)
        state["phase"] = "validating"
        atomic_write_json(state_path, state)
        source_root = _safe_extract_archive(archive, extracted)
        _validate_extracted_release(source_root, manifest)
    _stage_privileged_control_plane(source_root, manifest, control_root)
    state["phase"] = "prepared"
    state["version_root"] = str(control_root / "versions" / manifest["source_ref"])
    atomic_write_json(state_path, state)
    return manifest["source_ref"]


def activate_control_plane(control_root: Path | str) -> None:
    """Atomically select a fully verified versioned protected installer pair."""
    control_root = _validated_privileged_control_root(control_root)
    pending = (control_root / "pending.version").read_text(encoding="utf-8").strip()
    previous_active = ""
    with contextlib.suppress(OSError, UnicodeError):
        previous_active = (control_root / "current.version").read_text(encoding="utf-8").strip()
    if not pending or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in pending):
        raise UpdateError("Pending protected installer version is invalid")
    version_root = (control_root / "versions" / pending).resolve()
    try:
        version_root.relative_to((control_root / "versions").resolve())
    except ValueError as exc:
        raise UpdateError("Pending protected installer path is unsafe") from exc
    manifest = validate_manifest(read_json(version_root / "maintenance_manifest.json", {}) or {})
    if manifest["source_ref"] != pending:
        raise UpdateError("Protected installer manifest does not match its version directory")
    _validate_control_version(version_root, manifest)
    dispatcher = control_root / "install.bat"
    dispatcher_tmp = control_root / "install.dispatcher.verified.tmp"
    _durable_copy(version_root / "protected_install_dispatcher.bat", dispatcher_tmp)
    _durable_replace(dispatcher_tmp, dispatcher)
    atomic_write_text(control_root / "current.version", pending + "\n")
    state_path = control_root / "system_maintenance_state.json"
    state = read_json(state_path, {}) or {}
    state["phase"] = "active"
    state["activated_at"] = time.time()
    atomic_write_json(state_path, state)
    shutil.rmtree(control_root / "staging" / pending, ignore_errors=True)
    (control_root / "pending.version").unlink(missing_ok=True)
    versions_root = control_root / "versions"
    retained = {pending, previous_active}
    for candidate in versions_root.iterdir() if versions_root.is_dir() else ():
        if candidate.is_dir() and candidate.name not in retained:
            shutil.rmtree(candidate, ignore_errors=True)


def prepare_fresh_or_repair_install(
    manifest_url: str,
    install_root: Path | str,
    *,
    repair: bool = False,
    progress: ProgressCallback = _noop_progress,
    control_root: Path | str | None = None,
) -> None:
    """Stage and activate a tag-pinned source tree; system setup happens afterward."""
    paths = UpdatePaths(Path(install_root).resolve())
    configure_logging(paths, verbose=True)
    with UpdateLock(paths):
        if paths.app_lock.exists():
            try:
                app_owner = _read_lock_payload(paths.app_lock) or {}
            except UpdateActiveError as exc:
                raise UpdateActiveError(
                    "ENIMAS may be running; close it before fresh installation or repair"
                ) from exc
            if lock_owner_is_alive(app_owner):
                raise UpdateActiveError(
                    "ENIMAS is running; close it before fresh installation or repair"
                )
            paths.app_lock.unlink(missing_ok=True)
        state = read_json(paths.install_journal, {}) or {}
        if state.get("phase") == "source_activated":
            owner = state.get("installer_owner", {})
            if lock_owner_is_alive(owner) and int(owner.get("pid", 0) or 0) != os.getppid():
                raise UpdateActiveError("Another ENIMAS installer is completing system setup")
            state["installer_owner"] = {
                "pid": os.getppid(),
                "process_marker": process_start_marker(os.getppid()),
            }
            state["lease_reclaimed_at"] = time.time()
            atomic_write_json(paths.install_journal, state)
            return
        if state.get("phase") in {"activating", "old_backed_up"}:
            _rollback_install_state(paths, state, "Recovered interrupted source activation")
            state = {}
        legacy_recovery = _legacy_root_recovery_paths(paths.install_root)
        if legacy_recovery and not repair:
            raise RepairRequiredError(
                "Recoverable data from an interrupted legacy update was found. "
                "Run install.bat --repair so it is restored safely."
            )
        manifest = fetch_manifest(manifest_url)
        _preflight_install_disk_space(paths, manifest)
        version_stage = paths.staging / f"install-{manifest['version']}"
        archive = version_stage / "release.zip"
        extracted = version_stage / "extracted"
        state = {
            "schema_version": 1,
            "phase": "downloading",
            "target_version": manifest["version"],
            "repair": repair,
            "manifest": manifest,
            "version_stage": str(version_stage),
            "installer_owner": {
                "pid": os.getppid(),
                "process_marker": process_start_marker(os.getppid()),
            },
            "started_at": time.time(),
        }
        atomic_write_json(paths.install_journal, state)
        _download_archive_with_resume(release_archive_url(manifest), archive, progress=progress)
        progress("extracting", 0, 1, "Validating release archive")
        source_root = _safe_extract_archive(archive, extracted)
        _validate_extracted_release(source_root, manifest)
        if control_root is not None:
            _stage_privileged_control_plane(source_root, manifest, Path(control_root))
        state["source_root"] = str(source_root)
        state["phase"] = "staged"
        atomic_write_json(paths.install_journal, state)

        user_backup = version_stage / "preserved-user-data"
        if (paths.install_root / "src").exists() or legacy_recovery:
            previous_manifest = _load_installed_manifest(paths)
            preservation_manifest = _merge_managed_manifests(previous_manifest, manifest)
            backup_user_data(
                paths.install_root,
                user_backup,
                managed_manifest=preservation_manifest,
            )
            state["user_backup"] = str(user_backup)
        backup_root = paths.backups / f"install-before-{manifest['version']}"
        backup_src = backup_root / "src"
        backup_root.mkdir(parents=True, exist_ok=True)
        state["backup_root"] = str(backup_root)
        state["had_old_src"] = (paths.install_root / "src").is_dir()
        state["phase"] = "activating"
        atomic_write_json(paths.install_journal, state)
        if state["had_old_src"]:
            if backup_src.exists():
                shutil.rmtree(backup_src)
            _durable_replace(paths.install_root / "src", backup_src)
        state["phase"] = "old_backed_up"
        atomic_write_json(paths.install_journal, state)
        _durable_replace(source_root, paths.install_root / "src")
        if state.get("user_backup"):
            restore_user_data(paths.install_root, Path(state["user_backup"]))
        state["phase"] = "source_activated"
        atomic_write_json(paths.install_journal, state)
        progress("extracting", 1, 1, "Release source activated")


def _rollback_install_state(paths: UpdatePaths, state: dict[str, Any], error: str) -> None:
    current_src = paths.install_root / "src"
    backup_src = Path(state.get("backup_root", "")) / "src"
    activated = state.get("phase") == "source_activated" or (
        state.get("phase") == "old_backed_up"
        and current_src.exists()
        and not Path(state.get("source_root", "")).exists()
    )
    if activated and current_src.exists():
        failed = Path(state["version_stage"]) / "failed-source"
        if failed.exists():
            shutil.rmtree(failed)
        _durable_replace(current_src, failed)
    if state.get("had_old_src") and backup_src.is_dir() and not current_src.exists():
        _durable_replace(backup_src, current_src)
    if state.get("user_backup") and current_src.is_dir():
        restore_user_data(paths.install_root, Path(state["user_backup"]))
    state["phase"] = "rolled_back"
    state["error"] = error
    atomic_write_json(paths.install_journal, state)
    _write_result(paths, "install_rolled_back", error=error)


def abort_fresh_or_repair_install(install_root: Path | str, error: str = "") -> bool:
    paths = UpdatePaths(Path(install_root).resolve())
    state = read_json(paths.install_journal, {}) or {}
    if not state or state.get("phase") in {"complete", "rolled_back"}:
        return False
    owner = state.get("installer_owner", {})
    if lock_owner_is_alive(owner) and int(owner.get("pid", 0) or 0) != os.getppid():
        raise UpdateActiveError("Another ENIMAS installer owns this installation")
    with UpdateLock(paths):
        _rollback_install_state(paths, state, error)
    return True


def complete_fresh_or_repair_install(install_root: Path | str) -> None:
    paths = UpdatePaths(Path(install_root).resolve())
    with UpdateLock(paths):
        state = read_json(paths.install_journal, {}) or {}
        if state.get("phase") != "source_activated":
            raise UpdateError("Fresh/repair installation is not ready to complete")
        owner = state.get("installer_owner", {})
        if lock_owner_is_alive(owner) and int(owner.get("pid", 0) or 0) != os.getppid():
            raise UpdateActiveError("Another ENIMAS installer owns this installation")
        manifest = validate_manifest(state["manifest"])
        _run_health_check(paths, manifest["version"])
        if paths.journal.is_file():
            quarantine = Path(state["backup_root"]) / "pre-repair-transaction.json"
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            _durable_copy(paths.journal, quarantine)
            paths.journal.unlink(missing_ok=True)
        atomic_write_json(paths.installed_manifest, manifest)
        state["phase"] = "complete"
        state["completed_at"] = time.time()
        atomic_write_json(paths.install_journal, state)
        _write_result(
            paths,
            "success",
            version=manifest["version"],
            fresh_install=not state.get("repair"),
        )


def cleanup_completed_install(install_root: Path | str) -> bool:
    """Idempotently remove large fresh/repair staging only after durable success."""
    paths = UpdatePaths(Path(install_root).resolve())
    with UpdateLock(paths):
        state = read_json(paths.install_journal, {}) or {}
        if state.get("phase") != "complete":
            return False
        for key, allowed_root in (
            ("version_stage", paths.staging),
            ("backup_root", paths.backups),
        ):
            target_text = state.get(key)
            if not target_text:
                continue
            target = Path(str(target_text)).resolve()
            try:
                target.relative_to(allowed_root.resolve())
            except ValueError:
                raise UpdateError(f"Refused unsafe completed-install cleanup path: {target}")
            shutil.rmtree(target, ignore_errors=True)
        # A full repair establishes a new baseline; an older incremental
        # support rollback is no longer compatible with it.
        previous_pointer = read_json(paths.latest_backup, {}) or {}
        previous_backup_text = previous_pointer.get("backup_root")
        if previous_backup_text:
            previous_backup = Path(str(previous_backup_text)).resolve()
            try:
                previous_backup.relative_to(paths.backups.resolve())
            except ValueError:
                LOGGER.warning("Ignored unsafe known-good backup pointer during cleanup")
            else:
                shutil.rmtree(previous_backup, ignore_errors=True)
            paths.latest_backup.unlink(missing_ok=True)
        state["cleanup_completed_at"] = time.time()
        atomic_write_json(paths.install_journal, state)
        return True


def validate_fresh_or_repair_install(install_root: Path | str) -> None:
    paths = UpdatePaths(Path(install_root).resolve())
    with UpdateLock(paths):
        state = read_json(paths.install_journal, {}) or {}
        if state.get("phase") != "source_activated":
            raise UpdateError("Fresh/repair installation is not ready to validate")
        owner = state.get("installer_owner", {})
        if lock_owner_is_alive(owner) and int(owner.get("pid", 0) or 0) != os.getppid():
            raise UpdateActiveError("Another ENIMAS installer owns this installation")
        manifest = validate_manifest(state["manifest"])
        _run_health_check(paths, manifest["version"])


USER_DATA_PATHS = (
    "src/config.txt",
    "src/lenses.json",
    "src/plugins/measurement/plugin_m_config.json",
    "src/venv",
)
BUILTIN_PLUGIN_DIRS = {"Uniform_Background", "classification", "cropping", "measurement"}


def _copy_preserved_path(source: Path, target: Path) -> None:
    if source.is_symlink() or _is_reparse_point(source):
        raise UpdateError(f"Refused linked user-data path: {source}")
    if source.is_dir():
        for _file in _walk_unlinked_files(source):
            pass
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)


def _merge_managed_manifests(
    previous: dict[str, Any] | None,
    target: dict[str, Any],
) -> dict[str, Any]:
    """Classify both old and new shipped files as managed during repair.

    This prevents a built-in plugin deleted by the release from being mistaken
    for a custom plugin and copied back over the new source tree.
    """
    by_destination: dict[str, dict[str, Any]] = {}
    removals: dict[str, str] = {}
    for candidate in (previous or {}, target):
        for entry in candidate.get("files", []):
            destination = str(entry.get("destination", ""))
            if destination:
                by_destination[destination.casefold()] = entry
        for destination_value in candidate.get("removed", []):
            destination = str(destination_value)
            if destination:
                removals[destination.casefold()] = destination
    return {
        "files": list(by_destination.values()),
        "removed": list(removals.values()),
    }


def backup_user_data(
    install_root: Path | str,
    backup_root: Path | str,
    *,
    managed_manifest: dict[str, Any] | None = None,
) -> Path:
    install_root = Path(install_root).resolve()
    backup_root = Path(backup_root).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    manifest: list[str] = []
    for relative_text in USER_DATA_PATHS:
        source = safe_destination(install_root, relative_text)
        if not source.exists():
            continue
        target = backup_root / Path(*PurePosixPath(relative_text).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_preserved_path(source, target)
        manifest.append(relative_text)
    for source, relative_text in _legacy_root_recovery_paths(install_root):
        if relative_text in manifest:
            continue
        target = backup_root / Path(*PurePosixPath(relative_text).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_preserved_path(source, target)
        manifest.append(relative_text)
    if managed_manifest is None:
        managed_manifest = _load_installed_manifest(UpdatePaths(install_root))
    managed_plugin_files = {
        str(entry["destination"]).casefold()
        for entry in (managed_manifest or {}).get("files", [])
        if str(entry.get("destination", "")).startswith("src/plugins/")
    }
    managed_plugin_files.update(
        str(destination).casefold()
        for destination in (managed_manifest or {}).get("removed", [])
        if str(destination).startswith("src/plugins/")
    )
    plugins = install_root / "src" / "plugins"
    if plugins.is_dir():
        for source in _walk_unlinked_files(plugins):
            relative_text = source.relative_to(install_root).as_posix()
            plugin_name = source.relative_to(plugins).parts[0]
            should_preserve = relative_text.casefold() not in managed_plugin_files
            if managed_manifest is None and plugin_name in BUILTIN_PLUGIN_DIRS:
                should_preserve = False
            if should_preserve and relative_text not in manifest:
                target = backup_root / Path(*PurePosixPath(relative_text).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                manifest.append(relative_text)
    managed_destinations = {
        str(entry.get("destination", "")).casefold()
        for entry in (managed_manifest or {}).get("files", [])
    }
    managed_destinations.update(
        str(destination).casefold() for destination in (managed_manifest or {}).get("removed", [])
    )
    data_directory_names = {"cache", "cached", "data", "output", "outputs", "metadata", "logs"}
    data_suffixes = {
        ".log",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".onnx",
        ".pt",
        ".pth",
        ".engine",
    }
    source_root = install_root / "src"
    if source_root.is_dir():
        for source in _walk_unlinked_files(source_root):
            relative_text = source.relative_to(install_root).as_posix()
            source_parts = {part.casefold() for part in source.relative_to(source_root).parts[:-1]}
            relative_from_source = source.relative_to(source_root)
            is_model_artifact = bool(
                relative_from_source.parts
                and relative_from_source.parts[0].casefold() == "models"
            )
            is_user_artifact = (
                is_model_artifact
                or source.suffix.casefold() in data_suffixes
                or bool(source_parts & data_directory_names)
            )
            if (
                is_user_artifact
                and relative_text.casefold() not in managed_destinations
                and relative_text not in manifest
                and not relative_text.casefold().startswith("src/venv/")
            ):
                target = backup_root / Path(*PurePosixPath(relative_text).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                manifest.append(relative_text)
    atomic_write_json(backup_root / "user-data-manifest.json", {"paths": manifest})
    return backup_root


def restore_user_data(install_root: Path | str, backup_root: Path | str) -> None:
    install_root = Path(install_root).resolve()
    backup_root = Path(backup_root).resolve()
    payload = read_json(backup_root / "user-data-manifest.json", {}) or {}
    for relative_text in payload.get("paths", []):
        source = backup_root / Path(*_safe_relative(relative_text, "user data").parts)
        destination = safe_destination(install_root, relative_text)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        elif source.is_file():
            shutil.copy2(source, destination)


def acquire_app_lock(install_root: Path | str) -> bool:
    paths = UpdatePaths(Path(install_root).resolve())
    paths.updates.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "process_marker": process_start_marker(os.getpid()),
        "started_at": time.time(),
    }
    for _ in range(2):
        try:
            fd = os.open(paths.app_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except FileExistsError:
            try:
                existing = _read_lock_payload(paths.app_lock) or {}
            except UpdateActiveError:
                return False
            if int(existing.get("pid", 0) or 0) == os.getpid():
                return True
            if lock_owner_is_alive(existing):
                return False
            with contextlib.suppress(OSError):
                paths.app_lock.unlink()
    return False


def release_app_lock(install_root: Path | str) -> None:
    paths = UpdatePaths(Path(install_root).resolve())
    try:
        existing = _read_lock_payload(paths.app_lock) or {}
    except UpdateActiveError:
        return
    if int(existing.get("pid", 0) or 0) == os.getpid():
        paths.app_lock.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ENIMAS transactional updater")
    parser.add_argument("--install-root", default=str(DEFAULT_INSTALL_ROOT))
    parser.add_argument("--manifest-url", default=DEFAULT_MANIFEST_URL)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check")
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--no-restart", action="store_true")
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--plan", required=True)
    apply_parser.add_argument("--wait-pid", type=int, default=0)
    apply_parser.add_argument("--restart", action="store_true")
    sub.add_parser("recover")
    sub.add_parser("startup-check")
    sub.add_parser("installation-status")
    sub.add_parser("migration-status")
    sub.add_parser("application-status")
    backup = sub.add_parser("backup-user-data")
    backup.add_argument("--backup-root", required=True)
    restore = sub.add_parser("restore-user-data")
    restore.add_argument("--backup-root", required=True)
    install = sub.add_parser("fresh-install")
    install.add_argument("--repair", action="store_true")
    install.add_argument("--control-root")
    system_prepare = sub.add_parser("prepare-system")
    system_prepare.add_argument("--control-root", required=True)
    system_prepare.add_argument("--fresh", action="store_true")
    system_prepare.add_argument("--control-only", action="store_true")
    activate_control = sub.add_parser("activate-control-plane")
    activate_control.add_argument("--control-root", required=True)
    sub.add_parser("maintenance-status")
    sub.add_parser("sync-dependencies")
    sub.add_parser("validate-install")
    sub.add_parser("complete-install")
    sub.add_parser("cleanup-install")
    abort = sub.add_parser("abort-install")
    abort.add_argument("--error", default="")
    sub.add_parser("rollback-last")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    install_root = Path(args.install_root)
    try:
        if args.command == "check":
            manifest = fetch_manifest(args.manifest_url)
            current = read_local_version(install_root)
            print(json.dumps({"current": current, "manifest": manifest, "available": is_newer_version(manifest["version"], current)}))
            return EXIT_OK
        if args.command == "bootstrap":
            return bootstrap_update(args.manifest_url, install_root, restart=not args.no_restart)
        if args.command == "apply":
            apply_update(args.plan, install_root, wait_pid=args.wait_pid, restart=args.restart)
            return EXIT_OK
        if args.command == "recover":
            return EXIT_RECOVERED if recover_interrupted_update(install_root) else EXIT_OK
        if args.command == "startup-check":
            ensure_installation_is_ready(install_root)
            recover_interrupted_update(install_root)
            return EXIT_OK
        if args.command == "installation-status":
            install_state = read_json(UpdatePaths(install_root.resolve()).install_journal, {}) or {}
            if install_state and install_state.get("phase") not in {"complete", "rolled_back"}:
                print(f"Incomplete installation phase: {install_state.get('phase', 'unknown')}")
                return EXIT_INSTALL_INCOMPLETE
            if install_state.get("phase") == "complete" and not install_state.get("cleanup_completed_at"):
                cleanup_completed_install(install_root)
            return EXIT_OK
        if args.command == "migration-status":
            validate_incremental_installation(install_root)
            return EXIT_OK
        if args.command == "application-status":
            paths = UpdatePaths(install_root.resolve())
            if paths.app_lock.exists():
                try:
                    owner = _read_lock_payload(paths.app_lock) or {}
                except UpdateActiveError:
                    return EXIT_UPDATE_ACTIVE
                if lock_owner_is_alive(owner):
                    return EXIT_UPDATE_ACTIVE
                paths.app_lock.unlink(missing_ok=True)
            return EXIT_OK
        if args.command == "backup-user-data":
            backup_user_data(install_root, args.backup_root)
            return EXIT_OK
        if args.command == "restore-user-data":
            restore_user_data(install_root, args.backup_root)
            return EXIT_OK
        if args.command == "fresh-install":
            try:
                prepare_fresh_or_repair_install(
                    args.manifest_url,
                    install_root,
                    repair=args.repair,
                    control_root=args.control_root,
                    progress=lambda phase, current, total, message: print(
                        f"[{phase}] {int(current * 100 / total) if total else 0:3d}% {message}"
                    ),
                )
            except Exception:
                with contextlib.suppress(Exception):
                    abort_fresh_or_repair_install(install_root, "Source preparation failed")
                raise
            return EXIT_OK
        if args.command == "prepare-system":
            # This command runs in the short-lived elevated maintenance process.
            # Configure its durable log explicitly because it does not enter the
            # ordinary update/install transaction helpers that configure logging.
            configure_logging(UpdatePaths(install_root.resolve()), verbose=True)
            prepare_system_maintenance(
                args.manifest_url,
                args.control_root,
                fresh=args.fresh,
                control_only=args.control_only,
                progress=lambda phase, current, total, message: print(
                    f"[{phase}] {int(current * 100 / total) if total else 0:3d}% {message}"
                ),
            )
            return EXIT_OK
        if args.command == "sync-dependencies":
            synchronize_installed_dependencies(install_root)
            return EXIT_OK
        if args.command == "activate-control-plane":
            activate_control_plane(args.control_root)
            return EXIT_OK
        if args.command == "maintenance-status":
            manifest = fetch_manifest(args.manifest_url)
            if manifest["system_update_required"]:
                print(f"ENIMAS {manifest['version']} requires administrator system maintenance")
                return EXIT_MAINTENANCE_REQUIRED
            return EXIT_OK
        if args.command == "complete-install":
            complete_fresh_or_repair_install(install_root)
            return EXIT_OK
        if args.command == "cleanup-install":
            cleanup_completed_install(install_root)
            return EXIT_OK
        if args.command == "validate-install":
            validate_fresh_or_repair_install(install_root)
            return EXIT_OK
        if args.command == "abort-install":
            abort_fresh_or_repair_install(install_root, args.error)
            return EXIT_OK
        if args.command == "rollback-last":
            rollback_latest_update(install_root)
            return EXIT_OK
    except UpdateActiveError as exc:
        print(f"Update already active: {exc}", file=sys.stderr)
        return EXIT_UPDATE_ACTIVE
    except MaintenanceRequiredError as exc:
        print(f"Administrator maintenance required: {exc}", file=sys.stderr)
        return EXIT_MAINTENANCE_REQUIRED
    except RepairRequiredError as exc:
        print(f"ENIMAS Repair required: {exc}", file=sys.stderr)
        return EXIT_REPAIR_REQUIRED
    except Exception as exc:
        LOGGER.exception("Updater command failed")
        print(f"ENIMAS update failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
