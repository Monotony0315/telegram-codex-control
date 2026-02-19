from __future__ import annotations

import asyncio
import hmac
import json
import time
from urllib.parse import urlsplit

import httpx

from .auth import Authorizer, extract_message_identity
from .command_policy import CommandPolicy
from .config import Settings
from .runner import Runner
from .safety import SafetyManager, run_prompt_requires_autopilot_confirmation
from .store import ActiveJobExistsError, Store
from .utils import chunk_text, format_status, redact_text


WEBHOOK_READ_TIMEOUT_SECONDS = 5.0
WEBHOOK_MAX_REQUEST_LINE_BYTES = 4096
WEBHOOK_MAX_HEADER_BYTES = 16 * 1024
WEBHOOK_MAX_HEADER_LINES = 64
WEBHOOK_MAX_BODY_BYTES = 2_000_000


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
        self.command_policy = CommandPolicy.from_path(
            owner_user_id=settings.allowed_user_id,
            owner_chat_id=settings.allowed_chat_id,
            policy_path=settings.command_policy_path,
        )
        self.authorizer = Authorizer(
            allowed_user_id=settings.allowed_user_id,
            allowed_chat_id=settings.allowed_chat_id,
            extra_identities=self.command_policy.additional_identities(),
            allow_all_authenticated=settings.command_policy_path is not None,
        )
        self.client = client or httpx.AsyncClient(timeout=float(settings.poll_timeout_seconds) + 10.0)
        self._owns_client = client is None
        self._last_unauthorized_reply_at = 0.0
        self._unauthorized_reply_interval_seconds = 10.0
        self.runner.set_notifier(self.send_to_allowed_chat)
        self._webhook_server: asyncio.base_events.Server | None = None
        self._update_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._webhook_server is not None:
            self._webhook_server.close()
            await self._webhook_server.wait_closed()
            self._webhook_server = None
        if self._owns_client:
            await self.client.aclose()

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        if self.settings.telegram_transport == "webhook":
            await self.webhook_forever(stop_event=stop_event)
            return
        await self.poll_forever(stop_event=stop_event)

    async def poll_forever(self, stop_event: asyncio.Event | None = None) -> None:
        await self._disable_webhook_for_polling()

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

    async def webhook_forever(self, stop_event: asyncio.Event | None = None) -> None:
        await self._configure_webhook()
        self._webhook_server = await asyncio.start_server(
            self._handle_webhook_connection,
            host=self.settings.telegram_webhook_listen_host,
            port=self.settings.telegram_webhook_listen_port,
        )
        self.store.add_event(
            None,
            "webhook_started",
            (
                f"host={self.settings.telegram_webhook_listen_host} "
                f"port={self.settings.telegram_webhook_listen_port} "
                f"path={self.settings.telegram_webhook_path}"
            ),
        )
        async with self._webhook_server:
            if stop_event:
                await stop_event.wait()
            else:
                await asyncio.Event().wait()

    async def handle_update(self, update: dict) -> None:
        async with self._update_lock:
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
        if command == "/help":
            await self.send_message(
                chat_id,
                "Commands: /status, /chat, /run, /autopilot, /codex, /confirm, /cancel, /logs, /help",
            )
            return

        if not self.command_policy.is_allowed(user_id=user_id, chat_id=chat_id, command=command):
            self.store.add_event(
                None,
                "command_policy_denied",
                f"user={user_id} chat={chat_id} command={command}",
            )
            await self.send_message(chat_id, f"Command denied by policy: {command}")
            return

        if command == "/status":
            active_job = self.store.get_active_job()
            status_text = format_status(self.runner.uptime_seconds(), active_job)
            await self.send_message(chat_id, status_text)
            return

        if command == "/chat":
            normalized_arg = arg.strip()
            if not normalized_arg:
                thread_id = self.store.get_chat_session_thread(user_id=user_id, chat_id=chat_id)
                mode_text = "enabled" if self.settings.telegram_interactive_mode else "disabled"
                session_text = thread_id if thread_id else "none"
                await self.send_message(
                    chat_id,
                    (
                        f"Interactive chat: {mode_text}\n"
                        f"Session: {session_text}\n"
                        "Usage: send plain text or /chat <message>\n"
                        "Reset session: /chat reset"
                    ),
                )
                return
            if normalized_arg.lower() == "reset":
                cleared = self.store.clear_chat_session(user_id=user_id, chat_id=chat_id)
                self.store.add_event(
                    None,
                    "chat_session_reset",
                    f"user={user_id} chat={chat_id} cleared={cleared}",
                )
                await self.send_message(chat_id, "Chat session reset." if cleared else "No chat session to reset.")
                return
            if not self.settings.telegram_interactive_mode:
                await self.send_message(
                    chat_id,
                    "Interactive chat is disabled. Use slash commands or set TELEGRAM_INTERACTIVE_MODE=true.",
                )
                return

            existing_thread_id = self.store.get_chat_session_thread(user_id=user_id, chat_id=chat_id)
            try:
                result = await self.runner.run_chat_turn(prompt=normalized_arg, thread_id=existing_thread_id)
            except ActiveJobExistsError:
                await self.send_message(chat_id, "A job is already running.")
                return
            except Exception as exc:
                await self.send_message(chat_id, f"Chat turn failed: {redact_text(str(exc))}")
                return
            self.store.set_chat_session_thread(
                user_id=user_id,
                chat_id=chat_id,
                thread_id=result.thread_id,
            )
            self.store.add_event(
                None,
                "chat_turn",
                (
                    f"user={user_id} chat={chat_id} "
                    f"resumed={existing_thread_id is not None} thread_id={result.thread_id}"
                ),
            )
            await self.send_message(chat_id, result.assistant_text)
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

        if command == "/codex":
            if not arg:
                await self.send_message(chat_id, "Usage: /codex <raw codex args>")
                return
            request = self.safety.request_codex_confirmation(
                task=arg,
                user_id=user_id,
                chat_id=chat_id,
            )
            await self.send_message(
                chat_id,
                (
                    "Confirmation required for /codex.\n"
                    f"Run: /confirm {request.nonce}\n"
                    f"Args length: {len(arg)}\n"
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
                elif confirmation.command == "codex":
                    job = await self.runner.start_codex(confirmation.task)
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
            elif confirmation.command == "codex":
                await self.send_message(chat_id, f"Codex job #{job.id} accepted.")
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
            "Unknown command. Available: /status, /chat, /run, /autopilot, /codex, /confirm, /cancel, /logs, /help",
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

    async def _disable_webhook_for_polling(self) -> None:
        try:
            await self._telegram_post("deleteWebhook", {"drop_pending_updates": False})
        except Exception as exc:
            self.store.add_event(None, "poll_setup_error", redact_text(str(exc)))

    async def _configure_webhook(self) -> None:
        payload: dict[str, object] = {
            "url": self.settings.telegram_webhook_url,
            "allowed_updates": ["message", "edited_message"],
            "drop_pending_updates": False,
            "max_connections": 1,
        }
        if self.settings.telegram_webhook_secret_token:
            payload["secret_token"] = self.settings.telegram_webhook_secret_token
        await self._telegram_post("setWebhook", payload)

    async def _handle_webhook_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        status = 200
        body = b'{"ok":true}'
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=WEBHOOK_READ_TIMEOUT_SECONDS)
            if not request_line:
                await self._write_http_response(writer, 400, b'{"ok":false,"error":"empty request"}')
                return
            if len(request_line) > WEBHOOK_MAX_REQUEST_LINE_BYTES:
                await self._write_http_response(writer, 400, b'{"ok":false,"error":"request line too long"}')
                return
            parts = request_line.decode("latin-1", errors="replace").strip().split()
            if len(parts) < 2:
                await self._write_http_response(writer, 400, b'{"ok":false,"error":"bad request line"}')
                return
            method, target = parts[0].upper(), parts[1]

            headers: dict[str, str] = {}
            header_bytes = 0
            header_lines = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=WEBHOOK_READ_TIMEOUT_SECONDS)
                if line in (b"", b"\r\n"):
                    break
                header_lines += 1
                header_bytes += len(line)
                if header_lines > WEBHOOK_MAX_HEADER_LINES or header_bytes > WEBHOOK_MAX_HEADER_BYTES:
                    status = 400
                    body = b'{"ok":false,"error":"headers too large"}'
                    await self._write_http_response(writer, status, body)
                    return
                decoded = line.decode("latin-1", errors="replace")
                if ":" not in decoded:
                    continue
                key, value = decoded.split(":", 1)
                headers[key.strip().lower()] = value.strip()

            path = urlsplit(target).path
            if method != "POST":
                status = 405
                body = b'{"ok":false,"error":"method not allowed"}'
            elif path != self.settings.telegram_webhook_path:
                status = 404
                body = b'{"ok":false,"error":"not found"}'
            elif self.settings.telegram_webhook_secret_token and (
                not hmac.compare_digest(
                    headers.get("x-telegram-bot-api-secret-token", ""),
                    self.settings.telegram_webhook_secret_token,
                )
            ):
                status = 403
                body = b'{"ok":false,"error":"forbidden"}'
            else:
                content_length_raw = headers.get("content-length", "0")
                try:
                    content_length = int(content_length_raw)
                except ValueError:
                    content_length = -1
                if content_length < 0 or content_length > WEBHOOK_MAX_BODY_BYTES:
                    status = 400
                    body = b'{"ok":false,"error":"invalid content length"}'
                else:
                    payload_bytes = await asyncio.wait_for(
                        reader.readexactly(content_length),
                        timeout=WEBHOOK_READ_TIMEOUT_SECONDS,
                    )
                    try:
                        update = json.loads(payload_bytes.decode("utf-8"))
                    except json.JSONDecodeError:
                        status = 400
                        body = b'{"ok":false,"error":"invalid json"}'
                    else:
                        if isinstance(update, dict):
                            await self.handle_update(update)
                        else:
                            status = 400
                            body = b'{"ok":false,"error":"invalid update payload"}'
        except asyncio.IncompleteReadError:
            status = 400
            body = b'{"ok":false,"error":"incomplete body"}'
        except asyncio.TimeoutError:
            status = 408
            body = b'{"ok":false,"error":"request timeout"}'
        except Exception as exc:
            self.store.add_event(None, "webhook_error", redact_text(str(exc)))
            status = 500
            body = b'{"ok":false,"error":"internal"}'
        await self._write_http_response(writer, status, body)

    async def _write_http_response(self, writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
            408: "Request Timeout",
            500: "Internal Server Error",
        }.get(status, "OK")
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(header + body)
        try:
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

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
        stripped = text.strip()
        if not stripped:
            return "/chat", ""
        if not stripped.startswith("/"):
            return "/chat", stripped
        parts = text.split(maxsplit=1)
        command_token = parts[0]
        if command_token.startswith("/"):
            command_token = command_token.split("@", 1)[0]
        command = command_token.lower()
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
