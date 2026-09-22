"""Generate the deterministic stable-channel update manifest.

Run this only from a clean release candidate checkout.  The generated manifest
is committed with the release and references an immutable tag created from the
same commit before ``main`` is advanced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


REPOSITORY_RAW_URL = "https://gitlab.kit.edu/kit/iai/ber/enimas2/-/raw/{ref}"
UPDATER_PATH = "Tools/enimas_updater.py"
PYTORCH_CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"

# Data edited or created by users is intentionally never application-managed.
EXCLUDED_EXACT = {
    "config.txt",
    "lenses.json",
    "plugins/measurement/plugin_m_config.json",
    "update_manifest.json",
    ".gitignore",
    ".gitattributes",
    "install_test.bat",
    "ENIMAS_test.bat",
    "test_hidden_shortcut.bat",
}
EXCLUDED_PREFIXES = (
    "tests/",
    "models/classification/",
    "UserInterface/Partslist/",
    "3D_printed_parts/",
    "git/",
)
SYSTEM_FILE_PREFIXES = ("camera-driver/",)
LEGACY_REMOVALS = {
    # The old installer binary is no longer used or shipped. Only this exact
    # previously released path is removed; any other files under src/git stay.
    "src/git/Git-2.50.1-64-bit.exe",
    # Shipped through 1.0.7 and superseded by the transactional updater.
    "src/update.bat",
}

LFS_POINTER_PATTERN = re.compile(
    rb"\Aversion https://git-lfs\.github\.com/spec/v1\r?\n"
    rb"oid sha256:([0-9a-f]{64})\r?\nsize ([0-9]+)\r?\n?\Z"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def tracked_files(repo_root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    paths = completed.stdout.decode("utf-8").split("\0")
    return sorted(path for path in paths if path)


def tracked_files_at_ref(repo_root: Path, reference: str) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", reference],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    return sorted(path for path in completed.stdout.decode("utf-8").split("\0") if path)


def committed_blob_bytes(repo_root: Path, reference: str, path: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", f"{reference}:{path}"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def lfs_pointer_metadata(data: bytes) -> tuple[str, int] | None:
    match = LFS_POINTER_PATTERN.fullmatch(data)
    if match is None:
        return None
    return match.group(1).decode("ascii"), int(match.group(2))


def committed_file_bytes(repo_root: Path, path: str) -> bytes:
    """Return committed payload bytes, materializing a verified Git LFS object."""
    blob = committed_blob_bytes(repo_root, "HEAD", path)
    pointer = lfs_pointer_metadata(blob)
    if pointer is None:
        return blob
    expected_hash, expected_size = pointer
    candidates = [repo_root / Path(*PurePosixPath(path).parts)]
    git_path = subprocess.run(
        ["git", "rev-parse", "--git-path", f"lfs/objects/{expected_hash[:2]}/{expected_hash[2:4]}/{expected_hash}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if git_path.returncode == 0 and git_path.stdout.strip():
        candidate = Path(git_path.stdout.strip())
        candidates.append(candidate if candidate.is_absolute() else repo_root / candidate)
    for candidate in candidates:
        if not candidate.is_file() or candidate.stat().st_size != expected_size:
            continue
        data = candidate.read_bytes()
        if hashlib.sha256(data).hexdigest() == expected_hash:
            return data
    raise RuntimeError(
        f"Git LFS payload is not hydrated or does not match its committed pointer: {path}"
    )


def assert_release_tree_is_committed(repo_root: Path) -> None:
    dirty = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--"],
        cwd=repo_root,
        check=False,
    )
    if dirty.returncode != 0:
        raise RuntimeError("Commit all release files before generating GitLab payload hashes")
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8").split("\0")
    unmanaged_release_files = [path for path in untracked if path and is_managed(path)]
    if unmanaged_release_files:
        raise RuntimeError(
            "Commit managed release files before manifest generation: "
            + ", ".join(sorted(unmanaged_release_files))
        )


def is_managed(path: str) -> bool:
    normalized = PurePosixPath(path).as_posix()
    return (
        normalized not in EXCLUDED_EXACT
        and not normalized.startswith(EXCLUDED_PREFIXES)
        and not is_system_file(normalized)
    )


def is_system_file(path: str) -> bool:
    normalized = PurePosixPath(path).as_posix()
    return normalized.startswith(SYSTEM_FILE_PREFIXES)


def destinations_for(path: str) -> list[str]:
    # Launchers and the installer live at the installation root as well as in
    # source archives.  All application imports keep their established src path.
    if path in {"install.bat", "ENIMAS.bat"}:
        return [path, f"src/{path}"]
    return [f"src/{path}"]


def extract_release_notes(changelog: str, version: str) -> str:
    marker = f"## [{version}]"
    start = changelog.find(marker)
    if start < 0:
        return ""
    next_section = changelog.find("\n## [", start + len(marker))
    return changelog[start : next_section if next_section >= 0 else None].strip()


def build_manifest(
    repo_root: Path,
    *,
    version: str,
    source_ref: str,
    release_date: str,
    system_update_required: bool = False,
    legacy_ref: str | None = "main",
) -> dict:
    assert_release_tree_is_committed(repo_root)
    files: list[dict] = []
    system_files: list[dict] = []
    archive_files: list[dict] = []
    current_tracked = tracked_files(repo_root)
    current_system_blobs = {
        path: committed_blob_bytes(repo_root, "HEAD", path)
        for path in current_tracked
        if is_system_file(path)
    }
    previous_system_blobs: dict[str, bytes] = {}
    if legacy_ref:
        previous_system_blobs = {
            path: committed_blob_bytes(repo_root, legacy_ref, path)
            for path in tracked_files_at_ref(repo_root, legacy_ref)
            if is_system_file(path)
        }
    system_files_changed = current_system_blobs != previous_system_blobs
    if system_files_changed != system_update_required:
        expected = "required" if system_files_changed else "not allowed"
        raise RuntimeError(
            f"--system-update-required is {expected}: protected system payloads "
            f"{'changed' if system_files_changed else 'did not change'} relative to {legacy_ref}"
        )
    for relative in current_tracked:
        # A manifest cannot contain its own digest. It is not needed inside src
        # and the installer removes it before checking the archive allowlist.
        if relative == "update_manifest.json":
            continue
        data = committed_file_bytes(repo_root, relative)
        archive_files.append(
            {
                "path": relative,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        if is_system_file(relative):
            system_files.append(
                {
                    "path": relative,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
            continue
        if not is_managed(relative):
            continue
        digest = hashlib.sha256(data).hexdigest()
        size = len(data)
        for destination in destinations_for(relative):
            files.append(
                {
                    "path": relative,
                    "destination": destination,
                    "size": size,
                    "sha256": digest,
                }
            )

    updater_entry = next((entry for entry in files if entry["path"] == UPDATER_PATH), None)
    if updater_entry is None:
        raise RuntimeError(f"Committed updater is missing from the release: {UPDATER_PATH}")
    updater_digest = updater_entry["sha256"]
    requirements_digest = hashlib.sha256(committed_file_bytes(repo_root, "requirements.lock")).hexdigest()
    changelog = committed_file_bytes(repo_root, "CHANGELOG.md").decode("utf-8")
    removed: set[str] = set(LEGACY_REMOVALS)
    if legacy_ref:
        current_paths = set(current_tracked)
        for relative in tracked_files_at_ref(repo_root, legacy_ref):
            if relative not in current_paths and is_managed(relative):
                removed.update(destinations_for(relative))
    manifest = {
        "schema_version": 1,
        "version": version,
        "source_ref": source_ref,
        "repository_raw_url": REPOSITORY_RAW_URL,
        "release_date": release_date,
        "release_notes": extract_release_notes(changelog, version),
        "changelog_path": "CHANGELOG.md",
        "requirements_sha256": requirements_digest,
        "system_update_required": system_update_required,
        "updater": {"path": UPDATER_PATH, "sha256": updater_digest},
        "files": sorted(files, key=lambda item: item["destination"]),
        "archive_files": sorted(archive_files, key=lambda item: item["path"]),
        # Verified in tag-pinned fresh/repair archives, never applied by the
        # normal non-admin application updater.
        "system_files": sorted(system_files, key=lambda item: item["path"]),
        "removed": sorted(removed),
        # The target lock carries the same index declaration. Repeating this
        # allow-listed source here also lets rollback staging resolve CPU-only
        # versions from the existing environment without retaining VCS URLs.
        "pip_download_args": ["--extra-index-url", PYTORCH_CPU_INDEX_URL],
    }
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--release-date", required=True)
    parser.add_argument("--output", default="update_manifest.json")
    parser.add_argument("--system-update-required", action="store_true")
    parser.add_argument(
        "--legacy-ref",
        default="main",
        help="Stable ref used to record deleted managed files for legacy migration.",
    )
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    manifest = build_manifest(
        repo_root,
        version=args.version,
        source_ref=args.source_ref,
        release_date=args.release_date,
        system_update_required=args.system_update_required,
        legacy_ref=args.legacy_ref,
    )
    output = repo_root / args.output
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {output} with {len(manifest['files'])} managed destinations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
