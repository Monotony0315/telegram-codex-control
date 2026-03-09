from __future__ import annotations

import asyncio

from telegram_codex_control.live_events import ExecutionEvent
from telegram_codex_control.live_renderer import TelegramLiveRenderer


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value


def _run(coro):
    return asyncio.run(coro)


def test_live_renderer_creates_placeholder_and_edits_message() -> None:
    calls: list[tuple[str, dict]] = []
    clock = _Clock()

    async def sender(method: str, payload: dict) -> dict:
        calls.append((method, dict(payload)))
        if method == "sendMessage":
            return {"message_id": 10}
        return {"ok": True}

    renderer = TelegramLiveRenderer(
        chat_id=2222,
        sender=sender,
        now=clock.now,
        typing_interval_seconds=4.0,
        edit_interval_seconds=0.0,
    )

    async def scenario() -> None:
        await renderer.start("Thinking...")
        await renderer.apply_event(ExecutionEvent(event_type="status", status="running", message="Planning"))
        await renderer.apply_event(ExecutionEvent(event_type="text_delta", message="Hello"))
        await renderer.finalize("Hello world")

    _run(scenario())

    assert calls[0] == ("sendMessage", {"chat_id": 2222, "text": "Thinking..."})
    assert ("editMessageText", {"chat_id": 2222, "message_id": 10, "text": "Status: running\nPlanning"}) in calls
    assert calls[-1] == ("editMessageText", {"chat_id": 2222, "message_id": 10, "text": "Hello world"})


def test_live_renderer_sends_typing_heartbeat_when_due() -> None:
    calls: list[tuple[str, dict]] = []
    clock = _Clock()

    async def sender(method: str, payload: dict) -> dict:
        calls.append((method, dict(payload)))
        if method == "sendMessage":
            return {"message_id": 15}
        return {"ok": True}

    renderer = TelegramLiveRenderer(
        chat_id=2222,
        sender=sender,
        now=clock.now,
        typing_interval_seconds=4.0,
        edit_interval_seconds=0.0,
    )

    async def scenario() -> None:
        await renderer.start("Working...")
        await renderer.send_typing_if_due()
        clock.value = 5.0
        await renderer.send_typing_if_due()

    _run(scenario())

    assert ("sendChatAction", {"chat_id": 2222, "action": "typing"}) in calls
