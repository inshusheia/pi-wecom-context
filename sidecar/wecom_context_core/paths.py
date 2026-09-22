from __future__ import annotations

import os
from pathlib import Path


APP_DIR = Path(os.environ.get("WECOM_CONTEXT_APP_DIR", "~/Library/Application Support/WeCom Context")).expanduser()
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_VAULT_ROOT = Path("~/Library/Application Support/wecom-local-vault").expanduser()
