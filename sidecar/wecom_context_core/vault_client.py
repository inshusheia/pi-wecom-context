from __future__ import annotations

from pathlib import Path
from typing import Any

from .contacts import resolve_sessions
from .vault_runtime import (
    VaultRuntimeError,
    all_messages as read_all_messages,
    decrypt as decrypt_snapshot,
    history as read_history,
    inspect as inspect_dataset,
)


class VaultError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class VaultClient:
    def __init__(
        self,
        *,
        root: Path,
        data_dir: Path | None = None,
        snapshot_path: Path | None = None,
        timeout_seconds: int = 10,
        refresh_timeout_seconds: int = 120,
    ):
        self.root = root.expanduser().resolve()
        self.data_dir = data_dir.expanduser().resolve() if data_dir else None
        self.snapshot_path = snapshot_path.expanduser().resolve() if snapshot_path else None
        self.timeout_seconds = timeout_seconds
        self.refresh_timeout_seconds = refresh_timeout_seconds

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"

    def latest_snapshot(self) -> Path | None:
        if self.snapshot_path is not None:
            if self.snapshot_path.is_dir() and not self.snapshot_path.is_symlink():
                return self.snapshot_path
            return None
        if not self.snapshots_dir.is_dir():
            return None
        candidates = sorted(path for path in self.snapshots_dir.iterdir() if path.is_dir() and not path.is_symlink())
        return candidates[-1] if candidates else None

    def status(self) -> dict[str, Any]:
        try:
            return inspect_dataset(self.data_dir)
        except VaultRuntimeError as error:
            raise VaultError("DATASET_UNAVAILABLE", str(error), True) from error

    def sessions_at_snapshot(self, snapshot: Path, limit: int = 50) -> dict[str, Any]:
        if not snapshot.is_dir() or snapshot.is_symlink():
            raise VaultError("SNAPSHOT_UNAVAILABLE", "没有可用明文快照")
        return resolve_sessions(snapshot, max(1, min(limit, 100)))

    def sessions(self, limit: int = 50) -> dict[str, Any]:
        snapshot = self.latest_snapshot()
        if snapshot is None:
            raise VaultError("SNAPSHOT_UNAVAILABLE", "没有可用明文快照")
        return self.sessions_at_snapshot(snapshot, limit)

    def history_at_snapshot(self, snapshot: Path, conversation_id: str, limit: int = 30, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        if not conversation_id or "\0" in conversation_id or conversation_id.startswith("-"):
            raise VaultError("INVALID_REQUEST", "会话参数无效")
        if not snapshot.is_dir() or snapshot.is_symlink():
            raise VaultError("SNAPSHOT_UNAVAILABLE", "没有可用明文快照")
        try:
            return read_history(snapshot, conversation_id, max(1, min(limit, 500)), start, end)
        except VaultRuntimeError as error:
            raise VaultError("OUTPUT_INVALID", str(error)) from error

    def all_messages_at_snapshot(self, snapshot: Path, conversation_id: str) -> dict[str, Any]:
        if not conversation_id or "\0" in conversation_id or conversation_id.startswith("-"):
            raise VaultError("INVALID_REQUEST", "会话参数无效")
        if not snapshot.is_dir() or snapshot.is_symlink():
            raise VaultError("SNAPSHOT_UNAVAILABLE", "没有可用明文快照")
        try:
            return read_all_messages(snapshot, conversation_id)
        except VaultRuntimeError as error:
            raise VaultError("OUTPUT_INVALID", str(error)) from error

    def history(self, conversation_id: str, limit: int = 30, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        snapshot = self.latest_snapshot()
        if snapshot is None:
            raise VaultError("SNAPSHOT_UNAVAILABLE", "没有可用明文快照")
        return self.history_at_snapshot(snapshot, conversation_id, limit, start, end)

    def decrypt(self) -> dict[str, Any]:
        try:
            return decrypt_snapshot(self.root, self.data_dir)
        except VaultRuntimeError as error:
            raise VaultError("REFRESH_FAILED", str(error), True) from error
