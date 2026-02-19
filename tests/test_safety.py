from __future__ import annotations

import string

from telegram_codex_control.safety import (
    SafetyManager,
    requires_confirmation,
    run_prompt_requires_autopilot_confirmation,
)
from telegram_codex_control.store import Store


def test_requires_confirmation() -> None:
    assert requires_confirmation("autopilot") is True
    assert requires_confirmation("run") is True
    assert requires_confirmation("codex") is True


def test_run_prompt_requires_autopilot_confirmation() -> None:
    assert run_prompt_requires_autopilot_confirmation("please do $autopilot build x") is True
    assert run_prompt_requires_autopilot_confirmation("autopilot this task") is True
    assert run_prompt_requires_autopilot_confirmation("build me a dashboard") is True
    assert run_prompt_requires_autopilot_confirmation("I want an API service") is True
    assert run_prompt_requires_autopilot_confirmation("regular prompt without trigger") is False


def test_safety_confirmation_flow(store: Store) -> None:
    safety = SafetyManager(store, confirmation_ttl_seconds=120)
    request = safety.request_autopilot_confirmation(task="deploy", user_id=1, chat_id=2)
    assert request.nonce
    assert len(request.nonce) >= 32
    assert all(ch in string.hexdigits for ch in request.nonce)

    peeked = safety.get_confirmation(nonce=request.nonce, user_id=1, chat_id=2)
    assert peeked is not None
    assert peeked.task == "deploy"

    consumed = safety.consume_confirmation(nonce=request.nonce, user_id=1, chat_id=2)
    assert consumed is not None
    assert consumed.task == "deploy"

    assert safety.consume_confirmation(nonce=request.nonce, user_id=1, chat_id=2) is None


def test_codex_confirmation_flow(store: Store) -> None:
    safety = SafetyManager(store, confirmation_ttl_seconds=120)
    request = safety.request_codex_confirmation(task="--help", user_id=10, chat_id=20)
    fetched = safety.get_confirmation(nonce=request.nonce, user_id=10, chat_id=20)
    assert fetched is not None
    assert fetched.command == "codex"
