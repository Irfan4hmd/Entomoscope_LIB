"""Regenerate ENIMAS' reproducible, fully hashed Python dependency lock."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path


REFINERS_WHEEL = "vendor/refiners-0.4.0-py3-none-any.whl"
REFINERS_SHA256 = "c4a92663e4f735659049ab6cfabd1d1c5d8613df77852f2c6244e792af6d804a"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    wheel = root / REFINERS_WHEEL
    actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if actual != REFINERS_SHA256:
        raise RuntimeError(f"Vendored refiners wheel hash changed: {actual}")
    lock = root / "requirements.lock"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "piptools",
            "compile",
            "requirements.txt",
            "--generate-hashes",
            "--strip-extras",
            "--allow-unsafe",
            "--resolver=backtracking",
            "--extra-index-url",
            "https://download.pytorch.org/whl/cpu",
            "--output-file",
            str(lock),
        ],
        cwd=root,
        check=True,
    )
    contents = lock.read_text(encoding="utf-8")
    absolute = wheel.resolve().as_uri()
    expected = f"refiners @ {absolute}"
    if contents.count(expected) != 1:
        raise RuntimeError("pip-compile did not emit the expected pinned refiners wheel")
    contents = contents.replace(expected, f"./{REFINERS_WHEEL}")
    if f"--hash=sha256:{REFINERS_SHA256}" not in contents:
        raise RuntimeError("pip-compile omitted the vendored refiners hash")
    lock.write_text(contents, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
