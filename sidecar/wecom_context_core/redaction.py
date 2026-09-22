from __future__ import annotations

import re

EMAIL_PATTERN = re.compile(r"\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+\b", re.IGNORECASE)
LABELLED_SECRET_PATTERN = re.compile(
    r"((?:api[\s_-]*key|access[\s_-]*token|refresh[\s_-]*token|client[\s_-]*secret|authorization|bearer|password|passwd|secret|private[\s_-]*key|密码|验证码)\s*[:：=]\s*)[^\s,;，；]+",
    re.IGNORECASE,
)
PREFIXED_SECRET_PATTERN = re.compile(r"\b(?:sk|pk|gh[pousr]|xox[baprs]-|AIza|AKIA|ASIA|npm_|pypi-)[A-Z0-9._~+/=-]{10,}\b", re.IGNORECASE)
HIGH_ENTROPY_TOKEN_PATTERN = re.compile(
    r"\b(?=[A-Z0-9._~+/=-]{20,}\b)(?=[A-Z0-9._~+/=-]*[A-Z])(?=[A-Z0-9._~+/=-]*[a-z])(?=[A-Z0-9._~+/=-]*\d)[A-Z0-9][A-Z0-9._~+/=-]{19,}\b"
)


def redact_sensitive_data(value: str) -> str:
    value = EMAIL_PATTERN.sub("[email redacted]", value)
    value = LABELLED_SECRET_PATTERN.sub(r"\1[secret redacted]", value)
    value = PREFIXED_SECRET_PATTERN.sub("[secret redacted]", value)
    return HIGH_ENTROPY_TOKEN_PATTERN.sub("[secret redacted]", value)


def redact_sensitive_data_with_counts(value: str) -> tuple[str, dict[str, int]]:
    counts = {"email": 0, "credential": 0}

    def replace_email(match: re.Match[str]) -> str:
        counts["email"] += 1
        return "[email redacted]"

    def replace_credential(match: re.Match[str]) -> str:
        counts["credential"] += 1
        return match.group(1) + "[secret redacted]"

    value = EMAIL_PATTERN.sub(replace_email, value)
    value = LABELLED_SECRET_PATTERN.sub(replace_credential, value)

    def replace_secret(match: re.Match[str]) -> str:
        counts["credential"] += 1
        return "[secret redacted]"

    value = PREFIXED_SECRET_PATTERN.sub(replace_secret, value)
    return HIGH_ENTROPY_TOKEN_PATTERN.sub(replace_secret, value), counts


def sanitize_untrusted_text_with_counts(value: str, maximum_characters: int | None = None) -> tuple[str, dict[str, int]]:
    control_count = len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", value))
    normalized = value.replace("\x00", "")
    normalized = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", lambda m: f"\\x{ord(m.group(0)):02x}", normalized)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized, counts = redact_sensitive_data_with_counts(normalized)
    normalized = normalized.replace("--- BEGIN WECOM HISTORY ---", "--- BEGIN WECOM HISTORY (data text) ---")
    normalized = normalized.replace("--- END WECOM HISTORY ---", "--- END WECOM HISTORY (data text) ---")
    if maximum_characters is not None and len(normalized) > maximum_characters:
        if maximum_characters <= 0:
            return "", {"email": counts["email"], "credential": counts["credential"], "control": control_count}
        normalized = normalized[: max(0, maximum_characters - 1)] + "…"
    return normalized, {"email": counts["email"], "credential": counts["credential"], "control": control_count}


def sanitize_untrusted_text(value: str, maximum_characters: int | None = None) -> str:
    return sanitize_untrusted_text_with_counts(value, maximum_characters)[0]
