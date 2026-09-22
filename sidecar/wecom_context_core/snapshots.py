from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .vault_client import VaultError, VaultClient
from .vault_runtime import KEY_MATCH, load_matching_key_detailed

CORE_DATABASES = ("message.db", "session.db", "user.db")


def key_metadata(vault_root: Path) -> dict[str, Any]:
    private = vault_root / "private"
    if not private.is_dir():
        raise VaultError("KEY_UNAVAILABLE", "没有可用的验证密钥")
    candidates = sorted(
        path for path in private.iterdir()
        if path.is_file() and ((path.name.startswith("keys-") and path.name.endswith(".json")) or path.name == "keys.json")
    )
    if not candidates:
        raise VaultError("KEY_UNAVAILABLE", "没有可用的验证密钥")
    path = candidates[-1]
    if (path.stat().st_mode & 0o077) != 0:
        raise VaultError("KEY_PERMISSION_UNSAFE", "验证密钥权限不安全")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VaultError("KEY_UNAVAILABLE", "验证密钥文件格式无效") from error
    if not isinstance(value, dict) or not isinstance(value.get("dataset_id"), str):
        raise VaultError("KEY_UNAVAILABLE", "验证密钥文件格式无效")
    validated = value.get("validated_databases")
    if not isinstance(validated, list) or not validated:
        raise VaultError("KEY_UNAVAILABLE", "验证密钥缺少验证记录")
    return {"dataset_id": value["dataset_id"], "validated_database_count": len(validated)}


def preflight(vault_root: Path, client: VaultClient) -> dict[str, Any]:
    status = client.status()
    if status.get("database_count", 0) < 1:
        raise VaultError("DATASET_UNAVAILABLE", "没有可刷新的企微数据库", True)
    data_root = client.data_dir
    if data_root is None:
        raise VaultError("KEY_DATASET_MISMATCH", "验证密钥与当前数据集不匹配", True)
    outcome, matched = load_matching_key_detailed(data_root, vault_root / "private")
    if outcome == "read_failed":
        raise VaultError(
            "KEY_DATASET_READ_BLOCKED",
            "企业微信数据库暂时无法读取（可能被系统沙盒授权拦截）；稍后重试，或给应用授予「完全磁盘访问」后再试",
            True,
        )
    if outcome == "no_message_db":
        raise VaultError("DATASET_UNAVAILABLE", "当前数据集没有可验证的 message.db", True)
    if outcome == "no_keys":
        raise VaultError(
            "KEY_UNAVAILABLE",
            "该企业还没有本地密钥：请先运行 scripts/capture-enterprise-key.sh 为当前登录企业取钥（密钥按企业绑定）",
        )
    if outcome == "no_match" or matched is None:
        raise VaultError(
            "KEY_DATASET_MISMATCH",
            "本地密钥都无法解密当前企业的数据库；若刚切换了登录账号，请先为新企业重新取钥",
            True,
        )
    key = key_metadata_from_match(matched[1])
    previous = client.latest_snapshot()
    return {"status": status, "key": key, "previous_snapshot": str(previous) if previous else None}


def key_metadata_from_match(key_data: Any) -> dict[str, Any]:
    """用试解匹配命中的密钥文件构建展示 metadata（而不是任意最新文件）。"""
    if not isinstance(key_data, dict) or not isinstance(key_data.get("dataset_id"), str):
        raise VaultError("KEY_UNAVAILABLE", "验证密钥文件格式无效")
    validated = key_data.get("validated_databases")
    if not isinstance(validated, list) or not validated:
        raise VaultError("KEY_UNAVAILABLE", "验证密钥缺少验证记录")
    return {"dataset_id": key_data["dataset_id"], "validated_database_count": len(validated)}


def _is_within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_snapshot(snapshot_path: Path, expected_dataset_id: str, client: VaultClient) -> dict[str, Any]:
    snapshot = snapshot_path.expanduser().resolve()
    snapshots_root = client.snapshots_dir.expanduser().resolve()
    if not _is_within(snapshots_root, snapshot) or snapshot == snapshots_root:
        raise VaultError("SNAPSHOT_INVALID", "新快照路径越过 Vault 边界")
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise VaultError("SNAPSHOT_INVALID", "新快照目录无效")
    manifest_path = snapshot / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VaultError("SNAPSHOT_INVALID", "manifest 无效") from error
    if not isinstance(manifest, dict) or manifest.get("contains_plaintext_wecom_data") is not True:
        raise VaultError("SNAPSHOT_INVALID", "新快照不是明文企业微信快照")
    if manifest.get("dataset_id") != expected_dataset_id:
        raise VaultError("SNAPSHOT_INVALID", "新快照数据集不一致")
    for name in CORE_DATABASES:
        path = snapshot / name
        if not path.is_file() or path.is_symlink():
            raise VaultError("SNAPSHOT_INVALID", f"新快照缺少 {name}")
        with path.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                raise VaultError("SNAPSHOT_INVALID", f"{name} 不是明文 SQLite")
    sessions = client.sessions_at_snapshot(snapshot, limit=1)
    return {"snapshot": str(snapshot), "manifest": manifest, "session_count": sessions.get("count", 0)}
