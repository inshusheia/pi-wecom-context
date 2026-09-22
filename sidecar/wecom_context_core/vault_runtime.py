from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any




class VaultRuntimeError(RuntimeError):
    pass


def _runtime_directory() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidate = Path(str(frozen_root)) / "vault_runtime"
        if (candidate / "vault_cli.py").is_file():
            return candidate
    project_root = Path(__file__).resolve().parents[2]
    return project_root / "third_party" / "yichen-skills" / "yichen-wecom-local-vault" / "scripts"


KEY_MATCH = "match"
KEY_NO_MESSAGE_DB = "no_message_db"
KEY_READ_FAILED = "read_failed"
KEY_NO_KEYS = "no_keys"
KEY_NO_MATCH = "no_match"


def load_matching_key_detailed(dataset: Path, private_dir: Path | None = None) -> tuple[str, tuple[bytes, dict[str, Any]] | None]:
    """区分失败形态的密钥匹配：match / no_message_db / read_failed / no_keys / no_match。

    read_failed 表示 message.db 第一页读取被阻断（sandboxd/TCC 授权 pending 等暂态），可重试；
    no_match 才是「密钥与企业不匹配」。no_keys 是「该企业还没取过钥」。
    """
    if not dataset.is_dir():
        return KEY_NO_MESSAGE_DB, None
    message_db = next((candidate for candidate in sorted(dataset.rglob("message.db"))), None)
    if message_db is None or not message_db.is_file():
        return KEY_NO_MESSAGE_DB, None
    private = private_dir if private_dir is not None else (
        Path.home() / "Library" / "Application Support" / "wecom-local-vault" / "private"
    )
    key_files = sorted(private.glob("keys-*.json")) if private.is_dir() else []
    legacy = private / "keys.json"
    if legacy.is_file():
        key_files.append(legacy)
    if not key_files:
        return KEY_NO_KEYS, None
    try:
        runtime_text = str(_runtime_directory())
        if runtime_text not in sys.path:
            sys.path.insert(0, runtime_text)
        from wecom_common import verify_key
        from wecom_crypto import PAGE_SIZE
        with message_db.open("rb") as handle:
            page = handle.read(PAGE_SIZE)
    except OSError:
        return KEY_READ_FAILED, None
    for key_file in key_files:
        try:
            data = json.loads(key_file.read_text(encoding="utf-8"))
            raw_key = bytes.fromhex(str(data["global_key"]))
        except Exception:
            continue
        try:
            if verify_key(raw_key, page):
                return KEY_MATCH, (raw_key, data)
        except Exception:
            continue
    return KEY_NO_MATCH, None


def load_matching_key(dataset: Path) -> tuple[bytes, dict[str, Any]] | None:
    """遍历 private/ 下全部密钥文件，返回第一个能验证该数据集的 (raw_key, key_data)。"""
    outcome, matched = load_matching_key_detailed(dataset)
    return matched if outcome == KEY_MATCH else None


def key_validates_for(dataset: Path) -> bool:
    return load_matching_key(dataset) is not None


_CORP_NAME_CACHE: dict[str, str | None] = {}


def _pb_self_corp(blob: bytes) -> tuple[int, str] | None:
    """解析 self_corp_info protobuf 顶层字段：返回 (登录 userid, 企业名)。"""
    pos = 0
    userid = 0
    name = ""

    def varint(data: bytes, at: int) -> tuple[int, int]:
        value = 0
        shift = 0
        while at < len(data):
            byte = data[at]
            at += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value, at
        raise ValueError("truncated varint")

    try:
        while pos < len(blob):
            key, pos = varint(blob, pos)
            field, wire = key >> 3, key & 7
            if wire == 0:
                value, pos = varint(blob, pos)
                if field == 2:
                    userid = value
            elif wire == 2:
                length, pos = varint(blob, pos)
                value = blob[pos:pos + length]
                pos += length
                if field == 3:
                    try:
                        name = value.decode("utf-8")
                    except UnicodeDecodeError:
                        name = ""
            elif wire == 5:
                pos += 4
            elif wire == 1:
                pos += 8
            else:
                break
    except (ValueError, IndexError):
        return None
    return (userid, name) if userid and name else None


def account_id_for(dataset: Path) -> str | None:
    """返回企业微信本地账户 userid；账户目录是 15 位以上纯数字。"""
    return next((segment for segment in dataset.parts if segment.isdigit() and len(segment) >= 15), None)


