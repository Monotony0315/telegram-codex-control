from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import re


_BOT_TOKEN_RE = re.compile(r"\b\d{8,11}:[A-Za-z0-9_-]{20,}\b")
_OPENAI_KEY_RE = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}\b")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b(\s*[:=]\s*)([^\s,;]+)"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("?(?:password|passwd|secret|token|api[_-]?key)"?\s*:\s*")([^"]+)(")'
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_text(text: str) -> str:
    if not text:
        return text
    redacted = _BOT_TOKEN_RE.sub("[REDACTED_BOT_TOKEN]", text)
    redacted = _OPENAI_KEY_RE.sub("[REDACTED_API_KEY]", redacted)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    redacted = _JSON_SECRET_RE.sub(r"\1[REDACTED]\3", redacted)
    redacted = _KV_SECRET_RE.sub(r"\1\2[REDACTED]", redacted)
    return redacted


def chunk_text(text: str, max_size: int = 3500) -> list[str]:
    if max_size <= 0:
        raise ValueError("max_size must be > 0")
    if not text:
        return []
    if len(text) <= max_size:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(line) > max_size:
            if current:
                chunks.append(current)
                current = ""
            start = 0
            while start < len(line):
                chunks.append(line[start : start + max_size])
                start += max_size
            continue

        if len(current) + len(line) <= max_size:
            current += line
        else:
            chunks.append(current)
            current = line

    if current:
        chunks.append(current)

    return chunks


def format_status(uptime_seconds: int, active_job: object | None) -> str:
    lines = ["health: ok", f"uptime_seconds: {uptime_seconds}"]
    if active_job is None:
        lines.append("active_job: none")
        return "\n".join(lines)

    def _value(job: object, key: str) -> object:
        if isinstance(job, Mapping):
            return job.get(key)
        return getattr(job, key, None)

    lines.append(f"active_job.id: {_value(active_job, 'id')}")
    lines.append(f"active_job.command: {_value(active_job, 'command')}")
    lines.append(f"active_job.status: {_value(active_job, 'status')}")
    return "\n".join(lines)
