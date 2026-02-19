from __future__ import annotations

from telegram_codex_control.utils import chunk_text, redact_text


def test_redact_text_masks_tokens() -> None:
    raw = (
        "token=abc123 secret:hello "
        "sk-abcdefghijklmnopqrstuvwxyz12345 "
        "sk-proj-abcdefghijklmnopqrstuvwxyz_12345"
    )
    redacted = redact_text(raw)
    assert "abc123" not in redacted
    assert "hello" not in redacted
    assert "sk-abcdefghijklmnopqrstuvwxyz12345" not in redacted
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz_12345" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_masks_json_style_secrets() -> None:
    raw = '{"token": "abc123", "api_key": "secretvalue", "ok": "visible"}'
    redacted = redact_text(raw)
    assert "abc123" not in redacted
    assert "secretvalue" not in redacted
    assert '"ok": "visible"' in redacted


def test_chunk_text_respects_size_limit() -> None:
    source = "x" * 8100
    chunks = chunk_text(source, max_size=3500)
    assert len(chunks) == 3
    assert all(len(chunk) <= 3500 for chunk in chunks)
    assert "".join(chunks) == source