def enterprise_name_for(dataset: Path) -> str | None:
    """返回数据集所属企业名称；无密钥或解析失败时返回 None。"""
    owner = account_id_for(dataset)
    if owner is None:
        return None
    cache_key = f"{owner}:{dataset}"
    if cache_key in _CORP_NAME_CACHE:
        return _CORP_NAME_CACHE[cache_key]
    name: str | None = None
    company_db = next((candidate for candidate in sorted(dataset.rglob("company.db"))), None)
    if company_db is not None and company_db.is_file():
        try:
            import sqlite3
            import tempfile
            from wecom_common import load_key_file
            from wecom_crypto import PAGE_SIZE, database_format, decrypt_database
            with company_db.open("rb") as handle:
                fmt = database_format(handle.read(PAGE_SIZE))
            source = company_db
            cleanup: tempfile.TemporaryDirectory | None = None
            if fmt != "sqlite3":
                matched = load_matching_key(dataset)
                if matched is None:
                    _CORP_NAME_CACHE[cache_key] = None
                    return None
                raw_key = matched[0]
                cleanup = tempfile.TemporaryDirectory(prefix="wecom-corp-")
                source = Path(cleanup.name) / "company.db"
                decrypt_database(company_db, source, raw_key)
            connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            rows = connection.execute("SELECT self_corp_info FROM self_corp_list_table").fetchall()
            connection.close()
            if cleanup is not None:
                cleanup.cleanup()
            for (blob,) in rows:
                decoded = _pb_self_corp(bytes(blob)) if blob else None
                if decoded and str(decoded[0]) == owner and decoded[1]:
                    name = decoded[1]
                    break
        except Exception:
            name = None
    _CORP_NAME_CACHE[cache_key] = name
    return name

_ACCOUNT_NAME_CACHE: dict[str, str | None] = {}


def account_name_for(dataset: Path) -> str | None:
    """解析企业微信登录账号的联系人姓名；与公司名解析分开。"""
    owner = account_id_for(dataset)
    if owner is None:
        return None
    cache_key = f"{owner}:{dataset}"
    if cache_key in _ACCOUNT_NAME_CACHE:
        return _ACCOUNT_NAME_CACHE[cache_key]
    name: str | None = None
    cleanup: Any = None
    user_db = next((candidate for candidate in sorted(dataset.rglob("user.db"))), None)
    if user_db is None or not user_db.is_file():
        _ACCOUNT_NAME_CACHE[cache_key] = None
        return None
    try:
        runtime_text = str(_runtime_directory())
        if runtime_text not in sys.path:
            sys.path.insert(0, runtime_text)
        from wecom_crypto import PAGE_SIZE, database_format, decrypt_database
        with user_db.open("rb") as handle:
            fmt = database_format(handle.read(PAGE_SIZE))
        source = user_db
        if fmt != "sqlite3":
            matched = load_matching_key(dataset)
            if matched is None:
                _ACCOUNT_NAME_CACHE[cache_key] = None
                return None
            import tempfile
            cleanup = tempfile.TemporaryDirectory(prefix="wecom-account-")
            source = Path(cleanup.name) / "user.db"
            decrypt_database(user_db, source, matched[0])
        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(user_table)")}
        if "id" in columns:
            wanted = [column for column in ("real_name", "name", "account") if column in columns]
            if wanted:
                row = connection.execute(
                    f'SELECT {",".join(wanted)} FROM "user_table" WHERE id=? LIMIT 1',
                    (int(owner),),
                ).fetchone()
                if row:
                    for value in row:
                        candidate = " ".join(str(value or "").split()).strip()
                        if candidate and not candidate.isdigit() and len(candidate) <= 80:
                            name = candidate
                            break
        connection.close()
    except Exception:
        name = None
    finally:
        if cleanup is not None:
            cleanup.cleanup()
    _ACCOUNT_NAME_CACHE[cache_key] = name
    return name

