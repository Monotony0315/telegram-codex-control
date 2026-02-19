from __future__ import annotations

import asyncio
import time

import httpx

from .auth import Authorizer, extract_message_identity
from .config import Settings
from .runner import Runner
from .safety import SafetyManager, run_prompt_requires_autopilot_confirmation
from .store import ActiveJobExistsError, Store
from .utils import chunk_text, format_status, redact_text


class TelegramBotDaemon:
    """Long-polling Telegram bot controller for Codex jobs."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        runner: Runner,
        safety: SafetyManager,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.store = store
        self.runner = runner
        self.safety = safety
        self.authorizer = Authorizer(
            allowed_user_id=settings.allowed_user_id,
            allowed_chat_id=settings.allowed_chat_id,
        )
        self.client = client or httpx.AsyncClient(timeout=float(settings.poll_timeout_seconds) + 10.0)
        self._owns_client = client is None
        self._last_unauthorized_reply_at = 0.0
        self._unauthorized_reply_interval_seconds = 10.0
        self.runner.set_notifier(self.send_to_allowed_chat)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def poll_forever(self, stop_event: asyncio.Event | None = None) -> None:
        backoff = self.settings.poll_retry_base_seconds
        while True:
            if stop_event and stop_event.is_set():
                return
            try:
                offset = self.store.get_last_update_id() + 1
                updates = await self._get_updates(offset=offset)
                for update in updates:
                    try:
                        await self.handle_update(update)
                    except Exception as exc:
                        update_id = update.get("update_id") if isinstance(update, dict) else None
                        self.store.add_event(
                            None,
                            "update_error",
                            redact_text(f"update_id={update_id} error={exc}"),
                        )
                backoff = self.settings.poll_retry_base_seconds
            except Exception as exc:
                self.store.add_event(None, "poll_error", redact_text(str(exc)))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, self.settings.poll_retry_max_seconds)

    async def handle_update(self, update: dict) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return

        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            self.store.claim_update_with_event(
                update_id,
                event_type="update_ignored",
                message="reason=missing_message",
            )
            return
        text = message.get("text")
        if not isinstance(text, str):
            self.store.claim_update_with_event(
                update_id,
                event_type="update_ignored",
                message="reason=missing_text",
            )
            return
        text = text.strip()
        if not text:
            self.store.claim_update_with_event(
                update_id,
                event_type="update_ignored",
                message="reason=empty_text",
            )
            return

        user_id, chat_id = extract_message_identity(update)
        if not self.authorizer.is_authorized(user_id, chat_id):
            claimed = self.store.claim_update_with_event(
                update_id,
                event_type="auth_denied",
                message=redact_text(
                    f"update_id={update_id} user={user_id} chat={chat_id} command={self._audit_command(text)}"
                ),
            )
            if not claimed:
                return
            if chat_id is not None and self._should_send_unauthorized_reply():
                await self.send_message(chat_id, "Unauthorized.")
            return

        claimed = self.store.claim_update_with_event(
            update_id,
            event_type="command_received",
            message=self._command_audit_message(update_id=update_id, text=text),
        )
        if not claimed:
            return

        assert user_id is not None
        assert chat_id is not None
        try:
            await self.handle_command(chat_id=chat_id, user_id=user_id, text=text)
        except Exception as exc:
            self.store.add_event(
                None,
                "command_error",
                redact_text(f"update_id={update_id} error={exc}"),
            )
            await self.send_message(chat_id, "Internal error while handling command.")

    async def handle_command(self, *, chat_id: int, user_id: int, text: str) -> None:
        command, arg = self._parse_command(text)

        if command == "/status":
            active_job = self.store.get_active_job()
            status_text = format_status(self.runner.uptime_seconds(), active_job)
            await self.send_message(chat_id, status_text)
            return

        if command == "/run":
            if not arg:
                await self.send_message(chat_id, "Usage: /run <prompt>")
                return
            if run_prompt_requires_autopilot_confirmation(arg):
                await self.send_message(
                    chat_id,
                    "Autopilot-like prompts are blocked on /run. Use /autopilot <task> and /confirm <nonce>.",
                )
                return
            request = self.safety.request_run_confirmation(
                task=arg,
                user_id=user_id,
                chat_id=chat_id,
            )
            guidance = (
                "Confirmation required for /run.\n"
                f"Run: /confirm {request.nonce}\n"
                f"Prompt length: {len(arg)}\n"
                f"Expires: {request.expires_at}"
            )
            await self.send_message(chat_id, guidance)
            return

        if command == "/autopilot":
            if not arg:
                await self.send_message(chat_id, "Usage: /autopilot <task>")
                return
            request = self.safety.request_autopilot_confirmation(
                task=arg,
                user_id=user_id,
                chat_id=chat_id,
            )
            await self.send_message(
                chat_id,
                (
                    "Confirmation required.\n"
                    f"Run: /confirm {request.nonce}\n"
                    f"Task: {request.task}\n"
                    f"Expires: {request.expires_at}"
                ),
            )
            return

        if command == "/confirm":
            if not arg:
                await self.send_message(chat_id, "Usage: /confirm <nonce>")
                return
            confirmation = self.safety.get_confirmation(
                nonce=arg,
                user_id=user_id,
                chat_id=chat_id,
            )
            if confirmation is None:
                await self.send_message(chat_id, "Invalid or expired confirmation nonce.")
                return
            try:
                if confirmation.command == "run":
                    job = await self.runner.start_run(confirmation.task)
                elif confirmation.command == "autopilot":
                    job = await self.runner.start_autopilot(confirmation.task)
                else:
                    await self.send_message(chat_id, f"Unsupported confirmation command: {confirmation.command}")
                    return
            except ActiveJobExistsError:
                await self.send_message(chat_id, "A job is already running.")
                return
            except Exception as exc:
                await self.send_message(chat_id, f"Failed to start confirmed job: {redact_text(str(exc))}")
                return
            consumed = self.safety.consume_confirmation(
                nonce=arg,
                user_id=user_id,
                chat_id=chat_id,
            )
            if consumed is None:
                self.store.add_event(
                    job.id,
                    "confirmation_consume_race",
                    "Confirmation was not consumable after successful admission",
                )
            if confirmation.command == "run":
                await self.send_message(chat_id, f"Run job #{job.id} accepted.")
            else:
                await self.send_message(chat_id, f"Autopilot job #{job.id} accepted.")
            return

        if command == "/cancel":
            cancelled = await self.runner.cancel_active_job()
            if cancelled:
                await self.send_message(chat_id, "Cancellation request sent.")
            elif self.store.get_active_job() is not None:
                await self.send_message(chat_id, "Cancellation blocked or still in progress. Check /logs.")
            else:
                await self.send_message(chat_id, "No active job.")
            return

        if command == "/logs":
            await self._send_logs(chat_id=chat_id)
            return

        await self.send_message(
            chat_id,
            "Unknown command. Available: /status, /run, /autopilot, /confirm, /cancel, /logs",
        )

    async def send_to_allowed_chat(self, text: str) -> None:
        await self.send_message(self.settings.allowed_chat_id, text)

    async def send_message(self, chat_id: int, text: str) -> None:
        safe_text = redact_text(text)
        chunks = chunk_text(safe_text, max_size=self.settings.message_chunk_size)
        if not chunks:
            chunks = [safe_text]
        for chunk in chunks:
            await self._telegram_post("sendMessage", {"chat_id": chat_id, "text": chunk})

    async def _send_logs(self, *, chat_id: int) -> None:
        rows = self.store.list_events(limit=100)
        if not rows:
            await self.send_message(chat_id, "No logs available.")
            return

        lines: list[str] = []
        for row in rows:
            job_id = row["job_id"] if row["job_id"] is not None else "-"
            safe_message = self._sanitize_log_message(row["event_type"], row["message"])
            line = f"{row['created_at']} job={job_id} {row['event_type']} {safe_message}"
            lines.append(line)
        await self.send_message(chat_id, "\n".join(lines))

    async def _get_updates(self, *, offset: int) -> list[dict]:
        payload = {"offset": offset, "timeout": self.settings.poll_timeout_seconds}
        result = await self._telegram_post("getUpdates", payload)
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    async def _telegram_post(self, method: str, payload: dict) -> object:
        url = f"{self.settings.telegram_base_url}/{method}"
        response = await self.client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(f"Telegram API error on {method}: {data}")
        return data.get("result")

    @staticmethod
    def _parse_command(text: str) -> tuple[str, str]:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        return command, arg

    @classmethod
    def _audit_command(cls, text: str) -> str:
        command, _arg = cls._parse_command(text)
        return command

    @classmethod
    def _command_audit_message(cls, *, update_id: int, text: str) -> str:
        command, arg = cls._parse_command(text)
        return f"update_id={update_id} command={command} arg_len={len(arg)}"

    @staticmethod
    def _sanitize_log_message(event_type: str, message: str) -> str:
        if event_type.startswith("process_"):
            return "[process output omitted]"
        safe = redact_text(message)
        if len(safe) > 280:
            return safe[:277] + "..."
        return safe

    def _should_send_unauthorized_reply(self) -> bool:
        now = time.monotonic()
        if now - self._last_unauthorized_reply_at < self._unauthorized_reply_interval_seconds:
            return False
        self._last_unauthorized_reply_at = now
        return True
