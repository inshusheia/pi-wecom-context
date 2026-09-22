from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "sidecar" / "wecom_context_core.spec"
BUILD = ROOT / "sidecar" / "build"
DIST = ROOT / "sidecar" / "dist"
TAURI_RESOURCES = ROOT / "apps" / "desktop" / "src-tauri" / "resources"
APP_DIR = TAURI_RESOURCES / "wecom-context-core"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError as error:
        raise SystemExit(
            "PyInstaller 未安装；请在网络可用时执行 "
            "sidecar/.venv/bin/python -m pip install -r sidecar/requirements-build.txt"
        ) from error

    for path in (BUILD, DIST):
        shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(APP_DIR, ignore_errors=True)
    APP_DIR.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(DIST),
            "--workpath",
            str(BUILD),
            str(SPEC),
        ],
        cwd=ROOT,
        check=True,
    )
    built_dir = DIST / "wecom-context-core"
    built = built_dir / "wecom-context-core"
    if not built.is_file():
        raise SystemExit(f"PyInstaller 未生成 {built}")
    shutil.copytree(built_dir, APP_DIR)
    built.chmod(0o755)
    digest = hashlib.sha256(built.read_bytes()).hexdigest()
    (DIST / f"{built.name}.sha256").write_text(f"{digest}  {built.name}\n", encoding="utf-8")
    print(f"built {APP_DIR} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