def _vault_cli():
    runtime_directory = _runtime_directory()
    if not (runtime_directory / "vault_cli.py").is_file():
        raise VaultRuntimeError("Vault 运行时资源不存在")
    runtime_text = str(runtime_directory)
    if runtime_text not in sys.path:
        sys.path.insert(0, runtime_text)
    import vault_cli  # type: ignore[import-not-found]

    def connect_readonly(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    vault_cli.connect = connect_readonly
    return vault_cli
def discover_dataset_paths(data_dir: Path | None = None) -> list[Path]:
    vault_cli = _vault_cli()
    explicit = str(data_dir) if data_dir is not None else None
    return [Path(path).expanduser().resolve() for path in vault_cli.discover_datasets(explicit)]


def dataset_id(path: Path) -> str:
    vault_cli = _vault_cli()
    return str(vault_cli.dataset_id(path))


def inspect_dataset_path(path: Path) -> dict[str, Any]:
    vault_cli = _vault_cli()
    return vault_cli.inspect_dataset(path)


def choose_dataset(data_dir: Path | None) -> Path:
    vault_cli = _vault_cli()
    try:
        return vault_cli.choose_dataset(str(data_dir) if data_dir is not None else None)
    except SystemExit as error:
        raise VaultRuntimeError(str(error)) from error


def inspect(data_dir: Path | None) -> dict[str, Any]:
    vault_cli = _vault_cli()
    return vault_cli.inspect_dataset(choose_dataset(data_dir))


def sessions(snapshot: Path, limit: int) -> dict[str, Any]:
    from .contacts import resolve_sessions

    return resolve_sessions(snapshot, max(1, min(limit, 100)))


def history(snapshot: Path, conversation_id: str, limit: int, start: str | None, end: str | None) -> dict[str, Any]:
    vault_cli = _vault_cli()
    try:
        start_value = vault_cli.parse_time(start)
        end_value = vault_cli.parse_time(end)
        messages = vault_cli.iter_messages(snapshot, conversation_id, start_value, end_value, None, max(1, min(limit, 100)))
    except SystemExit as error:
        raise VaultRuntimeError(str(error)) from error
    from .contacts import resolve_sessions

    session = next((item for item in resolve_sessions(snapshot, 100)["sessions"] if item["conversation_id"] == conversation_id), None)
    if session is None:
        raise VaultRuntimeError("找不到当前会话")
    return {"session": session, "count": len(messages), "messages": messages}
def all_messages(snapshot: Path, conversation_id: str) -> dict[str, Any]:
    """读取一个会话的全部消息，供本地图片导出使用。"""
    vault_cli = _vault_cli()
    try:
        messages = vault_cli.iter_messages(snapshot, conversation_id, None, None, None, None)
    except SystemExit as error:
        raise VaultRuntimeError(str(error)) from error
    from .contacts import resolve_sessions

    session = next((item for item in resolve_sessions(snapshot, 100)["sessions"] if item["conversation_id"] == conversation_id), None)
    if session is None:
        raise VaultRuntimeError("找不到当前会话")
    return {"session": session, "count": len(messages), "messages": messages}


def _load_keys(vault_root: Path) -> dict[str, Any]:
    private = vault_root / "private"
    candidates = sorted(private.glob("keys-*.json")) if private.is_dir() else []
    legacy = private / "keys.json"
    key_path = candidates[-1] if candidates else legacy
    if not key_path.is_file():
        raise VaultRuntimeError("没有可用的验证密钥")
    if key_path.stat().st_mode & 0o077:
        raise VaultRuntimeError("验证密钥权限不安全")
    try:
        value = json.loads(key_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VaultRuntimeError("验证密钥文件格式无效") from error
    if not isinstance(value, dict):
        raise VaultRuntimeError("验证密钥文件格式无效")
    return value


def decrypt(vault_root: Path, data_dir: Path | None) -> dict[str, Any]:
    vault_cli = _vault_cli()
    dataset = choose_dataset(data_dir)
    matched = load_matching_key(dataset)
    keys = matched[1] if matched else _load_keys(vault_root)
    snapshots = vault_root / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    destination = snapshots / f"{stamp}-{vault_cli.dataset_id(dataset)}"
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    results: list[dict[str, Any]] = []
    for relative, source in vault_cli.iter_databases(dataset):
        with source.open("rb") as handle:
            kind = vault_cli.database_format(handle.read(vault_cli.PAGE_SIZE))
        key = vault_cli.key_for_database(keys, relative)
        if kind == "wecom-wxsqlite3-aes128" and key is None:
            results.append({"database": str(relative), "status": "skipped", "reason": "missing key"})
            continue
        try:
            details = vault_cli.decrypt_database(source, destination / relative, key or bytes(16), apply_wal=True)
            results.append({"database": str(relative), "status": "ok", **details})
        except Exception as error:
            results.append({"database": str(relative), "status": "failed", "reason": str(error)})
    manifest = {
        "version": 1,
        "created_at": vault_cli.utc_now(),
        "dataset_id": vault_cli.dataset_id(dataset),
        "contains_plaintext_wecom_data": True,
        "wal_merge_enabled": True,
        "results": results,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    failed = [item for item in results if item["status"] != "ok"]
    return {
        "snapshot": str(destination),
        "decrypted": len(results) - len(failed),
        "not_decrypted": len(failed),
        "manifest": str(manifest_path),
    }
