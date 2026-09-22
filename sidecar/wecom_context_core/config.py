from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFIG_VERSION = 2

LEGACY_CONFIG_FIELDS = ("session_key_map", "allowlisted_session_keys", "historySnapshotPath")


class ConfigError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def binding_key(dataset_id: str, session_key_value: str) -> str:
    return f"{dataset_id}:{session_key_value}"


def session_bindings(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("session_bindings")
    return value if isinstance(value, dict) else {}


def find_binding(config: dict[str, Any], session_key_value: Any, dataset_id: Any = None) -> dict[str, Any] | None:
    """按 session_key 查找绑定；给定 dataset_id 时按 dataset:session 精确命中。"""
    if not isinstance(session_key_value, str) or not session_key_value:
        return None
    bindings = session_bindings(config)
    if isinstance(dataset_id, str) and dataset_id:
        value = bindings.get(binding_key(dataset_id, session_key_value))
        return value if isinstance(value, dict) else None
    matches = [value for value in bindings.values() if isinstance(value, dict) and value.get("session_key") == session_key_value]
    if not matches:
        return None
    selected = config.get("selected_dataset_id")
    preferred = next((value for value in matches if value.get("dataset_id") == selected), None)
    return preferred or sorted(matches, key=lambda value: str(value.get("dataset_id") or ""))[0]


def set_binding(
    config: dict[str, Any],
    *,
    session_key_value: str,
    conversation_id: str,
    dataset_id: str,
    snapshot_id: str,
    snapshot_path: str,
    mode: str = "active",
    bound_at: str | None = None,
) -> dict[str, Any]:
    updated = dict(config)
    bindings = dict(session_bindings(config))
    bindings[binding_key(dataset_id, session_key_value)] = {
        "session_key": session_key_value,
        "conversation_id": conversation_id,
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "snapshot_path": snapshot_path,
        "mode": mode,
        "bound_at": bound_at or utc_now(),
    }
    updated["session_bindings"] = bindings
    return updated


def ignored_dataset_ids(config: dict[str, Any]) -> list[str]:
    """已被用户从应用移除的企业（仅在本地忽略，不动企业微信数据）。"""
    value = config.get("ignored_datasets")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def set_dataset_ignored(config: dict[str, Any], dataset_id: str, *, ignored: bool) -> dict[str, Any]:
    """增删忽略列表；幂等，返回新配置（调用方负责写盘）。"""
    current = ignored_dataset_ids(config)
    if ignored:
        if dataset_id not in current:
            current.append(dataset_id)
    else:
        current = [item for item in current if item != dataset_id]
    updated = dict(config)
    if current:
        updated["ignored_datasets"] = current
    else:
        updated.pop("ignored_datasets", None)
    return updated


def clear_dataset_selection(config: dict[str, Any], dataset_id: str) -> dict[str, Any]:
    """清除指向该企业的选择与绑定；不碰其它企业。"""
    updated = dict(config)
    if str(config.get("selected_dataset_id") or "") == dataset_id:
        updated.pop("selected_dataset_id", None)
        updated.pop("data_dir", None)
        updated.pop("activeSnapshotPath", None)
        updated.pop("activeSnapshotCreatedAt", None)
        updated.pop("activeSnapshotDatasetId", None)
        updated.pop("selected_session_key", None)
    bindings = {
        key: value
        for key, value in session_bindings(config).items()
        if str(value.get("dataset_id") or "") != dataset_id
    }
    if bindings:
        updated["session_bindings"] = bindings
    else:
        updated.pop("session_bindings", None)
    return updated


def migrate_config(config: dict[str, Any]) -> dict[str, Any]:
    """把任意历史版本的配置规范化为 v2（纯函数，幂等，不写盘）。

    v1 → v2：session_key_map 的每条映射转成 mode=active 绑定（快照取当前活动快照）；
    旧 historySnapshotPath 对应的选中会话转成 mode=history 绑定；随后删除旧字段。
    """
    if not isinstance(config, dict):
        raise ConfigError("配置文件必须是对象")
    if config.get("configVersion") == CONFIG_VERSION and not any(field in config for field in LEGACY_CONFIG_FIELDS):
        return dict(config)

    updated: dict[str, Any] = {key: value for key, value in config.items() if key not in LEGACY_CONFIG_FIELDS}
    updated["configVersion"] = CONFIG_VERSION

    dataset_id = str(config.get("activeSnapshotDatasetId") or config.get("selected_dataset_id") or "")
    active_snapshot_value = config.get("activeSnapshotPath") or config.get("snapshotPath")
    active_snapshot_path = str(active_snapshot_value) if active_snapshot_value else ""
    active_snapshot_id = Path(active_snapshot_path).name if active_snapshot_path else ""

    bindings = dict(session_bindings(config))
    mapping = config.get("session_key_map")
    mapping = mapping if isinstance(mapping, dict) else {}
    for key, conversation_id in mapping.items():
        if not isinstance(key, str) or not key or not isinstance(conversation_id, str) or not conversation_id:
            continue
        bindings[binding_key(dataset_id, key)] = {
            "session_key": key,
            "conversation_id": conversation_id,
            "dataset_id": dataset_id,
            "snapshot_id": active_snapshot_id,
            "snapshot_path": active_snapshot_path,
            "mode": "active",
            "bound_at": utc_now(),
        }

    selected = config.get("selected_session_key")
    history_value = config.get("historySnapshotPath")
    if isinstance(history_value, str) and history_value and isinstance(selected, str) and selected:
        conversation_id = mapping.get(selected)
        if isinstance(conversation_id, str) and conversation_id:
            history_path = Path(history_value).expanduser()
            bindings[binding_key(dataset_id, selected)] = {
                "session_key": selected,
                "conversation_id": conversation_id,
                "dataset_id": dataset_id,
                "snapshot_id": history_path.name,
                "snapshot_path": str(history_path),
                "mode": "history",
                "bound_at": utc_now(),
            }

    if bindings:
        updated["session_bindings"] = bindings
    else:
        updated.pop("session_bindings", None)
    if isinstance(selected, str) and selected and any(value.get("session_key") == selected for value in bindings.values()):
        updated["selected_session_key"] = selected
    else:
        updated.pop("selected_session_key", None)
    return updated


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError("配置文件不可读取") from error
    if not isinstance(value, dict):
        raise ConfigError("配置文件必须是对象")
    if value.get("configVersion") != CONFIG_VERSION or any(field in value for field in LEGACY_CONFIG_FIELDS):
        migrated = migrate_config(value)
        save_config_atomic(path, migrated)
        return migrated
    return value


def save_config_atomic(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def activate_snapshot(
    path: Path,
    config: dict[str, Any],
    *,
    snapshot_path: str,
    created_at: str,
    dataset_id: str,
) -> dict[str, Any]:
    updated = migrate_config(config)
    updated.update(
        {
            "configVersion": CONFIG_VERSION,
            "snapshotMode": "fixed",
            "snapshotPath": snapshot_path,
            "activeSnapshotPath": snapshot_path,
            "activeSnapshotCreatedAt": created_at,
            "activeSnapshotDatasetId": dataset_id,
            "selected_dataset_id": dataset_id,
        }
    )
    save_config_atomic(path, updated)
    return updated
