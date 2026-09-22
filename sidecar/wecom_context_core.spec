from pathlib import Path

from PyInstaller.building.build_main import Analysis, PYZ, EXE
from PyInstaller.building.api import COLLECT

ROOT = Path(SPECPATH).resolve().parent
VAULT_SCRIPTS = ROOT / "third_party" / "yichen-skills" / "yichen-wecom-local-vault" / "scripts"

analysis = Analysis(
    [str(ROOT / "sidecar" / "entrypoint.py")],
    pathex=[str(ROOT / "sidecar"), str(VAULT_SCRIPTS)],
    binaries=[],
    datas=[(str(VAULT_SCRIPTS / name), "vault_runtime") for name in ("vault_cli.py", "wecom_common.py", "wecom_crypto.py", "capture_key_macos.py")],
    hiddenimports=["Crypto", "Crypto.Cipher", "Crypto.Cipher.AES", "Crypto.Hash", "vault_cli", "wecom_common", "wecom_crypto", "capture_key_macos", "frida", "frida._frida"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "Crypto.SelfTest"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="wecom-context-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="wecom-context-core",
)
