from __future__ import annotations

from pathlib import Path

from .vault_client import VaultError


def _within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def validate_runtime_paths(*, vault_root: Path, data_dir: Path | None, snapshot_path: Path | None = None) -> None:
    allowed_vault_root = Path("~/Library/Application Support/wecom-local-vault").expanduser().resolve()
    if vault_root.resolve() != allowed_vault_root and not _within(allowed_vault_root, vault_root):
        raise VaultError("CONFIG_INVALID", "Vault 路径不在允许范围")
    if data_dir is not None:
        if not data_dir.is_dir() or data_dir.is_symlink():
            raise VaultError("CONFIG_INVALID", "数据集路径不安全")
        if any(not (data_dir / name).is_file() for name in ("message.db", "session.db", "user.db")):
            raise VaultError("CONFIG_INVALID", "数据集缺少核心数据库")
    if snapshot_path is not None:
        snapshots_root = (vault_root / "snapshots").resolve()
        if not _within(snapshots_root, snapshot_path) or snapshot_path.resolve() == snapshots_root:
            raise VaultError("CONFIG_INVALID", "活动快照路径不安全")
