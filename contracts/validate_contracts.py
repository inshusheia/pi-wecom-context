#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIRED_SCHEMAS = {
    "core-protocol.schema.json",
    "error-codes.json",
    "status.schema.json",
    "sessions.schema.json",
    "context-preview.schema.json",
    "refresh.schema.json",
    "read-context.schema.json",
    "app-config.schema.json",
    "datasets.schema.json",
    "select-dataset.schema.json",
    "bootstrap.schema.json",
    "bind-session.schema.json",
    "prepare-context.schema.json",
    "agent-status.schema.json",
    "memory.schema.json",
}


def main() -> int:
    files = {path.name for path in ROOT.glob("*.json")}
    missing = REQUIRED_SCHEMAS - files
    if missing:
        raise SystemExit(f"missing protocol artifacts: {sorted(missing)}")

    for name in REQUIRED_SCHEMAS:
        value = json.loads((ROOT / name).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise SystemExit(f"protocol artifact is not an object: {name}")
        if name.endswith(".schema.json"):
            if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
                raise SystemExit(f"invalid schema dialect: {name}")
            if not isinstance(value.get("$id"), str):
                raise SystemExit(f"missing schema id: {name}")

    errors = json.loads((ROOT / "error-codes.json").read_text(encoding="utf-8"))
    codes = errors.get("codes")
    if not isinstance(codes, dict) or not codes:
        raise SystemExit("error code registry is empty")
    if any(not isinstance(value, dict) or not isinstance(value.get("retryable"), bool) for value in codes.values()):
        raise SystemExit("error code registry has invalid retryable flags")

    # 同一份错误码登记表有三个镜像：JSON 注册表、TS 联合类型、Python Literal。
    # 任一镜像漂移都会让前端按 code 分支时静默落空，因此这里强制三者一致。
    registry = set(codes)
    ts_codes = _ts_error_codes((ROOT / "protocol.ts").read_text(encoding="utf-8"))
    py_codes = _py_error_codes((ROOT / "protocol_types.py").read_text(encoding="utf-8"))
    for label, mirror in (("protocol.ts", ts_codes), ("protocol_types.py", py_codes)):
        if mirror != registry:
            raise SystemExit(
                f"error code mirror drift in {label}: "
                f"missing={sorted(registry - mirror)} extra={sorted(mirror - registry)}"
            )

    print({"schemas": len(REQUIRED_SCHEMAS), "errorCodes": len(codes), "status": "ok"})
    return 0


def _ts_error_codes(source: str) -> set[str]:
    match = re.search(r"export type ProtocolErrorCode =([^;]*);", source, re.S)
    if match is None:
        raise SystemExit("protocol.ts has no ProtocolErrorCode union")
    return set(re.findall(r'"([A-Z][A-Z0-9_]*)"', match.group(1)))


def _py_error_codes(source: str) -> set[str]:
    match = re.search(r"ProtocolErrorCode = Literal\[(.*?)\]", source, re.S)
    if match is None:
        raise SystemExit("protocol_types.py has no ProtocolErrorCode literal")
    return set(re.findall(r'"([A-Z][A-Z0-9_]*)"', match.group(1)))


if __name__ == "__main__":
    raise SystemExit(main())
