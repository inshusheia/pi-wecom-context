from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    CONFIG_VERSION,
    LEGACY_CONFIG_FIELDS,
    clear_dataset_selection,
    find_binding,
    load_config,
    migrate_config,
    save_config_atomic,
    session_bindings,
    set_binding,
    utc_now,
    activate_snapshot,
)
from .context_reader import render_context
from .package_builder import (
    DEFAULT_MAX_TILES_PER_IMAGE,
    build_source,
    compose_text,
    normalise_request,
    package_files,
    package_id,
    package_stats,
    prune_packages,
    write_manifest,
)
from .paths import CONFIG_PATH, DEFAULT_VAULT_ROOT
from .refresh_lock import refresh_lock
from .security import validate_runtime_paths
from .capture_flow import capture_key_action
from .snapshots import key_metadata, preflight, validate_snapshot
from .vault_runtime import account_id_for, account_name_for, dataset_id as runtime_dataset_id, discover_dataset_paths, enterprise_name_for, inspect_dataset_path, key_validates_for
from .vault_client import VaultClient, VaultError
from .image_cache import (
    _cache_directory_index,
    cache_roots,
    downloaded_image_index,
    image_keys_for_messages,
    resolve_image,
)
from .client_fetch import ClientFetchTarget, fetch_images_via_client

PROTOCOL_VERSION = "1"

CONVERSATIONAL_KINDS = {"单聊", "群聊"}
MAX_HISTORY_CANDIDATES = 3
MAX_SESSION_SCAN = 100
SELECTION_MODES = ("explicit", "auto")
DEFAULT_SELECTION_MODE = "auto"
DATASET_SCAN_TIMEOUT_SECONDS = 4.0
DATASET_SCAN_CACHE_TTL_MINUTES = 30
DEFAULT_SNAPSHOT_MAX_AGE_MINUTES = 10
DEFAULT_STALE_AFTER_MINUTES = 60
SOURCE_WRITE_LEAD_SECONDS = 60
LIVE_ACCOUNT_WRITE_MINUTES = 15.0
RECENT_WRITE_LEAD_MINUTES = 10.0


def error_response(request_id: str, error: Exception) -> dict[str, Any]:
    if isinstance(error, VaultError):
        payload: dict[str, Any] = {"code": error.code, "message": str(error), "retryable": error.retryable}
        details = getattr(error, "details", None)
        if isinstance(details, dict) and details:
            payload["details"] = details
        return {"protocol_version": PROTOCOL_VERSION, "request_id": request_id, "ok": False, "error": payload}
    message = f"{type(error).__name__}: {error}" if os.environ.get("WECOM_CONTEXT_DEBUG") == "1" else "sidecar 操作失败"
    return {"protocol_version": PROTOCOL_VERSION, "request_id": request_id, "ok": False, "error": {"code": "INTERNAL_ERROR", "message": message, "retryable": False}}


def success_response(request_id: str, data: Any) -> dict[str, Any]:
    return {"protocol_version": PROTOCOL_VERSION, "request_id": request_id, "ok": True, "data": data}


def coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def make_client(config: dict[str, Any]) -> VaultClient:
    root = Path(str(config.get("vault_root") or DEFAULT_VAULT_ROOT)).expanduser()
    data_dir = Path(str(config["data_dir"])).expanduser() if config.get("data_dir") else None
    active_value = config.get("activeSnapshotPath") or config.get("snapshotPath")
    if active_value:
        snapshot_path = Path(str(active_value)).expanduser()
    elif config.get("snapshotMode") == "fixed":
        snapshot_path = root / "snapshots" / ".active-snapshot-required"
    else:
        snapshot_path = None
    validate_runtime_paths(vault_root=root, data_dir=data_dir, snapshot_path=snapshot_path)
    selected_dataset_id = config.get("selected_dataset_id")
    if data_dir is not None and isinstance(selected_dataset_id, str) and runtime_dataset_id(data_dir) != selected_dataset_id:
        raise VaultError("DATASET_NOT_FOUND", "当前数据集选择无效")
    return VaultClient(root=root, data_dir=data_dir, snapshot_path=snapshot_path)


def session_key(conversation_id: str) -> str:
    return hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:16]


def dataset_presentation(path: Path, enterprise_name: str | None = None) -> tuple[str, str]:
    parts = {part.lower() for part in path.parts}
    owner = next((segment for segment in path.parts if segment.isdigit() and len(segment) >= 15), None)
    kind = "backup" if "backup" in parts else "current" if path.name.lower() == "data" else "unknown"
    suffix = {"current": "当前数据", "backup": "备份数据"}.get(kind, "本机数据集")
    if enterprise_name:
        label = f"{enterprise_name}·{suffix}"
    else:
        enterprise = owner[-4:] if owner else ""
        label = f"企业…{enterprise}·{suffix}" if enterprise else suffix
    return kind, label


