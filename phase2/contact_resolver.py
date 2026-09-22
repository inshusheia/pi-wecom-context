#!/usr/bin/env python3
"""Resolve safe display names for WeCom sessions from a plaintext snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path


SESSION_PREFIXES = {
    "R": "群聊",
    "S": "单聊",
    "M": "微信联系人",
    "O": "应用/公众号",
    "Y": "系统会话",
}
SINGLE_ID_RE = re.compile(r"^S:(\d+(?:_\d+)*)$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise RuntimeError(f"missing snapshot database: {path.name}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM pragma_table_info(?)", (table,)
    ).fetchall()
    return {str(row[0]) for row in rows}


def has_table(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def clean_name(value: object) -> str:
    if value is None:
        return ""
    text = CONTROL_RE.sub(" ", str(value)).strip()
    text = " ".join(text.split())
    if not text or text.isdigit() or len(text) > 80:
        return text[:79] + "…" if len(text) > 80 else ""
    return text


def session_kind(conversation_id: str) -> str:
    return SESSION_PREFIXES.get(conversation_id[:1], "其他")


def conversation_participants(conversation_id: str) -> list[int]:
    match = SINGLE_ID_RE.fullmatch(conversation_id)
    if not match:
        return []
    return list(dict.fromkeys(int(value) for value in match.group(1).split("_")))


def stable_alias(conversation_id: str) -> str:
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:4].upper()
    return f"未识别单聊 #{digest}"


def load_user_names(snapshot: Path) -> tuple[dict[int, str], dict[int, str]]:
    users: dict[int, str] = {}
    remarks: dict[int, str] = {}
    path = snapshot / "user.db"
    if not path.is_file():
        return users, remarks
    with connect_readonly(path) as connection:
        if has_table(connection, "user_table"):
            columns = table_columns(connection, "user_table")
            wanted = [name for name in ("id", "name", "real_name", "account", "external_corp_name") if name in columns]
            if "id" in wanted:
                for row in connection.execute(f'SELECT {",".join(wanted)} FROM "user_table"'):
                    record = dict(zip(wanted, row))
                    try:
                        user_id = int(record["id"])
                    except (TypeError, ValueError):
                        continue
                    name = clean_name(record.get("real_name") or record.get("name") or record.get("account"))
                    corp = clean_name(record.get("external_corp_name"))
                    if name and corp and corp not in name:
                        name = f"{name}（{corp}）"
                    if name:
                        users[user_id] = name
        if has_table(connection, "external_user_relation_v3"):
            columns = table_columns(connection, "external_user_relation_v3")
            wanted = [name for name in ("user_id", "remarks", "real_remarks", "corp_remark") if name in columns]
            if "user_id" in wanted:
                for row in connection.execute(f'SELECT {",".join(wanted)} FROM "external_user_relation_v3"'):
                    record = dict(zip(wanted, row))
                    try:
                        user_id = int(record["user_id"])
                    except (TypeError, ValueError):
                        continue
                    name = clean_name(record.get("real_remarks") or record.get("remarks") or record.get("corp_remark"))
                    if name:
                        remarks[user_id] = name
    return users, remarks


def load_session_rows(snapshot: Path) -> list[dict]:
    path = snapshot / "session.db"
    rows: list[dict] = []
    with connect_readonly(path) as connection:
        if not has_table(connection, "conversation_table"):
            return rows
        columns = table_columns(connection, "conversation_table")
        wanted = [name for name in ("id", "name", "roomname_remark", "last_message_time", "last_message_id") if name in columns]
        if "id" not in wanted:
            return rows
        for row in connection.execute(f'SELECT {",".join(wanted)} FROM "conversation_table"'):
            record = dict(zip(wanted, row))
            conversation_id = str(record.get("id") or "")
            if not conversation_id:
                continue
            rows.append({
                "conversation_id": conversation_id,
                "raw_name": clean_name(record.get("roomname_remark") or record.get("name")),
                "kind": session_kind(conversation_id),
                "last_message_time": int(record.get("last_message_time") or 0),
            })
    return rows


def load_member_names(snapshot: Path) -> dict[str, dict[int, str]]:
    path = snapshot / "session.db"
    mapping: dict[str, dict[int, str]] = {}
    with connect_readonly(path) as connection:
        if not has_table(connection, "conversation_user_table"):
            return mapping
        columns = table_columns(connection, "conversation_user_table")
        if not {"conversation_id", "user_id", "nick_name"} <= columns:
            return mapping
        for conversation_id, user_id, nickname in connection.execute(
            'SELECT conversation_id,user_id,nick_name FROM "conversation_user_table"'
        ):
            name = clean_name(nickname)
            if name:
                mapping.setdefault(str(conversation_id), {})[int(user_id)] = name
    return mapping


def infer_self_id(rows: list[dict], users: dict[int, str], remarks: dict[int, str]) -> int | None:
    participants_by_session = [set(conversation_participants(row["conversation_id"])) for row in rows]
    participants_by_session = [ids for ids in participants_by_session if ids]
    if len(participants_by_session) < 2:
        return None
    frequency = Counter(user_id for ids in participants_by_session for user_id in ids)
    if not frequency:
        return None
    max_frequency = max(frequency.values())
    candidates = [user_id for user_id, count in frequency.items() if count == max_frequency]
    required_frequency = max(2, (len(participants_by_session) * 3 + 3) // 4)
    if len(candidates) != 1 or max_frequency < required_frequency:
        return None
    candidate = candidates[0]
    if candidate not in users and candidate not in remarks:
        return None
    return candidate


def resolve_single_name(
    row: dict,
    self_id: int | None,
    users: dict[int, str],
    remarks: dict[int, str],
    member_names: dict[str, dict[int, str]],
) -> str:
    conversation_id = row["conversation_id"]
    if row["raw_name"] and row["raw_name"] != conversation_id:
        return row["raw_name"]
    participants = conversation_participants(conversation_id)
    if self_id is not None:
        participants = [user_id for user_id in participants if user_id != self_id]
    names: list[str] = []
    for user_id in participants:
        name = remarks.get(user_id) or users.get(user_id) or member_names.get(conversation_id, {}).get(user_id, "")
        name = clean_name(name)
        if name and name not in names:
            names.append(name)
    if names:
        return "、".join(names)
    return stable_alias(conversation_id)


def resolve_sessions(snapshot: Path, limit: int) -> dict:
    rows = load_session_rows(snapshot)
    users, remarks = load_user_names(snapshot)
    member_names = load_member_names(snapshot)
    self_id = infer_self_id(rows, users, remarks)
    sessions = []
    for row in rows:
        if row["kind"] == "单聊":
            display_name = resolve_single_name(row, self_id, users, remarks, member_names)
        else:
            display_name = row["raw_name"] or row["kind"]
        sessions.append({
            "conversation_id": row["conversation_id"],
            "display_name": display_name,
            "kind": row["kind"],
            "last_message_time": row["last_message_time"],
        })
    sessions.sort(key=lambda item: (item["last_message_time"], item["conversation_id"]), reverse=True)
    return {"count": min(len(sessions), limit), "sessions": sessions[:limit]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve safe WeCom session display names")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    snapshot = Path(args.snapshot).expanduser().resolve()
    if not snapshot.is_dir():
        raise SystemExit("snapshot directory does not exist")
    if args.limit < 1 or args.limit > 100:
        raise SystemExit("limit must be between 1 and 100")
    print(json.dumps(resolve_sessions(snapshot, args.limit), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
