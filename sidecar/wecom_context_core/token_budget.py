from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .redaction import sanitize_untrusted_text

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")


@dataclass(frozen=True)
class BudgetResult:
    messages: list[dict]
    estimated_tokens: int
    truncated: bool


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for char in text if CJK_RE.fullmatch(char))
    other = len(text) - cjk
    return math.ceil((cjk + math.ceil(other / 4)) * 1.1)


def message_text(message: dict) -> str:
    return f"[{message.get('time', '')}] {message.get('sender', '')}\n{message.get('content', '')}"


def fit_messages(messages: list[dict], *, max_tokens: int, max_message_characters: int, prefix: str, suffix: str) -> BudgetResult:
    normalized = [
        {
            "time": sanitize_untrusted_text(str(message.get("time", "")), 80),
            "sender": sanitize_untrusted_text(str(message.get("sender", "")), 160),
            "content": sanitize_untrusted_text(str(message.get("content", "")), max_message_characters),
        }
        for message in messages
    ]
    truncated = any(
        len(item["content"]) < len(str(original.get("content", "")))
        for item, original in zip(normalized, messages)
    )
    selected: list[dict] = []
    for candidate in reversed(normalized):
        body = "\n\n".join([message_text(candidate), *[message_text(item) for item in selected]])
        if estimate_tokens(f"{prefix}\n{body}\n{suffix}") <= max_tokens:
            selected.append(candidate)
            continue
        truncated = True
        break
    if len(selected) < len(normalized):
        truncated = True
    selected.reverse()
    body = "\n\n".join(message_text(item) for item in selected)
    return BudgetResult(selected, estimate_tokens(f"{prefix}\n{body}\n{suffix}"), truncated)
