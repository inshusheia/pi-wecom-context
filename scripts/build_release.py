from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import plistlib
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps" / "desktop"
TAURI = APP / "src-tauri"
SIDECAR_PYTHON = ROOT / "sidecar" / ".venv" / "bin" / "python"
SIDECAR_APP_DIR = TAURI / "resources" / "wecom-context-core"
SIDECAR_BINARY = SIDECAR_APP_DIR / "wecom-context-core"
EVIDENCE = ROOT / "evidence" / "rc0" / "build.json"
TAURI_CONF = TAURI / "tauri.conf.json"
DIST = ROOT / "dist"
FRIDA_GUIDE = ROOT / "docs" / "frida-setup.html"

def run(command: list[str], *, cwd: Path) -> None:
    print("$", " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def app_bundle() -> Path:
    candidates = sorted((TAURI / "target" / "release" / "bundle" / "macos").glob("*.app"))
    if not candidates:
        raise SystemExit("Tauri 没有生成 macOS .app")
    return candidates[-1]


def verify_arm64(path: Path) -> str:
    result = subprocess.run(["file", str(path)], capture_output=True, text=True, check=True)
    description = result.stdout.strip()
    if "Mach-O" not in description or "arm64" not in description:
        raise SystemExit(f"Sidecar 架构不是 arm64: {description}")
    return description.split(": ", 1)[-1]


def dmg_path() -> Path:
    config = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    return DIST / f"{config['productName']}_{config['version']}_aarch64.dmg"


def build_dmg(bundle: Path) -> Path:
    output = dmg_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not FRIDA_GUIDE.is_file():
        raise SystemExit(f"缺少 DMG 说明页: {FRIDA_GUIDE}")
    stage = Path(tempfile.mkdtemp(prefix="wecom-context-dmg-"))
    try:
        run(["ditto", str(bundle), str(stage / "WeCom Context.app")], cwd=ROOT)
        shutil.copy2(FRIDA_GUIDE, stage / "Frida 安装与取钥步骤.html")
        (stage / "Applications").symlink_to("/Applications")
        run(
            [
                "hdiutil",
                "create",
                "-volname",
                "WeCom Context",
                "-srcfolder",
                str(stage),
                "-ov",
                "-format",
                "UDZO",
                str(output),
            ],
            cwd=ROOT,
        )
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return output


def verify_dmg(dmg: Path) -> None:
    mount = Path(tempfile.mkdtemp(prefix="wecom-context-dmg-mount-"))
    try:
        run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(dmg)], cwd=ROOT)
        verify_bundle(mount / "WeCom Context.app")
        guide = mount / "Frida 安装与取钥步骤.html"
        if not guide.is_file() or "17.18.0" not in guide.read_text(encoding="utf-8") or "普通用户无需手动安装" not in guide.read_text(encoding="utf-8"):
            raise SystemExit("DMG 缺少有效的 Frida 离线说明页")
    finally:
        subprocess.run(["hdiutil", "detach", str(mount)], cwd=ROOT, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(mount, ignore_errors=True)


def add_apple_events_usage_description(bundle: Path) -> None:
    info_plist = bundle / "Contents" / "Info.plist"
    with info_plist.open("rb") as handle:
        plist = plistlib.load(handle)
    plist["NSAppleEventsUsageDescription"] = (
        "WeCom Context needs to control System Events and WeCom to locate conversations "
        "and restore missing images."
    )
    with info_plist.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=False)


def verify_privacy_usage_description(bundle: Path) -> None:
    info_plist = bundle / "Contents" / "Info.plist"
    with info_plist.open("rb") as handle:
        plist = plistlib.load(handle)
    if not plist.get("NSAppleEventsUsageDescription"):
        raise SystemExit("App 缺少 NSAppleEventsUsageDescription")


def verify_bundle(bundle: Path) -> None:

    forbidden_names = ("keys-", "auth.json", "snapshot", "chat-history", "raw-key", "embedded-provider.json")
    violations = [
        str(path.relative_to(bundle))
        for path in bundle.rglob("*")
        if path.is_file() and any(token in path.name.lower() for token in forbidden_names)
    ]
    if violations:
        raise SystemExit(f"Bundle 包含敏感文件名: {violations}")
    run(["codesign", "--verify", "--deep", "--strict", str(bundle)], cwd=ROOT)


def main() -> int:
    if not SIDECAR_PYTHON.is_file():
        raise SystemExit(f"缺少 Sidecar 构建 Python: {SIDECAR_PYTHON}")

    run(["npm", "run", "check"], cwd=ROOT)
    run(["python3", "contracts/validate_contracts.py"], cwd=ROOT)
    run([str(SIDECAR_PYTHON), "-m", "unittest", "discover", "-s", "sidecar/tests", "-p", "test_*.py"], cwd=ROOT)
    run(["python3", "scripts/stage-sidecar-resources.py"], cwd=ROOT)
    run(["node", "scripts/stage-connector.mjs"], cwd=ROOT)
    run(["python3", "scripts/stage-pi-runtime.py"], cwd=ROOT)
    run([str(SIDECAR_PYTHON), "scripts/build_sidecar.py"], cwd=ROOT)
    architecture = verify_arm64(SIDECAR_BINARY)
    run(["npm", "run", "build"], cwd=APP)
    run(["npx", "tauri", "build", "--bundles", "app"], cwd=APP)

    bundle = app_bundle()
    add_apple_events_usage_description(bundle)
    verify_privacy_usage_description(bundle)
    run(["codesign", "--force", "--deep", "--sign", "-", str(bundle)], cwd=ROOT)
    verify_bundle(bundle)
    dmg = build_dmg(bundle)
    verify_dmg(dmg)

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "passed",
        "target": "aarch64-apple-darwin",
        "sidecar": {
            "path": str(SIDECAR_BINARY.relative_to(ROOT)),
            "sha256": sha256(SIDECAR_BINARY),
            "file": architecture,
        },
        "app": {
            "path": str(bundle.relative_to(ROOT)),
            "ad_hoc_signed": True,
        },
        "dmg": {
            "path": str(dmg.relative_to(ROOT)),
            "sha256": sha256(dmg),
            "app_signed": True,
        },
        "checks": [
            "root_typecheck_and_bun_tests",
            "contract_validation",
            "sidecar_tests",
            "resource_staging",
            "arm64_sidecar_build",
            "frontend_build",
            "tauri_macos_app",
            "manual_dmg_creation",
            "dmg_content_signature",
            "bundle_security_scan",
        ],
    }
    EVIDENCE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
