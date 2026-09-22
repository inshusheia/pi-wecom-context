from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "apps" / "desktop" / "src-tauri" / "resources" / "pi-runtime"


def locate(command: str, env_name: str) -> Path:
    value = os.environ.get(env_name)
    candidate = Path(value).expanduser() if value else None
    if candidate is None:
        found = shutil.which(command)
        if found:
            candidate = Path(found)
    if candidate is None or not candidate.exists():
        raise SystemExit(f"找不到 {command}；可用 {env_name} 指定路径")
    return candidate.resolve()


def copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise SystemExit(f"缺少 Pi 运行时目录: {source}")
    shutil.copytree(source, destination, symlinks=False)


def main() -> int:
    node = locate("node", "WECOM_PI_NODE")
    pi_entry = locate("pi", "WECOM_PI_BIN")
    node_description = subprocess.run(["file", str(node)], check=True, capture_output=True, text=True).stdout
    if "Mach-O" not in node_description or "arm64" not in node_description:
        raise SystemExit(f"内置 Node 必须是 arm64 Mach-O: {node_description.strip()}")
    node_version = subprocess.run([str(node), "--version"], check=True, capture_output=True, text=True).stdout.strip()
    try:
        node_version_tuple = tuple(int(part) for part in node_version.removeprefix("v").split(".")[:3])
    except ValueError as error:
        raise SystemExit(f"无法解析 Node 版本: {node_version}") from error
    if node_version_tuple < (22, 19, 0):
        raise SystemExit(f"Pi 需要 Node >=22.19.0，当前是 {node_version}")

    package_root = pi_entry.parents[2]
    bundle = package_root / "dist" / "bundle"
    package_json = package_root / "package.json"
    if not bundle.is_dir() or not package_json.is_file():
        raise SystemExit(f"Pi CLI 入口不对应 npm 包目录: {package_root}")
    package = json.loads(package_json.read_text(encoding="utf-8"))
    pi_version = str(package.get("version") or "unknown")

    dependencies = (
        ("@earendil-works/chord", package_root / "node_modules" / "@earendil-works" / "chord"),
        ("esbuild", package_root / "node_modules" / "esbuild"),
        ("@esbuild/darwin-arm64", package_root / "node_modules" / "@esbuild" / "darwin-arm64"),
        ("jiti", package_root / "node_modules" / "jiti"),
        ("typebox", package_root / "node_modules" / "typebox"),
    )
    for label, source in dependencies:
        if not source.is_dir():
            raise SystemExit(f"缺少 Pi 运行依赖 {label}: {source}")

    assets = (
        "modes/interactive/theme",
        "modes/interactive/assets",
        "core/export-html",
    )
    for relative in assets:
        source = package_root / "dist" / relative
        if not source.is_dir():
            raise SystemExit(f"缺少 Pi 资源目录: {source}")

    shutil.rmtree(TARGET, ignore_errors=True)
    (TARGET / "package" / "dist").mkdir(parents=True, exist_ok=True)
    (TARGET / "package" / "node_modules" / "@earendil-works").mkdir(parents=True, exist_ok=True)
    (TARGET / "package" / "node_modules" / "@esbuild").mkdir(parents=True, exist_ok=True)

    shutil.copy2(node, TARGET / "node")
    shutil.copystat(node, TARGET / "node", follow_symlinks=False)
    (TARGET / "node").chmod((TARGET / "node").stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    shutil.copy2(package_json, TARGET / "package" / "package.json")
    copy_tree(bundle, TARGET / "package" / "dist" / "bundle")
    for relative in assets:
        copy_tree(package_root / "dist" / relative, TARGET / "package" / "dist" / relative)
    copy_tree(dependencies[0][1], TARGET / "package" / "node_modules" / "@earendil-works" / "chord")
    copy_tree(dependencies[1][1], TARGET / "package" / "node_modules" / "esbuild")
    copy_tree(dependencies[2][1], TARGET / "package" / "node_modules" / "@esbuild" / "darwin-arm64")
    copy_tree(dependencies[3][1], TARGET / "package" / "node_modules" / "jiti")
    copy_tree(dependencies[4][1], TARGET / "package" / "node_modules" / "typebox")

    launcher = TARGET / "pi"
    launcher.write_text(
        '#!/bin/sh\n'
        'set -eu\n'
        'SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
        'exec "$SCRIPT_DIR/node" "$SCRIPT_DIR/package/dist/bundle/cli.js" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    version = subprocess.run([str(launcher), "--version"], check=True, capture_output=True, text=True).stdout.strip()
    print(f"staged Pi runtime {version} with Node {node_version}: {TARGET}")
    if version != pi_version:
        raise SystemExit(f"Pi 版本校验失败: package.json={pi_version}, cli={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
