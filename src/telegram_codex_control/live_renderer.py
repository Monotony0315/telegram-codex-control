from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol


class _ExecutionEventLike(Protocol):
    event_type: str
    message: str | None
    status: str | None


Sender = Callable[[str, dict], Awaitable[dict]]


@dataclass(slots=True)
class TelegramLiveRenderer:
    chat_id: int
    sender: Sender
    now: Callable[[], float] = monotonic
    typing_interval_seconds: float = 4.0
    edit_interval_seconds: float = 0.75
    _message_id: int | None = field(default=None, init=False)
    _last_typing_at: float | None = field(default=None, init=False)
    _last_edit_at: float | None = field(default=None, init=False)
    _last_text: str = field(default="", init=False)

    async def start(self, initial_text: str) -> None:
        response = await self.sender(
            "sendMessage",
            {"chat_id": self.chat_id, "text": initial_text},
        )
        self._message_id = self._extract_message_id(response)
        self._last_text = initial_text
        self._last_edit_at = self.now()

    async def apply_event(self, event: _ExecutionEventLike) -> None:
        if self._message_id is None:
            return

        text: str | None = None
        if event.event_type == "status":
            status_value = (event.status or "").strip()
            message_value = (event.message or "").strip()
            if status_value and message_value:
                text = f"Status: {status_value}\n{message_value}"
            elif status_value:
                text = f"Status: {status_value}"
            elif message_value:
                text = message_value
        elif event.event_type in {"text_delta", "text_done", "log"}:
            text = (event.message or "").strip() or None

        if text:
            await self._edit_text_if_due(text)

    async def finalize(self, text: str) -> None:
        if self._message_id is None:
            return
        await self._edit_text(text)

    async def send_typing_if_due(self) -> None:
        now = self.now()
        if self._last_typing_at is None or (now - self._last_typing_at) >= self.typing_interval_seconds:
            await self.sender(
                "sendChatAction",
                {"chat_id": self.chat_id, "action": "typing"},
            )
            self._last_typing_at = now

    async def _edit_text_if_due(self, text: str) -> None:
        now = self.now()
        if self._last_edit_at is None or (now - self._last_edit_at) >= self.edit_interval_seconds:
            await self._edit_text(text)
            return
        if self.edit_interval_seconds <= 0:
            await self._edit_text(text)

    async def _edit_text(self, text: str) -> None:
        if self._message_id is None:
            return
        if text == self._last_text:
            return
        await self.sender(
            "editMessageText",
            {
                "chat_id": self.chat_id,
                "message_id": self._message_id,
                "text": text,
            },
        )
        self._last_text = text
        self._last_edit_at = self.now()

    @staticmethod
    def _extract_message_id(response: dict) -> int | None:
        if not isinstance(response, dict):
            return None
        message_id = response.get("message_id")
        if isinstance(message_id, int):
            return message_id
        result = response.get("result")
        if isinstance(result, dict):
            nested = result.get("message_id")
            if isinstance(nested, int):
                return nested
        return None
