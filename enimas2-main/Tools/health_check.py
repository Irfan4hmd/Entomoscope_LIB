"""Hardware-independent validation run before an ENIMAS update is committed."""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class HealthCheckError(RuntimeError):
    pass


def _source_root(path: Path) -> Path:
    path = path.resolve()
    if (path / "src" / "main.py").is_file():
        return path / "src"
    return path


def _read_app_version(constants_path: Path) -> str:
    tree = ast.parse(constants_path.read_text(encoding="utf-8"), filename=str(constants_path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "APP_VERSION":
                    value = ast.literal_eval(node.value)
                    if isinstance(value, str):
                        return value
    raise HealthCheckError("constants.py does not define a string APP_VERSION")


def _runtime_import_smoke(source: Path) -> None:
    code = (
        "import importlib.util,os,sys;"
        "source=sys.argv[1];"
        "sys.path.insert(0,source);"
        "os.chdir(sys.argv[2]);"
        "import main;"
        "from PluginBase import PluginBase;"
        "builtins=('classification','cropping','measurement','Uniform_Background');"
        "plugins=os.path.join(source,'plugins');"
        "\nfor name in builtins:"
        "\n files=sorted(f for f in os.listdir(os.path.join(plugins,name)) if f.endswith('.py'))"
        "\n if not files: raise RuntimeError('Built-in plugin has no Python entrypoint: '+name)"
        "\n path=os.path.join(plugins,name,files[0])"
        "\n spec=importlib.util.spec_from_file_location('enimas_health_plugin_'+name,path)"
        "\n module=importlib.util.module_from_spec(spec)"
        "\n spec.loader.exec_module(module)"
        "\n if name=='measurement': module.PLUGIN_M_CONFIG_PATH=os.path.join(sys.argv[2],'plugin_m_config.json')"
        "\n cls=getattr(module,'Plugin',None)"
        "\n if cls is None or not issubclass(cls,PluginBase): raise RuntimeError('Invalid built-in plugin: '+name)"
        "\n cls()"
    )
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "QT_QPA_PLATFORM": "offscreen",
            "ENIMAS_HEALTH_CHECK": "1",
        }
    )
    with tempfile.TemporaryDirectory(prefix="enimas-health-") as working:
        working_path = Path(working)
        # Some legacy built-in plugins load data relative to cwd or can create
        # defaults on construction. Give them read/write disposable copies so
        # runtime validation exercises the real constructors without mutating
        # preserved user settings in the installation.
        shutil.copy2(source / "lenses.json", working_path / "lenses.json")
        measurement_config = source / "plugins" / "measurement" / "plugin_m_config.json"
        if measurement_config.is_file():
            shutil.copy2(measurement_config, working_path / "plugin_m_config.json")
        environment["MPLCONFIGDIR"] = working
        completed = subprocess.run(
            [sys.executable, "-I", "-c", code, str(source), working],
            cwd=working,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "unknown import failure").strip()
        raise HealthCheckError(f"Application import smoke test failed: {details[-2000:]}")


def run_health_check(
    root: Path,
    expected_version: str | None = None,
    *,
    runtime_imports: bool = True,
) -> None:
    source = _source_root(root)
    required = (
        source / "main.py",
        source / "constants.py",
        source / "UserInterface" / "ui.py",
        source / "Tools" / "enimas_updater.py",
        source / "Tools" / "update_manager.py",
        source / "lenses.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise HealthCheckError("Required application files are missing: " + ", ".join(missing))

    actual_version = _read_app_version(source / "constants.py")
    if expected_version and actual_version != expected_version:
        raise HealthCheckError(
            f"Application version is {actual_version}, expected {expected_version}"
        )

    lenses_path = source / "lenses.json"
    with lenses_path.open("r", encoding="utf-8") as handle:
        json.load(handle)

    for directory, subdirectories, filenames in os.walk(source):
        subdirectories[:] = [
            name
            for name in subdirectories
            if name.casefold() not in {"venv", "models", "__pycache__", ".git", "updates"}
        ]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = Path(directory) / filename
            try:
                ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            except (OSError, UnicodeError, SyntaxError) as exc:
                raise HealthCheckError(f"Python validation failed for {path}: {exc}") from exc
    if runtime_imports:
        _runtime_import_smoke(source)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate ENIMAS without opening hardware")
    parser.add_argument("--root", default=".")
    parser.add_argument("--expected-version")
    parser.add_argument(
        "--syntax-only",
        action="store_true",
        help="Developer-only check; production updater always runs the import smoke test.",
    )
    args = parser.parse_args()
    try:
        run_health_check(
            Path(args.root),
            args.expected_version,
            runtime_imports=not args.syntax_only,
        )
    except Exception as exc:
        print(f"ENIMAS health check failed: {exc}")
        return 1
    print("ENIMAS health check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
