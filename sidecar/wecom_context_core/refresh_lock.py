from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .vault_client import VaultError

LOCK_MAX_AGE_SECONDS = 30 * 60


@contextmanager
def refresh_lock(vault_root: Path):
    private = vault_root / "private"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = private / "refresh.lock"
    payload = {"pid": os.getpid(), "started_at": time.time(), "operation": "snapshot_refresh"}
    acquired = False
    try:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            acquired = True
        except FileExistsError:
            stale = False
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                pid = int(current.get("pid", -1))
                started = float(current.get("started_at", 0))
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
                stale = not alive and time.time() - started > LOCK_MAX_AGE_SECONDS
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                stale = False
            if not stale:
                raise VaultError("REFRESH_LOCKED", "已有快照刷新任务正在执行", True)
            path.unlink(missing_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            acquired = True
        if not acquired:
            raise VaultError("REFRESH_LOCKED", "无法获取刷新锁", True)
        yield
    finally:
        if acquired:
            path.unlink(missing_ok=True)