def dataset_activity(path: Path) -> float:
    """返回该数据集最近写入距今的分钟数（用于识别当前登录账号）。"""
    newest = 0.0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    newest = max(newest, item.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return float("inf")
    if newest <= 0:
        return float("inf")
    return max(0.0, (datetime.now().timestamp() - newest) / 60)


def current_account_for(datasets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按 current 数据最近写入时间识别当前登录企业微信账户；不确定时返回 None。"""
    ranked = sorted(
        [
            item
            for item in datasets
            if item.get("kind") == "current"
            and str(item.get("account_key") or item.get("account_id") or "")
            and isinstance(item.get("recent_write_minutes"), (int, float))
        ],
        key=lambda item: float(item["recent_write_minutes"]),
    )
    if not ranked:
        return None
    best = ranked[0]
    best_minutes = float(best["recent_write_minutes"])
    if best_minutes > LIVE_ACCOUNT_WRITE_MINUTES:
        return None
    if len(ranked) > 1 and float(ranked[1]["recent_write_minutes"]) - best_minutes < RECENT_WRITE_LEAD_MINUTES:
        return None
    account_key = str(best.get("account_key") or best.get("account_id") or "")
    return {
        "account_id": account_key,
        "account_name": str(best.get("account_name") or account_key),
        "dataset_id": str(best["dataset_id"]),
        "recent_write_minutes": round(best_minutes, 1),
        "confidence": "live",
    }


def account_summaries(
    datasets: list[dict[str, Any]],
    current_account_id: str | None,
    ignored_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    ignored = ignored_ids or set()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in datasets:
        account_key = str(item.get("account_key") or item.get("account_id") or "")
        if not account_key or str(item.get("dataset_id") or "") in ignored:
            continue
        grouped.setdefault(account_key, []).append(item)
    summaries: list[dict[str, Any]] = []
    for account_id, items in grouped.items():
        current = [item for item in items if item.get("kind") == "current"]
        preferred = min(current, key=lambda item: live_rank(item)) if current else items[0]
        summaries.append({
            "account_id": account_id,
            "account_name": str(preferred.get("account_name") or f"账户 {account_id}"),
            "dataset_id": str(preferred.get("dataset_id") or ""),
            "dataset_count": len(current) or len(items),
            "database_count": sum(int(item.get("database_count") or 0) for item in current or items),
            "key_available": any(item.get("key_available") is True for item in items),
            "current": account_id == current_account_id,
        })
    summaries.sort(key=lambda item: (not bool(item["current"]), str(item["account_name"])))
    return summaries


def collect_datasets() -> list[dict[str, Any]]:
    datasets: list[dict[str, Any]] = []
    for path in discover_dataset_paths():
        identifier = runtime_dataset_id(path)
        try:
            summary = inspect_dataset_path(path)
        except Exception:
            summary = {}
        company_name = enterprise_name_for(path)
        account_name = account_name_for(path)
        account_id = account_id_for(path) or ""
        account_key = account_name or account_id
        kind, label = dataset_presentation(path, company_name)
        formats = summary.get("formats") if isinstance(summary.get("formats"), dict) else {}
        wal_count = int(summary.get("wal_count") or 0)
        datasets.append(
            {
                "dataset_id": identifier,
                "account_id": account_id,
                "account_key": account_key,
                "account_name": account_name or "",
                "company_name": company_name or "",
                "kind": kind,
                "display_name": label,
                "database_count": int(summary.get("database_count") or 0),
                "encrypted_database_count": int(formats.get("wecom-wxsqlite3-aes128") or 0),
                "wal_count": wal_count,
                "key_available": key_validates_for(path),
                "recent_write_minutes": round(dataset_activity(path), 1) if kind == "current" else None,
            }
        )
    current = [item for item in datasets if item["kind"] == "current" and item.get("recent_write_minutes") is not None]
    if current:
        active = min(current, key=lambda item: item["recent_write_minutes"])
        for item in datasets:
            item["active"] = item["dataset_id"] == active["dataset_id"]
    return datasets


def discover_datasets_action(config_path: Path, config: dict[str, Any], include_backup: bool = False) -> dict[str, Any]:
    import threading

    config = dict(config)
    if config.get("ignored_datasets"):
        config["ignored_datasets"] = []
        save_config_atomic(config_path, config)
    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["datasets"] = collect_datasets()
        except Exception as error:  # noqa: BLE001 - 扫描失败走缓存降级
            outcome["error"] = str(error)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(DATASET_SCAN_TIMEOUT_SECONDS)
    deferred = thread.is_alive()
    datasets = outcome.get("datasets") if isinstance(outcome.get("datasets"), list) else None
    current_account: dict[str, Any] | None = None
    if datasets is None:
        cached = config.get("lastDatasetDiscovery")
        datasets = cached.get("datasets") if isinstance(cached, dict) and isinstance(cached.get("datasets"), list) else []
        current_account = cached.get("current_account") if isinstance(cached, dict) and isinstance(cached.get("current_account"), dict) else None
        if datasets:
            cached_active = cached.get("active_dataset_id") if isinstance(cached, dict) else None
            datasets = [{**item, "active": bool(cached_active) and item.get("dataset_id") == cached_active} for item in datasets]
        if not datasets:
            selected = config.get("data_dir")
            dataset_id_value = config.get("selected_dataset_id")
            if isinstance(selected, str) and selected and isinstance(dataset_id_value, str) and dataset_id_value:
                scan = cached_dataset_summary(config, dataset_id_value) or {}
                try:
                    kind, label = dataset_presentation(Path(selected), None)
                except Exception:
                    kind, label = "unknown", "当前数据集"
                datasets = [{
                    "dataset_id": dataset_id_value,
                    "account_id": account_id_for(Path(selected)) or "",
                    "company_name": "",
                    "kind": kind,
                    "display_name": label,
                    "database_count": int(scan.get("database_count") or 0),
                    "encrypted_database_count": int(scan.get("encrypted_database_count") or 0),
                    "wal_count": int(scan.get("wal_count") or 0),
                    "key_available": True,
                }]
        deferred = True
    else:
        current_account = current_account_for(datasets)
        updated = dict(config)
        updated["ignored_datasets"] = []
        updated["lastDatasetDiscovery"] = {
            "datasets": [item for item in datasets if item.get("kind") != "backup"],
            "active_dataset_id": next((item["dataset_id"] for item in datasets if item.get("active")), None),
            "current_account": current_account,
            "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        save_config_atomic(config_path, updated)
    datasets = [
        {
            **item,
            "account_id": str(item.get("account_id") or ""),
            "account_key": str(item.get("account_key") or item.get("account_name") or item.get("account_id") or ""),
            "account_name": str(item.get("account_name") or ""),
            "company_name": str(item.get("company_name") or ""),
        }
        for item in datasets
        if isinstance(item, dict)
    ]
    if isinstance(current_account, dict):
        current_account = {
            key: value for key, value in current_account.items() if key != "company_name"
        }
        current_account["account_id"] = str(current_account.get("account_id") or "")
        current_account["account_name"] = str(current_account.get("account_name") or "")
    if not include_backup:
        datasets = [item for item in datasets if item.get("kind") != "backup"]
    scanned = list(datasets)
    all_visible = scanned
    accounts = account_summaries(scanned, str(current_account.get("account_id")) if current_account else None)
    selected_id = str(config.get("selected_dataset_id") or "")
    selected_item = next((item for item in all_visible if str(item.get("dataset_id")) == selected_id), None)
    selected_account_id = str(selected_item.get("account_key") or selected_item.get("account_id") or "") if selected_item else ""
    selection_mode = str(config.get("selection_mode") or DEFAULT_SELECTION_MODE)
    scope_account_id = selected_account_id if selection_mode == "explicit" and selected_account_id else str(current_account.get("account_id") or "") if current_account else ""
    if scope_account_id:
        datasets = [item for item in all_visible if str(item.get("account_key") or item.get("account_id") or "") == scope_account_id]
    elif any(item.get("account_key") or item.get("account_id") for item in all_visible):
        datasets = []
    else:
        datasets = all_visible
    ignored = []
    selected_account = next((item for item in accounts if item.get("account_id") == scope_account_id), None)
    return {
        "count": len(datasets),
        "selected_dataset_id": config.get("selected_dataset_id"),
        "datasets": datasets,
        "ignored": ignored,
        "accounts": accounts,
        "current_account": current_account,
        "selected_account": selected_account,
        "scan_deferred": deferred,
    }


def _delete_snapshots_for(vault_root: Path, dataset_id_value: str) -> int:
    """删除该企业的本地快照目录（快照是应用侧副本，企业微信原始数据不动）。"""
    snapshots = vault_root / "snapshots"
    if not snapshots.is_dir():
        return 0
    removed = 0
    for candidate in sorted(snapshots.iterdir()):
        if not candidate.is_dir() or candidate.name.startswith("."):
            continue
        manifest_path = candidate / "manifest.json"
        owner = ""
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            owner = str(manifest.get("dataset_id") or "") if isinstance(manifest, dict) else ""
        except (OSError, json.JSONDecodeError):
            owner = ""
        if not owner and candidate.name.endswith(f"-{dataset_id_value}"):
            owner = dataset_id_value
        if owner != dataset_id_value:
            continue
        try:
            shutil.rmtree(candidate)
            removed += 1
        except OSError:
            continue
    return removed


def _discover_paths_bounded(timeout: float = DATASET_SCAN_TIMEOUT_SECONDS) -> list[Path] | None:
    """带超时的数据集路径枚举；超时返回 None（调用方据此采取保守策略）。"""
    import threading

    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["paths"] = list(discover_dataset_paths())
        except Exception:  # noqa: BLE001 - 扫描失败按不可判定处理
            outcome["error"] = True

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    paths = outcome.get("paths")
    return paths if isinstance(paths, list) else None


def _delete_keys_for(vault_root: Path, dataset_id_value: str) -> int:
    """删除该企业独占的密钥文件；同一把 key 还能解锁其它数据集时保留（同企业多目录共享）。"""
    private = vault_root / "private"
    if not private.is_dir():
        return 0
    discovered = _discover_paths_bounded()
    if discovered is None:
        # 无法确认是否还有其它数据集共用该密钥时保守保留，绝不误删。
        return 0
    others: list[Path] = []
    for path in discovered:
        try:
            if runtime_dataset_id(path) != dataset_id_value:
                others.append(path)
        except Exception:
            continue
    removed = 0
    candidates = sorted(private.glob("keys-*.json"))
    legacy = private / "keys.json"
    if legacy.is_file():
        candidates.append(legacy)
    for key_file in candidates:
        try:
            data = json.loads(key_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or str(data.get("dataset_id") or "") != dataset_id_value:
            continue
        try:
            raw_key = bytes.fromhex(str(data["global_key"]))
        except (KeyError, ValueError):
            continue
        if any(_key_unlocks(raw_key, other) for other in others):
            continue
        try:
            key_file.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _key_unlocks(raw_key: bytes, dataset: Path) -> bool:
    try:
        from wecom_common import verify_key
        from wecom_crypto import PAGE_SIZE
        message_db = next((candidate for candidate in sorted(dataset.rglob("message.db"))), None)
        if message_db is None or not message_db.is_file():
            return False
        with message_db.open("rb") as handle:
            page = handle.read(PAGE_SIZE)
        return bool(verify_key(raw_key, page))
    except Exception:
        return False


def remove_dataset_action(config_path: Path, config: dict[str, Any], dataset_id_value: str) -> dict[str, Any]:
    """从应用移除企业：删本地快照、密钥和绑定；不动企业微信数据，也不隐藏源数据。"""
    if not dataset_id_value:
        raise VaultError("INVALID_REQUEST", "缺少 dataset_id", False)
    vault_root = Path(str(config.get("vault_root") or DEFAULT_VAULT_ROOT)).expanduser()
    deleted_snapshots = _delete_snapshots_for(vault_root, dataset_id_value)
    deleted_keys = _delete_keys_for(vault_root, dataset_id_value)
    updated = clear_dataset_selection(migrate_config(config), dataset_id_value)
    updated["ignored_datasets"] = []
    save_config_atomic(config_path, updated)
    refreshed = discover_datasets_action(config_path, load_config(config_path), False)
    return {
        "removed": True,
        "dataset_id": dataset_id_value,
        "deleted_snapshots": deleted_snapshots,
        "deleted_keys": deleted_keys,
        **refreshed,
    }


def remove_account_action(config_path: Path, config: dict[str, Any], account_id_value: str) -> dict[str, Any]:
    """删除一个企业微信账户在应用侧的全部数据；不删除企业微信原始数据库。"""
    account_id_value = str(account_id_value or "").strip()
    if not account_id_value or len(account_id_value) > 128 or any(char in account_id_value for char in "/\0"):
        raise VaultError("INVALID_REQUEST", "企业微信账户标识无效", False)
    dataset_ids = set()
    for path in discover_dataset_paths():
        raw_id = account_id_for(path)
        display_name = account_name_for(path)
        if account_id_value in {raw_id, display_name}:
            dataset_ids.add(runtime_dataset_id(path))
    cached = config.get("lastDatasetDiscovery")
    cached_items = cached.get("datasets") if isinstance(cached, dict) else []
    if isinstance(cached_items, list):
        dataset_ids.update(
            str(item.get("dataset_id"))
            for item in cached_items
            if isinstance(item, dict)
            and str(item.get("account_key") or item.get("account_name") or item.get("account_id") or "") == account_id_value
            and item.get("dataset_id")
        )
        dataset_ids.update(
            str(item.get("dataset_id"))
            for item in cached_items
            if isinstance(item, dict)
            and str(item.get("account_id") or "") == account_id_value
            and item.get("dataset_id")
        )
    if not dataset_ids:
        raise VaultError("DATASET_NOT_FOUND", "找不到该企业微信账户的数据", False)
    vault_root = Path(str(config.get("vault_root") or DEFAULT_VAULT_ROOT)).expanduser()
    deleted_snapshots = sum(_delete_snapshots_for(vault_root, dataset_id_value) for dataset_id_value in dataset_ids)
    deleted_keys = sum(_delete_keys_for(vault_root, dataset_id_value) for dataset_id_value in dataset_ids)
    updated = migrate_config(config)
    for dataset_id_value in sorted(dataset_ids):
        updated = clear_dataset_selection(updated, dataset_id_value)
    updated["ignored_datasets"] = []
    updated.pop("lastDatasetDiscovery", None)
    save_config_atomic(config_path, updated)
    refreshed = discover_datasets_action(config_path, load_config(config_path), False)
    return {
        "removed": True,
        "account_id": account_id_value,
        "dataset_ids": sorted(dataset_ids),
        "deleted_datasets": len(dataset_ids),
        "deleted_snapshots": deleted_snapshots,
        "deleted_keys": deleted_keys,
        **refreshed,
    }

def restore_dataset_action(config_path: Path, config: dict[str, Any], dataset_id_value: str) -> dict[str, Any]:
    """兼容旧命令：清空历史隐藏标记并重新发现源数据。"""
    if not dataset_id_value:
        raise VaultError("INVALID_REQUEST", "缺少 dataset_id", False)
    updated = migrate_config(config)
    updated["ignored_datasets"] = []
    save_config_atomic(config_path, updated)
    refreshed = discover_datasets_action(config_path, load_config(config_path), False)
    return {"restored": True, "dataset_id": dataset_id_value, **refreshed}


def select_dataset_action(config_path: Path, config: dict[str, Any], dataset_id_value: str, *, selection_mode: str = "explicit") -> dict[str, Any]:
    """切换企业数据集。只保留目标企业自己的会话绑定，其余企业的绑定作废。"""
    if not dataset_id_value or "\0" in dataset_id_value:
        raise VaultError("INVALID_REQUEST", "数据集参数无效")
    paths = discover_dataset_paths()
    selected = next((path for path in paths if runtime_dataset_id(path) == dataset_id_value), None)
    if selected is None:
        raise VaultError("DATASET_NOT_FOUND", "找不到指定数据集")
    kind, label = dataset_presentation(selected, enterprise_name_for(selected))
    bindings = {
        key: binding
        for key, binding in session_bindings(config).items()
        if isinstance(binding, dict) and str(binding.get("dataset_id") or "") == dataset_id_value
    }
    updated = migrate_config(config)
    updated["configVersion"] = CONFIG_VERSION
    updated["data_dir"] = str(selected)
    updated["selected_dataset_id"] = dataset_id_value
    updated["selection_mode"] = selection_mode if selection_mode in SELECTION_MODES else "explicit"
    updated["snapshotMode"] = "fixed"
    for field in (
        "activeSnapshotPath",
        "activeSnapshotCreatedAt",
        "activeSnapshotDatasetId",
        "snapshotPath",
        *LEGACY_CONFIG_FIELDS,
    ):
        updated.pop(field, None)
    if bindings:
        updated["session_bindings"] = bindings
    else:
        updated.pop("session_bindings", None)
    selected_key = config.get("selected_session_key")
    if isinstance(selected_key, str) and any(str(binding.get("session_key") or "") == selected_key for binding in bindings.values()):
        updated["selected_session_key"] = selected_key
    else:
        updated.pop("selected_session_key", None)
    save_config_atomic(config_path, updated)
    return {"selected": True, "dataset_id": dataset_id_value, "kind": kind, "display_name": label}


def dataset_owner_id(config: dict[str, Any]) -> str:
    data_dir = str(config.get("data_dir") or "")
    for segment in reversed(Path(data_dir).parts):
        if segment.isdigit() and len(segment) >= 15:
            return segment
    return ""


def send_target_action(config: dict[str, Any], session_key_value: str) -> dict[str, Any]:
    """解析当前绑定会话对应的企业微信发送目标（不触发任何发送）。"""
    config = migrate_config(config)
    key = session_key_value or str(config.get("selected_session_key") or "")
    binding = find_binding(config, key)
    conversation_id = binding.get("conversation_id") if isinstance(binding, dict) else None
    if not isinstance(conversation_id, str) or not conversation_id:
        raise VaultError("SESSION_NOT_FOUND", "找不到会话，无法确定发送目标")
    owner = dataset_owner_id(config)
    kind = conversation_id[:1]
    if kind == "S":
        parts = [item for item in conversation_id[2:].split("_") if item]
        peer = next((item for item in parts if item != owner), parts[0] if parts else "")
        if not peer:
            raise VaultError("INVALID_REQUEST", "单聊会话缺少对端标识")
        return {"chat_id": peer, "chat_type": "single", "conversation_id": conversation_id}
    if kind != "R":
        raise VaultError("SEND_TARGET_UNSUPPORTED", "该会话类型不支持写回（仅支持单聊与群聊）", False)
    target = conversation_id[2:] if len(conversation_id) > 2 else conversation_id
    if not target:
        raise VaultError("INVALID_REQUEST", "会话标识无效")
    return {"chat_id": target, "chat_type": "group", "conversation_id": conversation_id}


def set_send_permission_action(config_path: Path, config: dict[str, Any], enabled: bool) -> dict[str, Any]:
    updated = migrate_config(config)
    updated["allowSendToWecom"] = bool(enabled)
    if not enabled:
        updated.pop("sendConfirmedAt", None)
    save_config_atomic(config_path, updated)
    return {"allow_send_to_wecom": bool(enabled)}


def send_message_action(
    config_path: Path,
    config: dict[str, Any],
    session_key_value: str,
    explicit_chat_id: str = "",
    explicit_chat_type: str = "",
) -> dict[str, Any]:
    """写回前的服务端校验（真正发送由 Connector 调官方 CLI 执行）。"""
    if not config.get("allowSendToWecom"):
        raise VaultError("SEND_DISABLED", "写回企业微信未启用，请在 App 设置中开启", False)
    if explicit_chat_id:
        if explicit_chat_type not in ("single", "group"):
            raise VaultError("SEND_TARGET_UNSUPPORTED", "发送目标类型无效", False)
        target = {"chat_id": explicit_chat_id, "chat_type": explicit_chat_type, "conversation_id": ""}
    else:
        target = send_target_action(config, session_key_value)
    updated = migrate_config(config)
    updated["sendConfirmedAt"] = utc_now()
    save_config_atomic(config_path, updated)
    return target


def snapshot_metadata(client: VaultClient) -> dict[str, Any]:
    snapshot = client.latest_snapshot()
    if snapshot is None:
        raise VaultError("SNAPSHOT_UNAVAILABLE", "没有可用明文快照，请先刷新快照", True)
    manifest_path = snapshot / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VaultError("SNAPSHOT_UNAVAILABLE", "快照清单不可读取，请重新刷新快照", True) from error
    if not isinstance(manifest, dict):
        raise VaultError("SNAPSHOT_UNAVAILABLE", "快照清单格式无效，请重新刷新快照", True)
    return {
        "dataset_id": str(manifest.get("dataset_id") or ""),
        "snapshot_id": snapshot.name,
        "created_at": str(manifest.get("created_at") or ""),
    }


def snapshot_age_minutes(created_at: str) -> float | None:
    try:
        moment = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds() / 60)


def read_snapshot_state(client: VaultClient, dataset_id_value: str) -> tuple[dict[str, Any] | None, str | None]:
    """读取活动快照状态；返回 (状态, 问题原因)，状态为空时问题原因说明为何不可用。"""
    snapshot = client.latest_snapshot()
    if snapshot is None:
        return None, "missing"
    try:
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "invalid"
    if not isinstance(manifest, dict):
        return None, "invalid"
    if str(manifest.get("dataset_id") or "") != dataset_id_value:
        return None, "mismatch"
    created_at = str(manifest.get("created_at") or "")
    age_minutes = snapshot_age_minutes(created_at)
    if age_minutes is None:
        return None, "invalid"
    return {
        "snapshot_id": snapshot.name,
        "created_at": created_at,
        "age_minutes": age_minutes,
        "path": str(snapshot),
    }, None


def format_message_time(value: Any) -> str | None:
    """把会话表里的 epoch 秒格式化为 `YYYY-MM-DD HH:MM`（契约要求字符串，0 表示无消息）。"""
    if isinstance(value, str):
        return value.strip() or None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%d %H:%M")


def build_session_mapping(client: VaultClient, limit: int = MAX_SESSION_SCAN) -> tuple[dict[str, str], list[dict[str, Any]]]:
    result = client.sessions(limit)
    mapping: dict[str, str] = {}
    sessions: list[dict[str, Any]] = []
    for item in result.get("sessions", []):
        conversation_id = str(item["conversation_id"])
        key = session_key(conversation_id)
        mapping[key] = conversation_id
        sessions.append({
            "session_key": key,
            "conversation_id": conversation_id,
            "display_name": str(item.get("display_name") or "未命名会话"),
            "kind": str(item.get("kind") or "其他"),
            "last_message_time": format_message_time(item.get("last_message_time")),
        })
    return mapping, sessions


def sessions_action(config_path: Path, config: dict[str, Any], client: VaultClient, limit: int, include_all: bool = False) -> dict[str, Any]:
    config = migrate_config(config)
    metadata = snapshot_metadata(client)
    _, sessions = build_session_mapping(client, MAX_SESSION_SCAN)
    if not include_all:
        sessions = [item for item in sessions if item["kind"] in CONVERSATIONAL_KINDS]
    selected = config.get("selected_session_key")
    for item in sessions:
        item["selected"] = item["session_key"] == selected
    return {
        "count": len(sessions[:limit]),
        "sessions": sessions[:limit],
        "dataset_id": metadata["dataset_id"],
        "snapshot_id": metadata["snapshot_id"],
        "snapshot_created_at": metadata["created_at"],
    }


def find_history_candidates(client: VaultClient, dataset_id: str, conversation_id: str, limit: int = MAX_HISTORY_CANDIDATES) -> list[dict[str, Any]]:
    """在同一企业的历史快照中查找该会话，返回可用于只读历史模式的快照候选。"""
    snapshots_dir = client.snapshots_dir
    if not snapshots_dir.is_dir():
        return []
    active = client.latest_snapshot()
    candidates: list[dict[str, Any]] = []
    for path in sorted((item for item in snapshots_dir.iterdir() if item.is_dir() and not item.is_symlink()), reverse=True):
        if active is not None and path.resolve() == active.resolve():
            continue
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict) or str(manifest.get("dataset_id") or "") != dataset_id:
            continue
        try:
            sessions = client.sessions_at_snapshot(path, limit=MAX_SESSION_SCAN).get("sessions", [])
        except Exception:
            continue
        if not any(str(item.get("conversation_id", "")) == conversation_id for item in sessions):
            continue
        candidates.append({
            "snapshot_id": path.name,
            "created_at": str(manifest.get("created_at") or ""),
        })
        if len(candidates) >= limit:
            break
    return candidates


def bind_session_payload(
    *,
    session_key_value: str,
    conversation_id: str,
    dataset_id: str,
    snapshot_id: str,
    mode: str,
    recovered: str,
    history_created_at: str | None = None,
) -> dict[str, Any]:
    return {
        "bound": True,
        "session_key": session_key_value,
        "conversation_id": conversation_id,
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "mode": mode,
        "recovered": recovered,
        "history_snapshot_id": snapshot_id if mode == "history" else None,
        "history_created_at": history_created_at if mode == "history" else None,
    }


def bind_session_action(
    config_path: Path,
    config: dict[str, Any],
    client: VaultClient,
    *,
    session_key_value: str = "",
    conversation_id_value: str | None = None,
    dataset_id_value: str | None = None,
    snapshot_id_value: str | None = None,
    allow_history: bool = False,
    select: bool = False,
) -> dict[str, Any]:
    """为单个会话建立绑定（默认不改动全局默认会话，多个 Agent 可并行绑定各自会话）。"""
    config = migrate_config(config)
    if not session_key_value and not conversation_id_value:
        raise VaultError("INVALID_REQUEST", "缺少会话标识")

    metadata = snapshot_metadata(client)
    if isinstance(dataset_id_value, str) and dataset_id_value and dataset_id_value != metadata["dataset_id"]:
        raise VaultError("DATASET_CHANGED", "企业数据集已切换，请重新加载会话列表", True)
    if isinstance(snapshot_id_value, str) and snapshot_id_value and snapshot_id_value != metadata["snapshot_id"]:
        raise VaultError("SNAPSHOT_CHANGED", "会话列表来自旧快照，正在为你重新加载", True)
    active_snapshot = client.latest_snapshot()
    if active_snapshot is None:
        raise VaultError("SNAPSHOT_UNAVAILABLE", "没有可用明文快照，请先刷新快照", True)

    fresh_mapping: dict[str, str] | None = None

    def fresh_sessions() -> dict[str, str]:
        nonlocal fresh_mapping
        if fresh_mapping is None:
            fresh_mapping, _ = build_session_mapping(client, MAX_SESSION_SCAN)
        return fresh_mapping

    recovered = "none"
    conversation_id: str | None = None
    existing = find_binding(config, session_key_value, metadata["dataset_id"])
    if isinstance(existing, dict) and isinstance(existing.get("conversation_id"), str) and existing["conversation_id"]:
        candidate = existing["conversation_id"]
        if candidate in set(fresh_sessions().values()):
            conversation_id = candidate
    if conversation_id is None:
        mapping = fresh_sessions()
        if session_key_value and session_key_value in mapping:
            conversation_id = mapping[session_key_value]
            recovered = "rebuilt_mapping"
        elif conversation_id_value and conversation_id_value in set(mapping.values()):
            conversation_id = conversation_id_value
            session_key_value = session_key(conversation_id)
            recovered = "from_conversation_id"

    if conversation_id is None:
        gone = VaultError("SESSION_GONE", "当前快照中不存在该会话，请重新选择会话", True)
        conversation = conversation_id_value or (existing.get("conversation_id") if isinstance(existing, dict) else "") or ""
        candidates = find_history_candidates(client, metadata["dataset_id"], conversation) if conversation else []
        gone.details = {"history_candidates": candidates}
        if allow_history and candidates and conversation:
            history_snapshot_id = str(candidates[0]["snapshot_id"])
            key = session_key(conversation)
            updated = set_binding(
                config,
                session_key_value=key,
                conversation_id=conversation,
                dataset_id=metadata["dataset_id"],
                snapshot_id=history_snapshot_id,
                snapshot_path=str(client.snapshots_dir / history_snapshot_id),
                mode="history",
            )
            if select:
                updated["selected_session_key"] = key
            save_config_atomic(config_path, updated)
            return bind_session_payload(
                session_key_value=key,
                conversation_id=conversation,
                dataset_id=metadata["dataset_id"],
                snapshot_id=history_snapshot_id,
                mode="history",
                recovered="history_snapshot",
                history_created_at=str(candidates[0].get("created_at") or ""),
            )
        raise gone

    if not isinstance(session_key_value, str) or not session_key_value:
        session_key_value = session_key(conversation_id)
    updated = set_binding(
        config,
        session_key_value=session_key_value,
        conversation_id=conversation_id,
        dataset_id=metadata["dataset_id"],
        snapshot_id=metadata["snapshot_id"],
        snapshot_path=str(active_snapshot),
        mode="active",
    )
    if select:
        updated["selected_session_key"] = session_key_value
    save_config_atomic(config_path, updated)
    return bind_session_payload(
        session_key_value=session_key_value,
        conversation_id=conversation_id,
        dataset_id=metadata["dataset_id"],
        snapshot_id=metadata["snapshot_id"],
        mode="active",
        recovered=recovered,
    )


def select_session_action(
    config_path: Path,
    config: dict[str, Any],
    client: VaultClient,
    session_key_value: str,
    conversation_id_value: str | None = None,
    dataset_id_value: str | None = None,
    snapshot_id_value: str | None = None,
    allow_history: bool = False,
) -> dict[str, Any]:
    """Pi TUI / 启动器脚本使用的全局默认会话绑定（内部复用 bind_session）。"""
    result = bind_session_action(
        config_path,
        config,
        client,
        session_key_value=session_key_value,
        conversation_id_value=conversation_id_value,
        dataset_id_value=dataset_id_value,
        snapshot_id_value=snapshot_id_value,
        allow_history=allow_history,
        select=True,
    )
    return {"selected": True, **result}


def clear_session_action(config_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    updated = migrate_config(config)
    updated.pop("selected_session_key", None)
    updated.pop("session_bindings", None)
    for field in LEGACY_CONFIG_FIELDS:
        updated.pop(field, None)
    save_config_atomic(config_path, updated)
    return {"selected": False}


def allow_session_action(config_path: Path, config: dict[str, Any], client: VaultClient, session_key_value: str) -> dict[str, Any]:
    """为启动器/Connector 兼容保留：写入一个 active 绑定，但不改变全局默认会话。"""
    config = migrate_config(config)
    if not isinstance(session_key_value, str) or not session_key_value:
        raise VaultError("INVALID_REQUEST", "缺少会话标识")
    existing = find_binding(config, session_key_value, config.get("selected_dataset_id"))
    if isinstance(existing, dict) and isinstance(existing.get("conversation_id"), str) and existing["conversation_id"]:
        return {"allowed": True}
    metadata = snapshot_metadata(client)
    mapping, _ = build_session_mapping(client, MAX_SESSION_SCAN)
    conversation_id = mapping.get(session_key_value)
    if not conversation_id:
        raise VaultError("SESSION_NOT_FOUND", "找不到会话")
    snapshot = client.latest_snapshot()
    if snapshot is None:
        raise VaultError("SNAPSHOT_UNAVAILABLE", "没有可用明文快照，请先刷新快照", True)
    updated = set_binding(
        config,
        session_key_value=session_key_value,
        conversation_id=conversation_id,
        dataset_id=metadata["dataset_id"],
        snapshot_id=metadata["snapshot_id"],
        snapshot_path=str(snapshot),
        mode="active",
    )
    save_config_atomic(config_path, updated)
    return {"allowed": True}


def resolve_binding_snapshot(
    config: dict[str, Any],
    session_key: str,
    dataset_id: str | None,
    snapshot_id: str | None,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """绑定与快照的唯一校验入口：read_context 与 prepare_context 共用，避免两套规则。

    返回 (binding, snapshot_dir, manifest)；任何不一致都抛结构化 VaultError。
    """
    if not isinstance(session_key, str) or not session_key:
        raise VaultError("INVALID_REQUEST", "缺少会话标识")
    exact_dataset = dataset_id if isinstance(dataset_id, str) and dataset_id else None
    binding = find_binding(config, session_key, exact_dataset) or find_binding(config, session_key)
    if not isinstance(binding, dict):
        raise VaultError("SESSION_NOT_ALLOWED", "该会话尚未绑定，请先在会话列表中选择会话")
    binding_dataset = str(binding.get("dataset_id") or "")
    if exact_dataset and exact_dataset != binding_dataset:
        raise VaultError("DATASET_CHANGED", "Agent 绑定的企业已切换，请重新启动该会话的 Agent", True)
    binding_snapshot_id = str(binding.get("snapshot_id") or "")
    if isinstance(snapshot_id, str) and snapshot_id and snapshot_id != binding_snapshot_id:
        raise VaultError("SNAPSHOT_CHANGED", "Agent 绑定的快照已更新，请重新启动该会话的 Agent", True)
    snapshot_value = binding.get("snapshot_path")
    if not isinstance(snapshot_value, str) or not snapshot_value:
        raise VaultError("SNAPSHOT_UNAVAILABLE", "会话绑定的快照不可用，请重新刷新快照", True)
    snapshot = Path(snapshot_value).expanduser()
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise VaultError("SNAPSHOT_UNAVAILABLE", "会话绑定的快照不可用，请重新刷新快照", True)
    try:
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VaultError("SNAPSHOT_UNAVAILABLE", "快照清单不可读取，请重新刷新快照", True) from error
    if not isinstance(manifest, dict) or str(manifest.get("dataset_id") or "") != binding_dataset:
        raise VaultError("SNAPSHOT_INVALID", "会话绑定的快照与数据集不一致")
    return binding, snapshot, manifest


def resolve_explicit_source_snapshot(
    client: VaultClient,
    session_key_value: str,
    dataset_id_value: str,
    snapshot_id_value: str,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """Resolve a one-shot source without creating a persistent session binding.

    The App sends an explicit dataset/snapshot/session identity for a selected
    source.  That identity is enough to read the immutable snapshot, while the
    normal Connector path still requires ``session_bindings``.
    """
    if not dataset_id_value or not snapshot_id_value:
        raise VaultError("SESSION_NOT_ALLOWED", "该会话尚未绑定，请先在会话列表中选择会话")
    snapshot = client.latest_snapshot()
    if snapshot is None:
        raise VaultError("SNAPSHOT_UNAVAILABLE", "没有可用明文快照，请先刷新快照", True)
    mode = "active"
    if snapshot.name != snapshot_id_value:
        candidate = client.snapshots_dir / snapshot_id_value
        if not candidate.is_dir() or candidate.is_symlink():
            raise VaultError("SNAPSHOT_CHANGED", "资料来源快照不可用，请刷新快照后重新选择", True)
        snapshot = candidate
        mode = "history"
    try:
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VaultError("SNAPSHOT_UNAVAILABLE", "资料来源快照不可读取，请重新刷新快照", True) from error
    if not isinstance(manifest, dict) or str(manifest.get("dataset_id") or "") != dataset_id_value:
        raise VaultError("DATASET_CHANGED", "资料来源企业已切换，请重新加载会话列表", True)
    try:
        sessions = client.sessions_at_snapshot(snapshot, limit=MAX_SESSION_SCAN).get("sessions", [])
    except Exception as error:  # noqa: BLE001 - a source read must fail explicitly
        raise VaultError("SESSION_GONE", "所选会话不在该快照中，请刷新快照后重新选择", True) from error
    for item in sessions:
        conversation_id = str(item.get("conversation_id") or "")
        if conversation_id and session_key(conversation_id) == session_key_value:
            return (
                {
                    "session_key": session_key_value,
                    "conversation_id": conversation_id,
                    "dataset_id": dataset_id_value,
                    "snapshot_id": snapshot_id_value,
                    "snapshot_path": str(snapshot),
                    "mode": mode,
                    "session_name": str(item.get("display_name") or ""),
                },
                snapshot,
                manifest,
            )
    raise VaultError("SESSION_GONE", "所选会话不在该快照中，请刷新快照后重新选择", True)


def read_context_action(config_path: Path, config: dict[str, Any], client: VaultClient, request: dict[str, Any]) -> dict[str, Any]:
    """按显式身份读取上下文：只认 session_bindings，不回退全局 selected_session_key，不修改配置。"""
    config = migrate_config(config)
    key = request.get("session_key")
    binding, snapshot, manifest = resolve_binding_snapshot(
        config,
        key,
        request.get("dataset_id"),
        request.get("snapshot_id"),
    )
    key = str(key)
    binding_dataset = str(binding.get("dataset_id") or "")
    binding_snapshot_id = str(binding.get("snapshot_id") or "")
    conversation_id = binding.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise VaultError("SESSION_NOT_FOUND", "找不到会话")
    mode = str(binding.get("mode") or "active")
    if mode == "history":
        try:
            sessions = client.sessions_at_snapshot(snapshot, limit=MAX_SESSION_SCAN).get("sessions", [])
        except Exception:
            sessions = []
        if not any(session_key(str(item.get("conversation_id", ""))) == key for item in sessions):
            raise VaultError("SESSION_GONE", "只读历史快照中已不存在该会话，请重新选择", True)
    limit = coerce_int(request.get("limit"), coerce_int(config.get("max_messages", 30), 30, 1, 500), 1, 500)
    history = client.history_at_snapshot(snapshot, conversation_id, limit, request.get("start"), request.get("end"))
    created_at = str(manifest.get("created_at") or "")
    age_minutes = snapshot_age_minutes(created_at) or 0.0
    session = history.get("session") or {}
    name = str(session.get("display_name") or "当前会话")
    max_tokens = coerce_int(request.get("max_context_tokens"), coerce_int(config.get("max_context_tokens", 2500), 2500, 100, 60000), 100, 60000)
    max_message_characters = coerce_int(request.get("max_message_characters"), coerce_int(config.get("max_message_characters", 2000), 2000, 50, 8000), 50, 8000)
    stale_after_minutes = coerce_int(config.get("snapshot_max_age_minutes"), DEFAULT_STALE_AFTER_MINUTES, 1, 10080)
    content, details = render_context(
        name,
        created_at,
        age_minutes,
        history.get("messages", []),
        max_tokens=max_tokens,
        max_message_characters=max_message_characters,
        stale_after_minutes=stale_after_minutes,
    )
    if mode == "history":
        details = {**details, "history_snapshot_id": binding_snapshot_id or snapshot.name, "read_only_history": True}
    return {"content": content, "details": details, "session_key": key}


def prepare_context_action(
    config_path: Path,
    config: dict[str, Any],
    client: VaultClient,
    request: dict[str, Any],
    *,
    image_roots: list[Path] | None = None,
    packages_root: Path | None = None,
) -> dict[str, Any]:
    """按显式来源集合生成本轮不可变图文资料包。

    - 每个来源单独校验绑定与快照归属，任何一个不合法就整体失败（不做部分注入）。
    - 图片复制进资料包目录并记录 sha256：预览与实际发送是同一份内容。
    - 多来源共享一个 token 预算，按顺序分配（前一个来源用不完的额度留给后面的）。
    """
    config = migrate_config(config)
    try:
        plan = normalise_request(request)
    except ValueError as error:
        raise VaultError("INVALID_REQUEST", str(error)) from error

    root = packages_root or (client.root / "packages")
    identifier = package_id()
    directory = package_files(root, identifier)
    directory.mkdir(parents=True, exist_ok=True)

    image_state: dict[str, Any] = {
        "count": 0,
        "omitted": 0,
        "bytes": 0,
        "max_images": plan["max_images"],
        "max_total_bytes": plan["max_total_bytes"],
        "max_edge_pixels": plan["max_edge_pixels"],
        "max_tiles_per_image": DEFAULT_MAX_TILES_PER_IMAGE,
        "package_dir": directory,
    }
    sources: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    blocks: list[str] = []
    remaining = plan["max_context_tokens"]
    stale_after_minutes = coerce_int(config.get("snapshot_max_age_minutes"), DEFAULT_STALE_AFTER_MINUTES, 1, 10080)
    include_images_requested = any(bool(item.get("include_images")) for item in plan["sources"])
    resolved_image_roots = (cache_roots() if image_roots is None else image_roots) if include_images_requested else []
    image_directory_index = _cache_directory_index(resolved_image_roots) if include_images_requested else {}
    downloaded_index = downloaded_image_index() if include_images_requested else {}
    try:
        for index, source_request in enumerate(plan["sources"], start=1):
            if source_request["dataset_id"] and source_request["snapshot_id"]:
                binding, snapshot, manifest = resolve_explicit_source_snapshot(
                    client,
                    source_request["session_key"],
                    source_request["dataset_id"],
                    source_request["snapshot_id"],
                )
            else:
                binding, snapshot, manifest = resolve_binding_snapshot(
                    config,
                    source_request["session_key"],
                    source_request["dataset_id"] or None,
                    source_request["snapshot_id"] or None,
                )
            conversation_id = str(binding.get("conversation_id") or "")
            if not conversation_id:
                raise VaultError("SESSION_NOT_FOUND", "找不到会话")
            history = client.history_at_snapshot(
                snapshot,
                conversation_id,
                source_request["limit"],
                source_request.get("start"),
                source_request.get("end"),
            )
            budget_left = max(remaining // max(len(plan["sources"]) - index + 1, 1), 100)
            built = build_source(
                source_index=index,
                source_request=source_request,
                binding={
                    **binding,
                    "session_name": str((history.get("session") or {}).get("display_name") or ""),
                    "kind": str((history.get("session") or {}).get("kind") or "单聊"),
                },
                snapshot=snapshot,
                manifest=manifest,
                messages=list(history.get("messages") or []),
                token_budget=budget_left,
                max_message_characters=plan["max_message_characters"],
                stale_after_minutes=stale_after_minutes,
                image_state=image_state,
                roots=resolved_image_roots,
                directory_index=image_directory_index,
                fallback_index=downloaded_index,
            )
            sources.append(built["source"])
            messages.extend(built["messages"])
            images.extend(built["images"])
            warnings.extend(built["warnings"])
            blocks.append(built["text"])
            remaining = max(remaining - int(built["source"]["estimated_tokens"]), 0)
    except Exception:
        # 生成失败不留半成品目录：否则界面可能拿到不完整的资料包。
        shutil.rmtree(directory, ignore_errors=True)
        raise

    payload = {
        "package_id": identifier,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "dir": str(directory),
        "text": compose_text(blocks),
        "sources": sources,
        "messages": messages,
        "images": images,
        "warnings": warnings,
        "stats": package_stats(sources, messages, images),
    }
    write_manifest(directory, payload)
    prune_packages(root)
    return payload

def _safe_download_component(value: str, fallback: str) -> str:
    cleaned = "".join(
        character if (character.isalnum() or character in " ._-") else "_"
        for character in str(value or "")
    ).strip(" .")
    return (cleaned[:80] or fallback).strip(" .") or fallback


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_images_action(
    config_path: Path,
    config: dict[str, Any],
    client: VaultClient,
    request: dict[str, Any],
    *,
    image_roots: list[Path] | None = None,
    downloads_root: Path | None = None,
) -> dict[str, Any]:
    """导出图片；可选地先让企业微信客户端补齐缺失缓存。"""
    image_roots = cache_roots() if image_roots is None else image_roots
    image_directory_index = _cache_directory_index(image_roots)
    dataset_id = str(request.get("dataset_id") or "")
    snapshot_id = str(request.get("snapshot_id") or "")
    session_key_value = str(request.get("session_key") or "")
    binding, snapshot, _manifest = resolve_explicit_source_snapshot(
        client,
        session_key_value,
        dataset_id,
        snapshot_id,
    )
    conversation_id = str(binding.get("conversation_id") or "")
    history = client.all_messages_at_snapshot(snapshot, conversation_id)
    messages = list(history.get("messages") or [])
    message_ids = [str(item.get("message_id") or "") for item in messages]
    keys_by_message = image_keys_for_messages(snapshot, message_ids)
    image_rows = [
        (message, image)
        for message in messages
        for image in keys_by_message.get(str(message.get("message_id") or ""), [])
    ]
    session_name = str(history.get("session", {}).get("display_name") or binding.get("session_name") or "")
    client_fetch_result: dict[str, Any] = {
        "attempted": False,
        "requested": 0,
        "fetched_keys": [],
        "missing": [],
        "error_code": None,
        "error": None,
    }
    if bool(request.get("fetch_via_client")):
        missing_targets: list[ClientFetchTarget] = []
        seen_keys: set[str] = set()
        for message, image in image_rows:
            key = str(image.get("key") or "")
            if not key or key in seen_keys:
                continue
            result = resolve_image(
                key,
                expected_size=image.get("size"),
                directory_index=image_directory_index,
                roots=image_roots,
            )
            if result.path is None:
                seen_keys.add(key)
                expected_size = image.get("size")
                missing_targets.append(
                    ClientFetchTarget(
                        key=key,
                        expected_size=expected_size if isinstance(expected_size, int) else None,
                        message_id=str(message.get("message_id") or ""),
                    )
                )
        if missing_targets:
            if str(config.get("selected_dataset_id") or "") != dataset_id:
                client_fetch_result = {
                    "attempted": False,
                    "requested": len(missing_targets),
                    "fetched_keys": [],
                    "missing": [
                        {
                            "key": target.key,
                            "message_id": target.message_id,
                            "expected_size": target.expected_size,
                            "status": "dataset_not_selected",
                        }
                        for target in missing_targets
                    ],
                    "error_code": "CLIENT_DATASET_NOT_SELECTED",
                    "error": "当前企业微信客户端未选中该企业数据集",
                }
            else:
                timeout_seconds = coerce_int(
                    request.get("client_fetch_timeout_seconds"),
                    30,
                    1,
                    90,
                )
                client_fetch_result = fetch_images_via_client(
                    session_name,
                    missing_targets,
                    timeout_seconds=float(timeout_seconds),
                )
            image_roots = cache_roots()
            image_directory_index = _cache_directory_index(image_roots)

    root = (downloads_root or (Path.home() / "Downloads")) / "WeCom Context" / _safe_download_component(dataset_id, "dataset")
    directory = root / _safe_download_component(session_name or session_key_value, session_key_value)
    directory.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    skipped_existing = 0
    omitted: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for index, (message, image) in enumerate(image_rows, start=1):
        result = resolve_image(
            str(image.get("key") or ""),
            expected_size=image.get("size"),
            directory_index=image_directory_index,
            roots=image_roots,
        )
        if result.path is None or result.status not in ("original", "thumbnail", "unsupported"):
            reason = result.reason or "图片不可用"
            if client_fetch_result.get("error"):
                reason = f"{reason}；{client_fetch_result['error']}"
            omitted.append(
                {
                    "message_id": str(message.get("message_id") or ""),
                    "time": str(message.get("time") or ""),
                    "reason": reason,
                }
            )
            continue
        suffix = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(result.mime_type or "", ".bin")
        base = f"image_{index:04d}{suffix}"
        target = directory / base
        candidate = 1
        while target.exists() or target.is_symlink():
            if target.is_file() and not target.is_symlink() and result.sha256 and _sha256_file(target) == result.sha256:
                skipped_existing += 1
                break
            candidate += 1
            target = directory / f"image_{index:04d}-{candidate}{suffix}"
        else:
            shutil.copyfile(result.path, target)
            downloaded += 1
        if target.is_file() and not target.is_symlink():
            files.append(
                {
                    "file": target.name,
                    "key": str(image.get("key") or ""),
                    "message_id": str(message.get("message_id") or ""),
                    "time": str(message.get("time") or ""),
                    "sender": str(message.get("sender") or ""),
                    "status": result.status,
                    "mime_type": result.mime_type,
                    "bytes": result.size_bytes,
                    "sha256": result.sha256,
                }
            )
    manifest = {
        "conversation_id": conversation_id,
        "session_key": session_key_value,
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "session_name": session_name,
        "total_images": len(image_rows),
        "downloaded": downloaded,
        "skipped_existing": skipped_existing,
        "omitted": omitted,
        "files": files,
        "client_fetch": client_fetch_result,
    }
    (directory / "download-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "directory": str(directory),
        "session_name": manifest["session_name"],
        "total_images": len(image_rows),
        "downloaded": downloaded,
        "skipped_existing": skipped_existing,
        "omitted_count": len(omitted),
        "omitted": omitted,
        "client_fetch_attempted": bool(client_fetch_result.get("attempted")),
        "client_fetch_attempts": int(client_fetch_result.get("attempts") or 0),
        "client_fetched": len(client_fetch_result.get("fetched_keys") or []),
        "client_fetch_missing": len(client_fetch_result.get("missing") or []),
        "client_fetch_error_code": client_fetch_result.get("error_code"),
        "client_fetch_error": client_fetch_result.get("error"),
    }


DATASET_SCAN_CACHE_TTL_MINUTES = 30


def scan_dataset_with_timeout(client: VaultClient, timeout_seconds: float = 4.0) -> dict[str, Any] | None:
    """带看门狗的容器扫描：超时返回 None，避免系统级文件访问阻塞拖死整个 status。"""
    import threading
    result: dict[str, Any] = {}

    def worker() -> None:
        try:
            result["value"] = client.status()
        except Exception as error:  # noqa: BLE001 - 扫描失败按不可用处理
            result["error"] = str(error)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        return None
    return result.get("value")


def cached_dataset_summary(config: dict[str, Any], dataset_id: str) -> dict[str, Any] | None:
    cache = config.get("lastDatasetScan")
    if not isinstance(cache, dict) or str(cache.get("dataset_id")) != dataset_id:
        return None
    return cache


def remember_dataset_summary(config_path: Path, config: dict[str, Any], dataset: dict[str, Any]) -> None:
    formats = dataset.get("formats") if isinstance(dataset.get("formats"), dict) else {}
    updated = dict(config)
    updated["lastDatasetScan"] = {
        "dataset_id": str(dataset.get("dataset_id") or ""),
        "database_count": int(dataset.get("database_count") or 0),
        "encrypted_database_count": int(formats.get("wecom-wxsqlite3-aes128") or 0),
        "wal_count": int(dataset.get("wal_count") or 0),
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_config_atomic(config_path, updated)


def status_action(config_path: Path, config: dict[str, Any], client: VaultClient) -> dict[str, Any]:
    live = scan_dataset_with_timeout(client)
    scan_deferred = live is None
    dataset = live if isinstance(live, dict) else {}
    if scan_deferred:
        dataset = cached_dataset_summary(config, str(config.get("selected_dataset_id") or "")) or {
            "dataset_id": str(config.get("selected_dataset_id") or ""),
            "database_count": 0,
            "formats": {},
        }
        if dataset.get("database_count"):
            dataset.setdefault("formats", {"wecom-wxsqlite3-aes128": int(dataset.get("encrypted_database_count") or 0)})
    elif scan_deferred is False:
        remember_dataset_summary(config_path, config, live)
    database_count = int(dataset.get("database_count") or 0)
    formats = dataset.get("formats") if isinstance(dataset.get("formats"), dict) else {}
    encrypted_count = int(formats.get("wecom-wxsqlite3-aes128") or dataset.get("encrypted_database_count") or 0)
    try:
        key = key_metadata(client.root)
    except VaultError:
        key = {"validated_database_count": 0}

    snapshot = client.latest_snapshot()
    created_at: str | None = None
    age_minutes: float | None = None
    if snapshot is not None:
        try:
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            created_at_value = manifest.get("created_at")
            if manifest.get("dataset_id") != dataset.get("dataset_id"):
                raise VaultError("SNAPSHOT_INVALID", "活动快照与当前数据集不一致")
            if isinstance(created_at_value, str):
                created_at = created_at_value
                age_minutes = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(created_at_value.replace("Z", "+00:00"))).total_seconds() / 60)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            snapshot = None

    selected_name: str | None = None
    selected_kind: str | None = None
    selected_key = config.get("selected_session_key")
    selected_binding = find_binding(config, selected_key) if isinstance(selected_key, str) else None
    if isinstance(selected_binding, dict):
        selected_kind = "单聊" if str(selected_binding.get("conversation_id") or "").startswith("S") else "群聊"
    if isinstance(selected_key, str) and snapshot is not None:
        try:
            for item in client.sessions_at_snapshot(snapshot, limit=MAX_SESSION_SCAN).get("sessions", []):
                if session_key(str(item.get("conversation_id", ""))) == selected_key:
                    selected_name = str(item.get("display_name") or "未命名会话")
                    selected_kind = str(item.get("kind") or "其他")
                    break
        except (VaultError, KeyError, TypeError):
            pass

    return {
        "dataset": {
            "available": database_count > 0,
            "database_count": database_count,
            "encrypted_database_count": encrypted_count,
            "scan_deferred": scan_deferred,
        },
        "key": {
            "available": bool(key.get("validated_database_count")),
            "validated_database_count": int(key.get("validated_database_count") or 0),
        },
        "allow_send_to_wecom": bool(config.get("allowSendToWecom")),
        "snapshot": {
            "available": snapshot is not None,
            "created_at": created_at,
            "age_minutes": age_minutes,
            "active": snapshot is not None and str(config.get("activeSnapshotPath") or "") == str(snapshot),
        },
        "selected_session": {
            "available": selected_name is not None,
            "display_name": selected_name,
            "kind": selected_kind,
        },
        "dataset_selection_required": False,
    }


def status_without_dataset(config_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    discovery = discover_datasets_action(config_path, config)
    return {
        "dataset": {
            "available": False,
            "database_count": 0,
            "encrypted_database_count": 0,
        },
        "key": {
            "available": False,
            "validated_database_count": 0,
        },
        "snapshot": {
            "available": False,
            "created_at": None,
            "age_minutes": None,
            "active": False,
        },
        "selected_session": {
            "available": False,
            "display_name": None,
            "kind": None,
        },
        "dataset_selection_required": discovery["count"] > 0,
    }


def refresh_action(config_path: Path, config: dict[str, Any], client: VaultClient, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        raise VaultError("REFRESH_CANCELLED", "刷新未确认")
    config = migrate_config(config)
    root = client.root
    before = preflight(root, client)
    with refresh_lock(root):
        created = client.decrypt()
        snapshot_path = Path(str(created.get("snapshot"))).expanduser().resolve()
        validated = validate_snapshot(snapshot_path, before["status"]["dataset_id"], client)
        dataset_id_value = str(before["status"]["dataset_id"])
        updated = activate_snapshot(
            config_path,
            config,
            snapshot_path=str(snapshot_path),
            created_at=str(validated["manifest"].get("created_at")),
            dataset_id=dataset_id_value,
        )
        try:
            refreshed_sessions = client.sessions_at_snapshot(snapshot_path, limit=MAX_SESSION_SCAN).get("sessions", [])
        except Exception:  # noqa: BLE001 - 会话清单不可读时按全部失效处理
            refreshed_sessions = []
        conversation_ids = {str(item.get("conversation_id", "")) for item in refreshed_sessions}
        preserved: list[str] = []
        invalidated: list[str] = []
        bindings: dict[str, Any] = {}
        for key, binding in session_bindings(config).items():
            if not isinstance(binding, dict):
                continue
            session_key_value = str(binding.get("session_key") or "")
            if not session_key_value:
                continue
            if str(binding.get("mode") or "active") == "history":
                bindings[key] = binding
                preserved.append(session_key_value)
                continue
            if str(binding.get("dataset_id") or "") != dataset_id_value or str(binding.get("conversation_id") or "") not in conversation_ids:
                invalidated.append(session_key_value)
                continue
            bindings[key] = {
                **binding,
                "snapshot_id": snapshot_path.name,
                "snapshot_path": str(snapshot_path),
                "bound_at": utc_now(),
            }
            preserved.append(session_key_value)
        if bindings:
            updated["session_bindings"] = bindings
        else:
            updated.pop("session_bindings", None)

        selected_key = config.get("selected_session_key")
        if isinstance(selected_key, str) and any(str(value.get("session_key") or "") == selected_key for value in bindings.values()):
            updated["selected_session_key"] = selected_key
            binding_state = "preserved"
        elif isinstance(selected_key, str):
            updated.pop("selected_session_key", None)
            binding_state = "cleared"
        else:
            updated.pop("selected_session_key", None)
            binding_state = "none"
        save_config_atomic(config_path, updated)
        return {
            "created": True,
            "decrypted_database_count": int(created.get("decrypted", 0)),
            "failed_database_count": int(created.get("not_decrypted", 0)),
            "active_snapshot_created_at": str(validated["manifest"].get("created_at")),
            "session_binding": binding_state,
            "active_config_fields": bool(updated.get("activeSnapshotPath")),
            "invalidated_session_keys": invalidated,
            "preserved_session_keys": preserved,
            "generation": {"dataset_id": dataset_id_value, "snapshot_id": snapshot_path.name},
        }


def live_rank(dataset: dict[str, Any]) -> float:
    minutes = dataset.get("recent_write_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return math.inf
    return float(minutes)


def clearly_leading(ranked: list[dict[str, Any]]) -> bool:
    """判断排名第一的候选是否「明显领先」（用于自动识别当前登录企业）。"""
    if not ranked:
        return False
    best = live_rank(ranked[0])
    if not math.isfinite(best) or best > LIVE_ACCOUNT_WRITE_MINUTES:
        return False
    if len(ranked) == 1:
        return True
    second = live_rank(ranked[1])
    if not math.isfinite(second):
        return True
    return second - best >= RECENT_WRITE_LEAD_MINUTES


def select_enterprise(config: dict[str, Any], datasets: list[dict[str, Any]], scan_deferred: bool) -> tuple[str | None, str, list[dict[str, str]]]:
    """决定 bootstrap 使用的企业数据集；返回 (dataset_id, selection_reason, warnings)。"""
    warnings: list[dict[str, str]] = []
    if scan_deferred:
        warnings.append({"code": "SCAN_DEFERRED", "message": "容器扫描超时，已使用缓存的数据集信息"})
    current = [item for item in datasets if item.get("kind") == "current"]
    if not current:
        return None, "none", warnings
    selected = config.get("selected_dataset_id")
    selection_mode = str(config.get("selection_mode") or DEFAULT_SELECTION_MODE)
    selected_item = next((item for item in current if item.get("dataset_id") == selected), None)
    ranked = sorted(current, key=live_rank)
    leader = next((item for item in ranked if item.get("key_available")), None)
    adoptable = leader if (not scan_deferred and leader is not None and clearly_leading(ranked) and ranked[0].get("dataset_id") == leader.get("dataset_id")) else None
    if selected_item is not None:
        if selection_mode == "explicit":
            return str(selected), "kept", warnings
        if adoptable is not None and adoptable.get("dataset_id") != selected:
            warnings.append({"code": "ENTERPRISE_ADOPTED", "message": f"检测到当前登录企业已切换到「{adoptable.get('display_name')}」，已自动采用"})
            return str(adoptable["dataset_id"]), "auto_adopted", warnings
        return str(selected), "kept", warnings
    if len(current) == 1:
        return str(current[0]["dataset_id"]), "single_dataset", warnings
    if adoptable is not None:
        warnings.append({"code": "ENTERPRISE_ADOPTED", "message": f"检测到当前登录企业「{adoptable.get('display_name')}」，已自动采用"})
        return str(adoptable["dataset_id"]), "auto_adopted", warnings
    warnings.append({"code": "DATASET_AMBIGUOUS", "message": "检测到多个当前数据集，无法确定当前登录企业，请先选择账户"})
    return None, "ambiguous", warnings


def bootstrap_payload(
    readiness: str,
    dataset: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    sessions: list[dict[str, Any]],
    selection_reason: str,
    warnings: list[dict[str, str]],
    *,
    current_account: dict[str, Any] | None = None,
    selected_account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "readiness": readiness,
        "dataset": dataset,
        "snapshot": snapshot,
        "sessions": sessions,
        "selection_reason": selection_reason,
        "warnings": warnings,
        "current_account": current_account,
        "selected_account": selected_account,
    }


def refresh_reason_for(config: dict[str, Any], snapshot: dict[str, Any] | None, problem: str | None, dataset: dict[str, Any] | None) -> str | None:
    max_age = coerce_int(config.get("snapshot_max_age_minutes"), DEFAULT_SNAPSHOT_MAX_AGE_MINUTES, 1, 10080)
    if snapshot is None:
        return problem or "missing"
    if snapshot["age_minutes"] > max_age:
        return "stale"
    source_minutes = dataset.get("recent_write_minutes") if isinstance(dataset, dict) else None
    if isinstance(source_minutes, (int, float)) and not isinstance(source_minutes, bool):
        if (snapshot["age_minutes"] - float(source_minutes)) * 60 > SOURCE_WRITE_LEAD_SECONDS:
            return "source_newer"
    return None


def bootstrap_sessions(client: VaultClient, dataset_id_value: str, snapshot_id: str) -> list[dict[str, Any]]:
    _, sessions = build_session_mapping(client, MAX_SESSION_SCAN)
    return [
        {**item, "dataset_id": dataset_id_value, "snapshot_id": snapshot_id}
        for item in sessions
        if item["kind"] in CONVERSATIONAL_KINDS
    ]


def bootstrap_action(config_path: Path, config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """一个进程内完成：发现数据集 → 决定企业 → 校验密钥 → 评估/刷新快照 → 构建会话列表。"""
    config = migrate_config(config)
    auto_refresh = bool(request.get("auto_refresh", True))
    discovery = discover_datasets_action(config_path, config, False)
    config = load_config(config_path)
    datasets = discovery.get("datasets") if isinstance(discovery.get("datasets"), list) else []
    scan_deferred = bool(discovery.get("scan_deferred"))
    current_account = discovery.get("current_account") if isinstance(discovery.get("current_account"), dict) else None
    selected_account = discovery.get("selected_account") if isinstance(discovery.get("selected_account"), dict) else None
    dataset_id_value, selection_reason, warnings = select_enterprise(config, datasets, scan_deferred)
    if not dataset_id_value:
        if not datasets:
            warnings.append({"code": "NO_DATASET", "message": "没有发现当前账户的企业微信数据集"})
        return bootstrap_payload("needs_dataset", None, None, [], selection_reason, warnings, current_account=current_account, selected_account=selected_account)

    if dataset_id_value != config.get("selected_dataset_id"):
        select_dataset_action(config_path, config, dataset_id_value, selection_mode="auto")
        config = load_config(config_path)
    dataset_item = next((item for item in datasets if item.get("dataset_id") == dataset_id_value), None)
    if dataset_item is None:
        dataset_item = {"dataset_id": dataset_id_value}
    dataset_payload = {
        "dataset_id": dataset_id_value,
        "account_id": str(dataset_item.get("account_id") or ""),
        "company_name": str(dataset_item.get("company_name") or ""),
        "display_name": str(dataset_item.get("display_name") or "当前数据集"),
        "kind": str(dataset_item.get("kind") or "unknown"),
        "key_available": bool(dataset_item.get("key_available")),
        "active": bool(dataset_item.get("active")),
    }
    if not dataset_payload["key_available"]:
        warnings.append({"code": "KEY_MISSING", "message": "该企业还没有本地密钥，需要先运行一次取钥脚本（密钥按企业绑定）"})
        return bootstrap_payload("needs_key", dataset_payload, None, [], selection_reason, warnings, current_account=current_account, selected_account=selected_account)

    try:
        client = make_client(config)
    except VaultError as error:
        warnings.append({"code": error.code, "message": str(error)})
        return bootstrap_payload("needs_dataset", None, None, [], selection_reason, warnings, current_account=current_account, selected_account=selected_account)

    snapshot, problem = read_snapshot_state(client, dataset_id_value)
    reason = refresh_reason_for(config, snapshot, problem, dataset_item)
    refreshed = False
    degraded = False
    if reason is not None and auto_refresh:
        try:
            refresh_action(config_path, config, client, True)
            config = load_config(config_path)
            client = make_client(config)
            snapshot, problem = read_snapshot_state(client, dataset_id_value)
            refreshed = snapshot is not None
        except Exception as error:  # noqa: BLE001 - 刷新失败时降级到旧快照
            warnings.append({"code": getattr(error, "code", "REFRESH_FAILED"), "message": str(error) or "快照刷新失败"})
            if snapshot is None:
                return bootstrap_payload("refresh_failed", dataset_payload, None, [], selection_reason, warnings, current_account=current_account, selected_account=selected_account)
            degraded = True
    elif reason is not None:
        warnings.append({"code": "REFRESH_SKIPPED", "message": "快照需要刷新，本次未自动刷新"})
        if snapshot is None:
            return bootstrap_payload("refresh_failed", dataset_payload, None, [], selection_reason, warnings, current_account=current_account, selected_account=selected_account)
        degraded = True

    if snapshot is None:
        return bootstrap_payload("refresh_failed", dataset_payload, None, [], selection_reason, warnings, current_account=current_account, selected_account=selected_account)
    try:
        sessions = bootstrap_sessions(client, dataset_id_value, str(snapshot["snapshot_id"]))
    except VaultError as error:
        warnings.append({"code": error.code, "message": str(error)})
        sessions = []
    snapshot_payload = {
        "snapshot_id": str(snapshot["snapshot_id"]),
        "created_at": str(snapshot["created_at"]),
        "age_minutes": round(float(snapshot["age_minutes"]), 3),
        "refreshed": refreshed,
        "degraded": degraded,
    }
    return bootstrap_payload("ready", dataset_payload, snapshot_payload, sessions, selection_reason, warnings, current_account=current_account, selected_account=selected_account)

def dispatch(request: dict[str, Any], config_path: Path) -> dict[str, Any]:
    request_id = str(request.get("request_id") or "sidecar-request")
    action = request.get("action")
    config = load_config(config_path)
    if action == "bootstrap":
        return success_response(request_id, bootstrap_action(config_path, config, request))
    if action == "discover_datasets":
        return success_response(request_id, discover_datasets_action(config_path, config, bool(request.get("include_backup"))))
    if action == "select_dataset":
        return success_response(request_id, select_dataset_action(config_path, config, str(request.get("dataset_id", ""))))
    if action == "remove_account":
        return success_response(request_id, remove_account_action(config_path, config, str(request.get("account_id", ""))))
    if action == "remove_dataset":
        return success_response(request_id, remove_dataset_action(config_path, config, str(request.get("dataset_id", ""))))
    if action == "restore_dataset":
        return success_response(request_id, restore_dataset_action(config_path, config, str(request.get("dataset_id", ""))))
    if action == "status" and not config.get("data_dir"):
        return success_response(request_id, status_without_dataset(config_path, config))
    client = make_client(config)
    if action == "status":
        return success_response(request_id, status_action(config_path, config, client))
    if action == "sessions":
        return success_response(request_id, sessions_action(config_path, config, client, int(request.get("limit", 50)), bool(request.get("include_all"))))
    if action == "send_target":
        return success_response(request_id, send_target_action(config, str(request.get("session_key", ""))))
    if action == "send_message":
        return success_response(
            request_id,
            send_message_action(
                config_path,
                config,
                str(request.get("session_key", "")),
                str(request.get("chat_id", "")),
                str(request.get("chat_type", "")),
            ),
        )
    if action == "set_send_permission":
        return success_response(request_id, set_send_permission_action(config_path, config, bool(request.get("enabled"))))
    if action == "allow_session":
        return success_response(request_id, allow_session_action(config_path, config, client, str(request.get("session_key", ""))))
    if action == "bind_session":
        return success_response(
            request_id,
            bind_session_action(
                config_path,
                config,
                client,
                session_key_value=str(request.get("session_key", "")),
                conversation_id_value=str(request.get("conversation_id", "")) or None,
                dataset_id_value=str(request.get("dataset_id", "")) or None,
                snapshot_id_value=str(request.get("snapshot_id", "")) or None,
                allow_history=bool(request.get("allow_history")),
            ),
        )
    if action == "select_session":
        return success_response(
            request_id,
            select_session_action(
                config_path,
                config,
                client,
                str(request.get("session_key", "")),
                str(request.get("conversation_id", "")) or None,
                str(request.get("dataset_id", "")) or None,
                str(request.get("snapshot_id", "")) or None,
                bool(request.get("allow_history")),
            ),
        )
    if action == "clear_session":
        return success_response(request_id, clear_session_action(config_path, config))
    if action == "read_context":
        return success_response(request_id, read_context_action(config_path, config, client, request))
    if action == "prepare_context":
        return success_response(request_id, prepare_context_action(config_path, config, client, request))
    if action == "download_images":
        return success_response(request_id, download_images_action(config_path, config, client, request))
    if action == "preview_context":
        result = read_context_action(config_path, config, client, request)
        return success_response(request_id, result["details"])
    if action == "capture_key":
        return success_response(request_id, capture_key_action(config_path, config, request))
    if action == "refresh_snapshot":
        return success_response(request_id, refresh_action(config_path, config, client, bool(request.get("confirmed"))))
    raise VaultError("INVALID_REQUEST", "未知 sidecar action")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    if config_path != CONFIG_PATH.resolve() and os.environ.get("WECOM_CONTEXT_DEV_MODE") != "1":
        raise SystemExit("config path must remain inside the App config location")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise VaultError("INVALID_REQUEST", "请求必须是对象")
            response = dispatch(request, config_path)
        except Exception as error:
            request_id = str(request.get("request_id") if isinstance(request, dict) else "sidecar-request")
            response = error_response(request_id, error)
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
