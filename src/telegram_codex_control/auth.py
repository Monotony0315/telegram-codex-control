from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Authorizer:
    allowed_user_id: int
    allowed_chat_id: int

    def is_authorized(self, user_id: int | None, chat_id: int | None) -> bool:
        return user_id == self.allowed_user_id and chat_id == self.allowed_chat_id


def extract_message_identity(update: dict) -> tuple[int | None, int | None]:
    """Extract `user_id` and `chat_id` from a Telegram update."""
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    from_user = message.get("from") or {}

    user_id = from_user.get("id")
    chat_id = chat.get("id")

    if isinstance(user_id, bool) or not isinstance(user_id, int):
        user_id = None
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        chat_id = None

    return user_id, chat_id
