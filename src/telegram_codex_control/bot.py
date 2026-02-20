from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hmac
import json
from pathlib import Path
import re
import shlex
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
INVOCATION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_DISCOVERED_ITEMS = 60
MAX_LISTED_FILES = 200
MAX_READ_LINES_DEFAULT = 200
MAX_READ_LINES_LIMIT = 1000
MAX_READ_FILE_BYTES = 512 * 1024
MAX_READ_LINE_CHARS = 400
MAX_SEARCH_OUTPUT_LINES = 200
MAX_SEARCH_OUTPUT_CHARS = 12000
RG_TIMEOUT_SECONDS = 5.0
RG_MAX_MATCHES = 500


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
            raw_text = message.get("text")
            text = raw_text.strip() if isinstance(raw_text, str) else ""
            document = message.get("document")
            has_document = isinstance(document, dict)
            if not has_document:
                if not isinstance(raw_text, str):
                    self.store.claim_update_with_event(
                        update_id,
                        event_type="update_ignored",
                        message="reason=missing_text",
                    )
                    return
                if not text:
                    self.store.claim_update_with_event(
                        update_id,
                        event_type="update_ignored",
                        message="reason=empty_text",
                    )
                    return

            user_id, chat_id = extract_message_identity(update)
            audited_command = "/upload" if has_document else self._audit_command(text)
            if not self.authorizer.is_authorized(user_id, chat_id):
                claimed = self.store.claim_update_with_event(
                    update_id,
                    event_type="auth_denied",
                    message=redact_text(
                        f"update_id={update_id} user={user_id} chat={chat_id} command={audited_command}"
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
                message=(
                    self._command_audit_message_for_command(update_id=update_id, command="/upload", arg_len=0)
                    if has_document
                    else self._command_audit_message(update_id=update_id, text=text)
                ),
            )
            if not claimed:
                return

            assert user_id is not None
            assert chat_id is not None
            if has_document:
                if not self.command_policy.is_allowed(user_id=user_id, chat_id=chat_id, command="/upload"):
                    self.store.add_event(
                        None,
                        "command_policy_denied",
                        f"user={user_id} chat={chat_id} command=/upload",
                    )
                    await self.send_message(chat_id, "Command denied by policy: /upload")
                    return
                try:
                    await self._handle_document_upload(chat_id=chat_id, user_id=user_id, document=document)
                except Exception as exc:
                    self.store.add_event(
                        None,
                        "command_error",
                        redact_text(f"update_id={update_id} error={exc}"),
                    )
                    await self.send_message(chat_id, "Internal error while handling upload.")
                return
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
                (
                    "Commands: /status, /chat, /run, /autopilot, /codex, /files, /search, "
                    "/read, /download, /report, /confirm, /cancel, /skills, /skill, /prompts, "
                    "/prompt, /logs, /help"
                ),
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

        if command == "/files":
            await self._handle_files_command(chat_id=chat_id, raw_arg=arg)
            return

        if command == "/search":
            await self._handle_search_command(chat_id=chat_id, raw_arg=arg)
            return

        if command == "/read":
            await self._handle_read_command(chat_id=chat_id, raw_arg=arg)
            return

        if command == "/download":
            await self._handle_download_command(chat_id=chat_id, raw_arg=arg)
            return

        if command == "/report":
            await self._handle_report_command(chat_id=chat_id, user_id=user_id, raw_arg=arg)
            return

        if command == "/skills":
            names = self._discover_skill_names()
            await self._send_discovered_items(
                chat_id=chat_id,
                label="skills",
                items=names,
                filter_query=arg,
                usage="Use: /skill <name> <task>",
            )
            return

        if command == "/prompts":
            names = self._discover_prompt_names()
            await self._send_discovered_items(
                chat_id=chat_id,
                label="prompts",
                items=names,
                filter_query=arg,
                usage="Use: /prompt <name> <task>",
            )
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

            try:
                existing_thread_id = self.store.get_chat_session_thread(user_id=user_id, chat_id=chat_id)
                result = await self.runner.run_chat_turn(prompt=normalized_arg, thread_id=existing_thread_id)
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
                        f"resumed={existing_thread_id is not None} thread_id={result.thread_id} "
                        f"assistant_len={len(result.assistant_text)}"
                    ),
                )
            except ActiveJobExistsError:
                await self.send_message(chat_id, "A job is already running.")
                return
            except Exception as exc:
                await self.send_message(chat_id, f"Chat turn failed: {redact_text(str(exc))}")
                return
            assistant_text = result.assistant_text.strip() or "No response."
            if assistant_text == "No response.":
                self.store.add_event(
                    None,
                    "chat_empty_response",
                    f"user={user_id} chat={chat_id} thread_id={result.thread_id}",
                )
                assistant_text = "No response from Codex. Please retry, or run /chat reset."
            await self.send_message(chat_id, assistant_text)
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

        if command == "/skill":
            parsed = self._parse_named_invocation(arg)
            if parsed is None:
                await self.send_message(chat_id, "Usage: /skill <name> <task>")
                return
            skill_name, task = parsed
            payload = self._encode_named_invocation_payload(kind="skill", name=skill_name, task=task)
            request = self.safety.request_confirmation(
                command="skill",
                task=payload,
                user_id=user_id,
                chat_id=chat_id,
            )
            await self.send_message(
                chat_id,
                (
                    "Confirmation required for /skill.\n"
                    f"Run: /confirm {request.nonce}\n"
                    f"Skill: {skill_name}\n"
                    f"Task: {self._preview_text(task)}\n"
                    f"Expires: {request.expires_at}"
                ),
            )
            return

        if command == "/prompt":
            parsed = self._parse_named_invocation(arg)
            if parsed is None:
                await self.send_message(chat_id, "Usage: /prompt <name> <task>")
                return
            prompt_name, task = parsed
            payload = self._encode_named_invocation_payload(kind="prompt", name=prompt_name, task=task)
            request = self.safety.request_confirmation(
                command="prompt",
                task=payload,
                user_id=user_id,
                chat_id=chat_id,
            )
            await self.send_message(
                chat_id,
                (
                    "Confirmation required for /prompt.\n"
                    f"Run: /confirm {request.nonce}\n"
                    f"Prompt: {prompt_name}\n"
                    f"Task: {self._preview_text(task)}\n"
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
            accepted_message = ""
            try:
                if confirmation.command == "run":
                    job = await self.runner.start_run(confirmation.task)
                    accepted_message = f"Run job #{job.id} accepted."
                elif confirmation.command == "autopilot":
                    job = await self.runner.start_autopilot(confirmation.task)
                    accepted_message = f"Autopilot job #{job.id} accepted."
                elif confirmation.command == "codex":
                    job = await self.runner.start_codex(confirmation.task)
                    accepted_message = f"Codex job #{job.id} accepted."
                elif confirmation.command == "skill":
                    parsed = self._decode_named_invocation_payload(payload=confirmation.task, expected_kind="skill")
                    if parsed is None:
                        await self.send_message(chat_id, "Invalid /skill confirmation payload.")
                        return
                    skill_name, task = parsed
                    composed_prompt = f"${skill_name} {task}".strip()
                    job = await self.runner.start_run(composed_prompt)
                    accepted_message = f"Skill job #{job.id} accepted."
                elif confirmation.command == "prompt":
                    parsed = self._decode_named_invocation_payload(payload=confirmation.task, expected_kind="prompt")
                    if parsed is None:
                        await self.send_message(chat_id, "Invalid /prompt confirmation payload.")
                        return
                    prompt_name, task = parsed
                    composed_prompt = f"/prompts:{prompt_name} {task}".strip()
                    job = await self.runner.start_run(composed_prompt)
                    accepted_message = f"Prompt job #{job.id} accepted."
                elif confirmation.command == "report":
                    parsed = self._decode_report_payload(confirmation.task)
                    if parsed is None:
                        await self.send_message(chat_id, "Invalid /report confirmation payload.")
                        return
                    report_topic, report_path = parsed
                    prompt = self._build_report_prompt(topic=report_topic, relative_report_path=report_path)
                    job = await self.runner.start_run(prompt)
                    accepted_message = f"Report job #{job.id} accepted. Planned path: {report_path}"
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
            await self.send_message(chat_id, accepted_message)
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
            (
                "Unknown command. Available: /status, /chat, /run, /autopilot, /codex, /files, "
                "/search, /read, /download, /report, /confirm, /cancel, /skills, /skill, "
                "/prompts, /prompt, /logs, /help"
            ),
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

    async def _handle_files_command(self, *, chat_id: int, raw_arg: str) -> None:
        args = self._split_shell_args(raw_arg)
        if args is None or len(args) > 1:
            await self.send_message(chat_id, "Usage: /files [relative_dir]")
            return
        relative_dir = args[0] if args else "."
        try:
            target = self._resolve_workspace_path(relative_dir)
        except ValueError as exc:
            await self.send_message(chat_id, str(exc))
            return
        if not target.exists():
            await self.send_message(chat_id, f"Path not found: {relative_dir}")
            return
        if not target.is_dir():
            await self.send_message(chat_id, f"Path is not a directory: {relative_dir}")
            return
        try:
            entries = sorted(
                target.iterdir(),
                key=lambda path: (not path.is_dir(), path.name.lower()),
            )
        except OSError as exc:
            await self.send_message(chat_id, f"Failed to list directory: {redact_text(str(exc))}")
            return

        rel_dir = self._relative_workspace_path(target)
        if not entries:
            await self.send_message(chat_id, f"No entries in {rel_dir}.")
            return

        listed = entries[:MAX_LISTED_FILES]
        lines = [f"- {entry.name}/" if entry.is_dir() else f"- {entry.name}" for entry in listed]
        remaining = len(entries) - len(listed)
        suffix = f"\n...and {remaining} more entries." if remaining > 0 else ""
        await self.send_message(
            chat_id,
            f"Entries in {rel_dir} ({len(entries)}):\n" + "\n".join(lines) + suffix,
        )

    async def _handle_search_command(self, *, chat_id: int, raw_arg: str) -> None:
        args = self._split_shell_args(raw_arg)
        if args is None or not args or len(args) > 2:
            await self.send_message(chat_id, "Usage: /search <pattern> [relative_dir_or_file]")
            return
        pattern = args[0].strip()
        if not pattern:
            await self.send_message(chat_id, "Usage: /search <pattern> [relative_dir_or_file]")
            return
        raw_scope = args[1] if len(args) == 2 else "."
        try:
            scope = self._resolve_workspace_path(raw_scope)
        except ValueError as exc:
            await self.send_message(chat_id, str(exc))
            return
        if not scope.exists():
            await self.send_message(chat_id, f"Path not found: {raw_scope}")
            return
        try:
            output = await self._run_ripgrep(pattern=pattern, scope=scope)
        except ValueError as exc:
            await self.send_message(chat_id, str(exc))
            return
        await self.send_message(chat_id, output)

    async def _handle_read_command(self, *, chat_id: int, raw_arg: str) -> None:
        args = self._split_shell_args(raw_arg)
        if args is None or not args or len(args) > 2:
            await self.send_message(chat_id, "Usage: /read <relative_file> [max_lines]")
            return
        try:
            target = self._resolve_workspace_path(args[0])
        except ValueError as exc:
            await self.send_message(chat_id, str(exc))
            return
        max_lines = MAX_READ_LINES_DEFAULT
        if len(args) == 2:
            try:
                max_lines = int(args[1])
            except ValueError:
                await self.send_message(chat_id, "max_lines must be an integer.")
                return
            if max_lines <= 0:
                await self.send_message(chat_id, "max_lines must be greater than 0.")
                return
            if max_lines > MAX_READ_LINES_LIMIT:
                max_lines = MAX_READ_LINES_LIMIT
        if not target.exists():
            await self.send_message(chat_id, f"Path not found: {args[0]}")
            return
        if not target.is_file():
            await self.send_message(chat_id, f"Path is not a file: {args[0]}")
            return
        try:
            file_size = target.stat().st_size
        except OSError as exc:
            await self.send_message(chat_id, f"Failed to stat file: {redact_text(str(exc))}")
            return
        if file_size > MAX_READ_FILE_BYTES:
            await self.send_message(
                chat_id,
                (
                    f"File is too large for /read ({file_size} bytes > {MAX_READ_FILE_BYTES} bytes). "
                    "Use /download."
                ),
            )
            return
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            await self.send_message(chat_id, f"Failed to read file: {redact_text(str(exc))}")
            return
        lines = content.splitlines()
        rel_path = self._relative_workspace_path(target)
        if not lines:
            await self.send_message(chat_id, f"{rel_path} is empty.")
            return
        selected = lines[:max_lines]
        formatted_lines: list[str] = []
        for line_number, line in enumerate(selected, start=1):
            rendered = line
            if len(rendered) > MAX_READ_LINE_CHARS:
                rendered = rendered[: MAX_READ_LINE_CHARS - 3] + "..."
            formatted_lines.append(f"{line_number:>5}: {rendered}")
        suffix = f"\n...truncated {len(lines) - len(selected)} lines." if len(lines) > len(selected) else ""
        payload = f"{rel_path} ({len(lines)} lines):\n" + "\n".join(formatted_lines) + suffix
        if len(payload) > MAX_SEARCH_OUTPUT_CHARS:
            payload = payload[: MAX_SEARCH_OUTPUT_CHARS - 3] + "..."
        await self.send_message(chat_id, payload)

    async def _handle_download_command(self, *, chat_id: int, raw_arg: str) -> None:
        args = self._split_shell_args(raw_arg)
        if args is None or len(args) != 1:
            await self.send_message(chat_id, "Usage: /download <relative_file>")
            return
        try:
            target = self._resolve_workspace_path(args[0])
        except ValueError as exc:
            await self.send_message(chat_id, str(exc))
            return
        if not target.exists():
            await self.send_message(chat_id, f"Path not found: {args[0]}")
            return
        if not target.is_file():
            await self.send_message(chat_id, f"Path is not a file: {args[0]}")
            return
        try:
            file_size = target.stat().st_size
        except OSError as exc:
            await self.send_message(chat_id, f"Failed to stat file: {redact_text(str(exc))}")
            return
        if file_size > self.settings.max_download_file_size_bytes:
            await self.send_message(
                chat_id,
                (
                    f"File exceeds download limit ({file_size} bytes > "
                    f"{self.settings.max_download_file_size_bytes} bytes)."
                ),
            )
            return
        try:
            await self._telegram_send_document(chat_id=chat_id, file_path=target)
        except Exception as exc:
            await self.send_message(chat_id, f"Failed to send file: {redact_text(str(exc))}")
            return
        rel_path = self._relative_workspace_path(target)
        await self.send_message(chat_id, f"Sent file: {rel_path} ({file_size} bytes).")

    async def _handle_report_command(self, *, chat_id: int, user_id: int, raw_arg: str) -> None:
        topic = raw_arg.strip()
        if not topic:
            await self.send_message(chat_id, "Usage: /report <topic>")
            return
        report_path = self._planned_report_path(topic)
        payload = self._encode_report_payload(topic=topic, report_path=report_path)
        request = self.safety.request_confirmation(
            command="report",
            task=payload,
            user_id=user_id,
            chat_id=chat_id,
        )
        await self.send_message(
            chat_id,
            (
                "Confirmation required for /report.\n"
                f"Run: /confirm {request.nonce}\n"
                f"Topic: {self._preview_text(topic)}\n"
                f"Planned path: {report_path}\n"
                f"Expires: {request.expires_at}"
            ),
        )

    async def _handle_document_upload(self, *, chat_id: int, user_id: int, document: dict) -> None:
        try:
            relative_path, saved_size = await self._download_telegram_document(document)
        except ValueError as exc:
            await self.send_message(chat_id, str(exc))
            return
        except Exception as exc:
            await self.send_message(chat_id, f"Upload failed: {redact_text(str(exc))}")
            return
        self.store.add_event(
            None,
            "upload_saved",
            f"user={user_id} chat={chat_id} path={relative_path} size={saved_size}",
        )
        await self.send_message(chat_id, f"Uploaded file saved to {relative_path} ({saved_size} bytes).")

    async def _download_telegram_document(self, document: dict) -> tuple[str, int]:
        file_id = document.get("file_id")
        if not isinstance(file_id, str) or not file_id.strip():
            raise ValueError("Invalid upload payload: missing file_id.")

        declared_size = document.get("file_size")
        if isinstance(declared_size, int) and declared_size > self.settings.max_upload_file_size_bytes:
            raise ValueError(
                (
                    f"Upload exceeds limit ({declared_size} bytes > "
                    f"{self.settings.max_upload_file_size_bytes} bytes)."
                )
            )

        file_info = await self._telegram_post("getFile", {"file_id": file_id})
        if not isinstance(file_info, dict):
            raise ValueError("Telegram getFile returned an invalid payload.")
        file_path = file_info.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise ValueError("Telegram did not return a file path for the upload.")

        remote_size = file_info.get("file_size")
        if isinstance(remote_size, int) and remote_size > self.settings.max_upload_file_size_bytes:
            raise ValueError(
                (
                    f"Upload exceeds limit ({remote_size} bytes > "
                    f"{self.settings.max_upload_file_size_bytes} bytes)."
                )
            )

        download_url = f"{self.settings.telegram_api_base}/file/bot{self.settings.telegram_bot_token}/{file_path}"
        response = await self.client.get(download_url)
        response.raise_for_status()
        content = response.content
        if len(content) > self.settings.max_upload_file_size_bytes:
            raise ValueError(
                (
                    f"Upload exceeds limit ({len(content)} bytes > "
                    f"{self.settings.max_upload_file_size_bytes} bytes)."
                )
            )

        upload_dir = self.settings.upload_dir
        upload_dir.mkdir(parents=True, exist_ok=True)

        raw_name = document.get("file_name") if isinstance(document.get("file_name"), str) else Path(file_path).name
        safe_name = self._sanitize_upload_filename(raw_name, fallback=f"upload-{file_id}")
        target = self._allocate_upload_path(upload_dir=upload_dir, file_name=safe_name)
        target.write_bytes(content)
        return self._relative_workspace_path(target), len(content)

    async def _run_ripgrep(self, *, pattern: str, scope: Path) -> str:
        scope_arg = self._relative_workspace_path(scope)
        argv = [
            "rg",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            str(RG_MAX_MATCHES),
            "--",
            pattern,
            scope_arg,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.settings.workspace_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise ValueError("Search unavailable: ripgrep (rg) is not installed.") from None
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(process.communicate(), timeout=RG_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError(f"Search timed out after {RG_TIMEOUT_SECONDS:.1f}s.") from None

        stdout_text = stdout_raw.decode(errors="replace")
        stderr_text = stderr_raw.decode(errors="replace").strip()
        if process.returncode == 1:
            return "No matches found."
        if process.returncode != 0:
            detail = redact_text(stderr_text or f"exit={process.returncode}")
            raise ValueError(f"Search failed: {detail}")

        lines = stdout_text.splitlines()
        if not lines:
            return "No matches found."
        shown_lines = lines[:MAX_SEARCH_OUTPUT_LINES]
        rendered = "\n".join(shown_lines)
        if len(rendered) > MAX_SEARCH_OUTPUT_CHARS:
            rendered = rendered[: MAX_SEARCH_OUTPUT_CHARS - 3] + "..."
        if len(lines) > len(shown_lines):
            rendered += f"\n...truncated {len(lines) - len(shown_lines)} additional matches."
        return rendered

    async def _telegram_send_document(self, *, chat_id: int, file_path: Path) -> object:
        url = f"{self.settings.telegram_base_url}/sendDocument"
        with file_path.open("rb") as file_obj:
            response = await self.client.post(
                url,
                data={"chat_id": str(chat_id)},
                files={"document": (file_path.name, file_obj, "application/octet-stream")},
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(f"Telegram API error on sendDocument: {data}")
        return data.get("result")

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
    def _command_audit_message_for_command(*, update_id: int, command: str, arg_len: int) -> str:
        return f"update_id={update_id} command={command} arg_len={arg_len}"

    @staticmethod
    def _sanitize_log_message(event_type: str, message: str) -> str:
        if event_type.startswith("process_"):
            return "[process output omitted]"
        safe = redact_text(message)
        if len(safe) > 280:
            return safe[:277] + "..."
        return safe

    def _resolve_workspace_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path.strip())
        if candidate.is_absolute():
            raise ValueError("Path must be relative to WORKSPACE_ROOT.")
        resolved = (self.settings.workspace_root / candidate).resolve()
        if not resolved.is_relative_to(self.settings.workspace_root):
            raise ValueError("Path escapes WORKSPACE_ROOT.")
        return resolved

    def _relative_workspace_path(self, path: Path) -> str:
        rel = path.resolve().relative_to(self.settings.workspace_root)
        if rel == Path("."):
            return "."
        return rel.as_posix()

    @staticmethod
    def _split_shell_args(raw_arg: str) -> list[str] | None:
        try:
            return shlex.split(raw_arg)
        except ValueError:
            return None

    def _discover_skill_names(self) -> list[str]:
        roots = (
            Path.home() / ".agents" / "skills",
            Path.home() / ".codex" / "skills",
            self.settings.workspace_root / ".agents" / "skills",
            self.settings.workspace_root / ".codex" / "skills",
        )
        names: set[str] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                if not (child / "SKILL.md").is_file():
                    continue
                names.add(child.name)
        return sorted(names, key=str.lower)

    def _discover_prompt_names(self) -> list[str]:
        roots = (
            Path.home() / ".codex" / "prompts",
            self.settings.workspace_root / ".codex" / "prompts",
        )
        names: set[str] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for child in root.glob("*.md"):
                if child.is_file():
                    names.add(child.stem)
        return sorted(names, key=str.lower)

    async def _send_discovered_items(
        self,
        *,
        chat_id: int,
        label: str,
        items: list[str],
        filter_query: str,
        usage: str,
    ) -> None:
        query = filter_query.strip().lower()
        filtered = [item for item in items if query in item.lower()] if query else list(items)
        if not filtered:
            query_suffix = f" matching '{filter_query.strip()}'" if query else ""
            await self.send_message(chat_id, f"No {label} found{query_suffix}.")
            return
        capped = filtered[:MAX_DISCOVERED_ITEMS]
        extra_count = len(filtered) - len(capped)
        extra_text = f"\n...and {extra_count} more." if extra_count > 0 else ""
        await self.send_message(
            chat_id,
            (
                f"Available {label} ({len(filtered)}):\n"
                + ", ".join(capped)
                + extra_text
                + f"\n{usage}"
            ),
        )

    @staticmethod
    def _parse_named_invocation(raw_arg: str) -> tuple[str, str] | None:
        arg = raw_arg.strip()
        if not arg:
            return None
        parts = arg.split(maxsplit=1)
        name = parts[0].strip()
        task = parts[1].strip() if len(parts) > 1 else ""
        if not task:
            return None
        if not INVOCATION_NAME_RE.match(name):
            return None
        return name, task

    @staticmethod
    def _encode_named_invocation_payload(*, kind: str, name: str, task: str) -> str:
        return json.dumps(
            {"kind": kind, "name": name, "task": task},
            ensure_ascii=False,
        )

    @staticmethod
    def _decode_named_invocation_payload(payload: str, *, expected_kind: str) -> tuple[str, str] | None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("kind") != expected_kind:
            return None
        name = data.get("name")
        task = data.get("task")
        if not isinstance(name, str) or not INVOCATION_NAME_RE.match(name):
            return None
        if not isinstance(task, str) or not task.strip():
            return None
        return name, task.strip()

    @staticmethod
    def _encode_report_payload(*, topic: str, report_path: str) -> str:
        return json.dumps(
            {"kind": "report", "topic": topic, "report_path": report_path},
            ensure_ascii=False,
        )

    @staticmethod
    def _decode_report_payload(payload: str) -> tuple[str, str] | None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("kind") != "report":
            return None
        topic = data.get("topic")
        report_path = data.get("report_path")
        if not isinstance(topic, str) or not topic.strip():
            return None
        if not isinstance(report_path, str) or not report_path.strip():
            return None
        return topic.strip(), report_path.strip()

    @classmethod
    def _planned_report_path(cls, topic: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        slug = cls._slugify_topic(topic)
        return f"reports/{timestamp}-{slug}.md"

    @staticmethod
    def _slugify_topic(topic: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
        if not slug:
            slug = "report"
        if len(slug) > 64:
            slug = slug[:64].rstrip("-")
        return slug or "report"

    @staticmethod
    def _build_report_prompt(*, topic: str, relative_report_path: str) -> str:
        return (
            "Create a markdown report in the workspace.\n"
            f"Topic: {topic}\n"
            f"Output file: {relative_report_path}\n"
            "Requirements:\n"
            "1. Ensure the parent directory exists.\n"
            "2. Include a title, summary, key points, and next steps.\n"
            "3. Save the final report to the exact output file path.\n"
            "4. In your response, confirm the saved path.\n"
        )

    @staticmethod
    def _sanitize_upload_filename(file_name: str, *, fallback: str) -> str:
        candidate = Path(file_name).name.strip()
        if not candidate:
            candidate = fallback
        sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", candidate)
        sanitized = sanitized.lstrip(".")
        if not sanitized:
            sanitized = fallback
        if len(sanitized) > 180:
            prefix, dot, suffix = sanitized.partition(".")
            if dot:
                trimmed_prefix = prefix[: max(1, 180 - len(suffix) - 1)]
                sanitized = f"{trimmed_prefix}.{suffix}"
            else:
                sanitized = sanitized[:180]
        return sanitized

    @staticmethod
    def _allocate_upload_path(*, upload_dir: Path, file_name: str) -> Path:
        candidate = upload_dir / file_name
        if not candidate.exists():
            return candidate
        stem = candidate.stem or "upload"
        suffix = candidate.suffix
        for idx in range(1, 1000):
            alt = upload_dir / f"{stem}-{idx}{suffix}"
            if not alt.exists():
                return alt
        raise ValueError("Could not allocate a unique upload file name.")

    @staticmethod
    def _preview_text(text: str, *, limit: int = 220) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _should_send_unauthorized_reply(self) -> bool:
        now = time.monotonic()
        if now - self._last_unauthorized_reply_at < self._unauthorized_reply_interval_seconds:
            return False
        self._last_unauthorized_reply_at = now
        return True
