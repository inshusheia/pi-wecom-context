from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "apps" / "desktop" / "src-tauri" / "resources"
VAULT_SOURCE = ROOT / "third_party" / "yichen-skills" / "yichen-wecom-local-vault" / "scripts"

for relative in ("vault_cli.py", "wecom_common.py", "wecom_crypto.py"):
    destination = TARGET / "vault" / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(VAULT_SOURCE / relative, destination)

resolver = TARGET / "contact_resolver.py"
shutil.copy2(ROOT / "phase2" / "contact_resolver.py", resolver)
print("staged Sidecar runtime resources")
