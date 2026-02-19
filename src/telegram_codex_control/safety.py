from __future__ import annotations

from dataclasses import dataclass
import re
import secrets

from .store import Confirmation, Store

_AUTOPILOT_TRIGGER_RE = re.compile(
    r"(?i)(\$autopilot|\bautopilot\b|\bauto pilot\b|\bautonomous\b|"
    r"\bbuild me\b|\bcreate me\b|\bmake me\b|\bfull auto\b|"
    r"\bhandle it all\b|\bi want a\b|\bi want an\b)"
)


def requires_confirmation(command: str) -> bool:
    return command.strip().lower() in {"autopilot", "run", "codex"}


def run_prompt_requires_autopilot_confirmation(prompt: str) -> bool:
    return bool(_AUTOPILOT_TRIGGER_RE.search(prompt))


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    nonce: str
    task: str
    expires_at: str


class SafetyManager:
    def __init__(self, store: Store, confirmation_ttl_seconds: int = 300):
        self.store = store
        self.confirmation_ttl_seconds = confirmation_ttl_seconds

    def request_autopilot_confirmation(
        self,
        *,
        task: str,
        user_id: int,
        chat_id: int,
    ) -> ConfirmationRequest:
        return self.request_confirmation(
            command="autopilot",
            task=task,
            user_id=user_id,
            chat_id=chat_id,
        )

    def request_run_confirmation(
        self,
        *,
        task: str,
        user_id: int,
        chat_id: int,
    ) -> ConfirmationRequest:
        return self.request_confirmation(
            command="run",
            task=task,
            user_id=user_id,
            chat_id=chat_id,
        )

    def request_codex_confirmation(
        self,
        *,
        task: str,
        user_id: int,
        chat_id: int,
    ) -> ConfirmationRequest:
        return self.request_confirmation(
            command="codex",
            task=task,
            user_id=user_id,
            chat_id=chat_id,
        )

    def request_confirmation(
        self,
        *,
        command: str,
        task: str,
        user_id: int,
        chat_id: int,
    ) -> ConfirmationRequest:
        nonce = secrets.token_hex(16)
        confirmation = self.store.create_confirmation(
            nonce=nonce,
            command=command,
            task=task,
            user_id=user_id,
            chat_id=chat_id,
            ttl_seconds=self.confirmation_ttl_seconds,
        )
        return ConfirmationRequest(
            nonce=confirmation.nonce,
            task=confirmation.task,
            expires_at=confirmation.expires_at,
        )

    def get_confirmation(self, *, nonce: str, user_id: int, chat_id: int) -> Confirmation | None:
        return self.store.get_confirmation(nonce, user_id=user_id, chat_id=chat_id)

    def consume_confirmation(self, *, nonce: str, user_id: int, chat_id: int) -> Confirmation | None:
        return self.store.consume_confirmation(nonce, user_id=user_id, chat_id=chat_id)
