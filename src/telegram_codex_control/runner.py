from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
import asyncio
import json
import os
import shlex
import signal
import subprocess
import tempfile
import time

from .config import Settings
from .live_events import ExecutionEvent, parse_execution_events
from .network_diagnostics import (
    build_dns_network_restriction_guidance,
    is_dns_network_restriction_error,
)
from .store import ActiveJobExistsError, Job, Store
from .utils import chunk_text, redact_text


@dataclass(frozen=True, slots=True)
class RunnerNotification:
    text: str
    job_id: int | None = None


Notifier = Callable[[RunnerNotification], Awaitable[None]]
ChatEventCallback = Callable[[ExecutionEvent], Awaitable[None]]
CANCEL_SLA_SECONDS = 15.0
CHAT_TURN_POLL_INTERVAL_SECONDS = 0.25
CHAT_TIMEOUT_PARTIAL_TEXT_MAX_CHARS = 240


@dataclass(frozen=True, slots=True)
class ChatTurnResult:
    thread_id: str
    assistant_text: str


@dataclass(frozen=True, slots=True)
class _ChatAttemptOutput:
    stdout_text: str
    stderr_text: str
    timeout_kind: str | None


@dataclass(slots=True)
class _OwnedExecution:
    process: asyncio.subprocess.Process
    job_task: asyncio.Task[None]
    job_id: int
    cancel_requested: bool = False


class _ChatTurnTimeoutError(RuntimeError):
    def __init__(self, *, thread_id: str | None):
        super().__init__("Chat turn timed out")
        self.thread_id = thread_id


class Runner:
    """Single active-job process runner for Codex commands."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._job_task: asyncio.Task[None] | None = None
        self._active_job_id: int | None = None
        self._cancel_requested = False
        self._owned_executions: dict[str, _OwnedExecution] = {}
        self._notifier: Notifier | None = None
        self._started_at_monotonic = time.monotonic()

    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self._started_at_monotonic)

    async def start_run(self, prompt: str, *, owner_key: str = "global") -> Job:
        return await self._start_job(command="run", prompt=prompt, owner_key=owner_key)

    async def start_autopilot(self, task: str, *, owner_key: str = "global") -> Job:
        return await self._start_job(command="autopilot", prompt=task, owner_key=owner_key)

    async def start_codex(self, raw_args: str, *, owner_key: str = "global") -> Job:
        return await self._start_job(command="codex", prompt=raw_args, owner_key=owner_key)

    async def run_chat_turn(
        self,
        *,
        prompt: str,
        thread_id: str | None = None,
        event_callback: ChatEventCallback | None = None,
        owner_key: str = "global",
    ) -> ChatTurnResult:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("Chat prompt must not be empty")
        resume_thread_id = thread_id.strip() if thread_id and thread_id.strip() else None

        async with self._lock:
            active = self._active_execution_for_owner_locked(owner_key)
            if active is not None and active.process.returncode is None:
                raise ActiveJobExistsError("An active job is already running")

        fallback_codex_command = (self.settings.codex_command_fallback or "").strip()
        codex_commands = [self.settings.codex_command]
        if fallback_codex_command and fallback_codex_command != self.settings.codex_command:
            codex_commands.append(fallback_codex_command)

        max_attempts = max(1, self.settings.chat_turn_retry_count + 1)
        for codex_index, codex_command in enumerate(codex_commands):
            current_thread_id = resume_thread_id
            for attempt in range(1, max_attempts + 1):
                use_live_core = bool((self.settings.codex_live_core_command or "").strip())
                output_path: Path | None = None
                if current_thread_id is None and not use_live_core:
                    fd, output_path_raw = tempfile.mkstemp(prefix="telegram-codex-chat-", suffix=".txt")
                    os.close(fd)
                    output_path = Path(output_path_raw)
                if use_live_core:
                    argv = self._build_live_core_chat_argv(
                        prompt=clean_prompt,
                        thread_id=current_thread_id,
                        codex_command=codex_command,
                    )
                else:
                    argv = self._build_chat_argv(
                        prompt=clean_prompt,
                        output_path=output_path,
                        thread_id=current_thread_id,
                        codex_command=codex_command,
                    )
                try:
                    result = await self._run_chat_turn_attempt(
                        argv=argv,
                        output_path=output_path,
                        resume_thread_id=current_thread_id,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        event_callback=event_callback,
                    )
                    if (
                        codex_index == 0
                        and len(codex_commands) > 1
                        and is_dns_network_restriction_error(result.assistant_text)
                    ):
                        self.store.add_event(
                            None,
                            "chat_turn_fallback_retry",
                            "Retrying chat turn via fallback command after DNS-restriction assistant response",
                        )
                        break
                    if codex_index == 1:
                        self.store.add_event(None, "chat_turn_fallback_succeeded", "Fallback command succeeded")
                    return result
                except _ChatTurnTimeoutError as exc:
                    if attempt >= max_attempts:
                        raise RuntimeError("Chat turn timed out") from exc
                    if self.settings.chat_turn_reset_session_on_timeout:
                        current_thread_id = None
                    elif current_thread_id is None and exc.thread_id:
                        current_thread_id = exc.thread_id
                except RuntimeError as exc:
                    if codex_index == 0 and len(codex_commands) > 1 and is_dns_network_restriction_error(str(exc)):
                        self.store.add_event(None, "chat_turn_fallback_retry", "Retrying chat turn via fallback command")
                        break
                    raise
                finally:
                    if output_path is not None:
                        try:
                            output_path.unlink()
                        except FileNotFoundError:
                            pass
        raise RuntimeError("Chat turn timed out")

    async def wait_for_current_job(self, *, owner_key: str = "global") -> None:
        if owner_key == "global":
            task = self._job_task
        else:
            execution = self._owned_executions.get(owner_key)
            task = execution.job_task if execution is not None else None
        if task:
            await task

    async def cancel_active_job(self, *, owner_key: str = "global") -> bool:
        deadline = time.monotonic() + CANCEL_SLA_SECONDS
        finished_task: asyncio.Task[None] | None = None
        async with self._lock:
            active = self._active_execution_for_owner_locked(owner_key)
            process = active.process if active is not None else None
            job_id = active.job_id if active is not None else None
            job_task = active.job_task if active is not None else None
            if process is not None:
                if process.returncode is None:
                    self._set_cancel_requested_locked(owner_key, True)
                    orphan_job = None
                else:
                    orphan_job = None
                    finished_task = job_task
                    process = None
                    job_id = None
                    job_task = None
            else:
                orphan_job = self.store.get_active_job(owner_key=owner_key)
                if orphan_job is None or orphan_job.status != "RUNNING" or orphan_job.pid is None:
                    return False
                process = None
                job_id = orphan_job.id
                job_task = None

        if finished_task is not None:
            try:
                await asyncio.wait_for(finished_task, timeout=2.0)
            except asyncio.TimeoutError:
                pass
            return False

        if job_id is not None:
            self.store.add_event(job_id, "cancel_requested", "Cancellation requested")

        if process is not None:
            remaining = max(0.0, deadline - time.monotonic())
            terminated = await self._cancel_process(process, timeout_budget=remaining)
            if not terminated and job_id is not None:
                self.store.add_event(
                    job_id,
                    "cancel_sla_miss",
                    f"Process did not exit within {CANCEL_SLA_SECONDS:.1f}s cancellation window",
                )
            remaining = max(0.0, deadline - time.monotonic())
            if job_task and remaining > 0:
                try:
                    await asyncio.wait_for(job_task, timeout=remaining)
                except asyncio.TimeoutError:
                    if job_id is not None:
                        self.store.add_event(
                            job_id,
                            "cancel_sla_miss",
                            "Cancellation requested but monitor task did not complete in SLA window",
                        )
            return True

        assert orphan_job is not None
        return await self._cancel_orphan_job(
            job_id=orphan_job.id,
            pid=orphan_job.pid,
            pid_start_token=orphan_job.pid_start_token,
            timeout_budget=max(0.0, deadline - time.monotonic()),
            owner_key=owner_key,
        )

    async def _start_job(self, *, command: str, prompt: str, owner_key: str = "global") -> Job:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("Prompt/task must not be empty")

        async with self._lock:
            active = self._active_execution_for_owner_locked(owner_key)
            if active is not None and active.process.returncode is None:
                raise ActiveJobExistsError("An active job is already running")

            job = self.store.create_job(command=command, prompt=clean_prompt, status="RUNNING", owner_key=owner_key)
            argv = self._build_argv(command, clean_prompt, codex_command=self.settings.codex_command)

            try:
                process = await self._spawn_process(argv)
            except Exception as exc:
                self.store.set_job_status(job.id, "FAILED", error=str(exc))
                self.store.add_event(job.id, "spawn_failed", redact_text(str(exc)))
                raise

            if owner_key == "global":
                self._process = process
                self._active_job_id = job.id
                self._cancel_requested = False
            self.store.set_job_pid(
                job.id,
                process.pid,
                pid_start_token=self._read_pid_start_token(process.pid),
            )
            self.store.add_event(job.id, "job_started", f"command={command}")
            job_task = asyncio.create_task(
                self._monitor_job(job.id, process, command=command, prompt=clean_prompt, owner_key=owner_key)
            )
            if owner_key == "global":
                self._job_task = job_task
            else:
                self._owned_executions[owner_key] = _OwnedExecution(
                    process=process,
                    job_task=job_task,
                    job_id=job.id,
                )

        await self._safe_notify(
            f"Started job #{job.id}: {command} {clean_prompt[:80]}".rstrip(),
            job_id=job.id,
        )
        return job

    def _active_execution_for_owner_locked(self, owner_key: str) -> _OwnedExecution | None:
        if owner_key == "global":
            if self._process is None or self._job_task is None or self._active_job_id is None:
                return None
            return _OwnedExecution(
                process=self._process,
                job_task=self._job_task,
                job_id=self._active_job_id,
                cancel_requested=self._cancel_requested,
            )
        return self._owned_executions.get(owner_key)

    def _set_cancel_requested_locked(self, owner_key: str, value: bool) -> None:
        if owner_key == "global":
            self._cancel_requested = value
            return
        execution = self._owned_executions.get(owner_key)
        if execution is not None:
            execution.cancel_requested = value

    def _cancel_requested_for_owner(self, owner_key: str) -> bool:
        if owner_key == "global":
            return self._cancel_requested
        execution = self._owned_executions.get(owner_key)
        return execution.cancel_requested if execution is not None else False

    def _build_argv(self, command: str, prompt: str, *, codex_command: str | None = None) -> list[str]:
        executable = self.settings.codex_command if codex_command is None else codex_command
        if command == "run":
            return [executable, "--", prompt]
        if command == "autopilot":
            return [executable, "--", f"$autopilot {prompt}"]
        if command == "codex":
            parsed_args = self._parse_codex_args(prompt)
            return [executable, *parsed_args]
        raise ValueError(f"Unsupported command: {command}")

    def _fallback_codex_command(self) -> str | None:
        fallback = (self.settings.codex_command_fallback or "").strip()
        if not fallback:
            return None
        if fallback == self.settings.codex_command:
            return None
        return fallback

    def _build_chat_argv(
        self,
        *,
        prompt: str,
        output_path: Path | None,
        thread_id: str | None,
        codex_command: str | None = None,
    ) -> list[str]:
        executable = self.settings.codex_command if codex_command is None else codex_command
        if thread_id:
            return [
                executable,
                "exec",
                "resume",
                "--json",
                thread_id,
                "--",
                prompt,
            ]
        if output_path is None:
            raise ValueError("output_path is required for new chat sessions")
        return [
            executable,
            "exec",
            "--json",
            "-o",
            str(output_path),
            "--",
            prompt,
        ]

    def _build_live_core_chat_argv(
        self,
        *,
        prompt: str,
        thread_id: str | None,
        codex_command: str | None = None,
    ) -> list[str]:
        live_core_command = (self.settings.codex_live_core_command or "").strip()
        if not live_core_command:
            raise ValueError("CODEX_LIVE_CORE_COMMAND is not configured")
        try:
            argv = shlex.split(live_core_command, posix=True)
        except ValueError as exc:
            raise ValueError(f"Invalid CODEX_LIVE_CORE_COMMAND: {exc}") from exc
        if not argv:
            raise ValueError("CODEX_LIVE_CORE_COMMAND is not configured")

        executable = self.settings.codex_command if codex_command is None else codex_command
        argv.extend(
            [
                "--workspace-root",
                str(self.settings.workspace_root),
                "--codex-bin",
                executable,
            ]
        )
        if thread_id:
            argv.extend(["resume", thread_id, "--", prompt])
            return argv
        argv.extend(["exec", "--", prompt])
        return argv

    @staticmethod
    def _parse_codex_args(raw_args: str) -> list[str]:
        try:
            parsed = shlex.split(raw_args, posix=True)
        except ValueError as exc:
            raise ValueError(f"Invalid /codex arguments: {exc}") from exc
        if not parsed:
            raise ValueError("Usage: /codex <raw codex args>")
        return parsed

    async def _spawn_process(self, argv: list[str]) -> asyncio.subprocess.Process:
        # Must remain argv-only (`exec`) and never shell interpolation.
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.settings.workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.settings.subprocess_env(),
            start_new_session=True,
        )

    async def _run_chat_turn_attempt(
        self,
        *,
        argv: list[str],
        output_path: Path | None,
        resume_thread_id: str | None,
        attempt: int,
        max_attempts: int,
        event_callback: ChatEventCallback | None,
    ) -> ChatTurnResult:
        process = await self._spawn_process(argv)
        attempt_output = await self._collect_chat_attempt_output(
            process,
            resume_thread_id=resume_thread_id,
            attempt=attempt,
            max_attempts=max_attempts,
            event_callback=event_callback,
        )
        all_text = f"{attempt_output.stdout_text}\n{attempt_output.stderr_text}"
        resolved_thread_id = self._extract_thread_id_from_jsonl(all_text) or resume_thread_id
        assistant_text = self._extract_assistant_text_from_jsonl(all_text)
        if not assistant_text and output_path is not None:
            assistant_text = self._read_chat_output(output_path)

        if attempt_output.timeout_kind is not None:
            self._record_chat_turn_timeout_event(
                timeout_kind=attempt_output.timeout_kind,
                attempt=attempt,
                max_attempts=max_attempts,
                thread_id=resolved_thread_id,
                assistant_text=assistant_text,
            )
            raise _ChatTurnTimeoutError(thread_id=resolved_thread_id)

        if process.returncode != 0:
            detail = redact_text(
                attempt_output.stderr_text.strip()
                or attempt_output.stdout_text.strip()
                or f"exit={process.returncode}"
            )
            raise RuntimeError(f"Chat turn failed: {detail}")

        if not resolved_thread_id:
            raise RuntimeError("Chat turn missing thread.started thread_id")
        if not assistant_text:
            assistant_text = "No response."
        return ChatTurnResult(thread_id=resolved_thread_id, assistant_text=assistant_text)

    async def _collect_chat_attempt_output(
        self,
        process: asyncio.subprocess.Process,
        *,
        resume_thread_id: str | None,
        attempt: int,
        max_attempts: int,
        event_callback: ChatEventCallback | None,
    ) -> _ChatAttemptOutput:
        started_at = time.monotonic()
        last_progress_at = started_at
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        status_check_attempt = 0

        async def _read_stream(stream: asyncio.StreamReader | None, sink: list[str]) -> None:
            nonlocal last_progress_at
            if stream is None:
                return
            while True:
                raw_line = await stream.readline()
                if not raw_line:
                    break
                line = raw_line.decode(errors="replace")
                sink.append(line)
                if event_callback is not None:
                    for event in parse_execution_events([line]):
                        await event_callback(event)
                if self._is_chat_progress_event_line(line):
                    last_progress_at = time.monotonic()

        stdout_task = asyncio.create_task(_read_stream(process.stdout, stdout_chunks))
        stderr_task = asyncio.create_task(_read_stream(process.stderr, stderr_chunks))
        timeout_kind: str | None = None

        try:
            while process.returncode is None:
                now = time.monotonic()
                absolute_elapsed = now - started_at
                idle_elapsed = now - last_progress_at
                if absolute_elapsed >= float(self.settings.chat_turn_timeout_seconds):
                    timeout_kind = "absolute"
                    break
                if idle_elapsed >= float(self.settings.chat_turn_progress_timeout_seconds):
                    status_check_attempt += 1
                    observed_thread_id = self._extract_thread_id_from_jsonl(
                        f"{''.join(stdout_chunks)}\n{''.join(stderr_chunks)}"
                    )
                    thread_id = observed_thread_id or resume_thread_id
                    process_alive = self._is_chat_turn_process_alive(process)
                    self._record_chat_turn_status_check_event(
                        attempt=attempt,
                        max_attempts=max_attempts,
                        status_check_attempt=status_check_attempt,
                        elapsed_seconds=absolute_elapsed,
                        idle_elapsed_seconds=idle_elapsed,
                        thread_id=thread_id,
                        process_alive=process_alive,
                    )
                    if not process_alive:
                        timeout_kind = "idle"
                        break
                    last_progress_at = now
                    continue

                absolute_remaining = float(self.settings.chat_turn_timeout_seconds) - absolute_elapsed
                poll_timeout = min(absolute_remaining, CHAT_TURN_POLL_INTERVAL_SECONDS)
                if poll_timeout <= 0:
                    continue
                try:
                    await asyncio.wait_for(process.wait(), timeout=poll_timeout)
                except asyncio.TimeoutError:
                    continue

            if timeout_kind is not None:
                await self._cancel_process(process)
            else:
                await process.wait()
        finally:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        return _ChatAttemptOutput(
            stdout_text="".join(stdout_chunks),
            stderr_text="".join(stderr_chunks),
            timeout_kind=timeout_kind,
        )

    def _record_chat_turn_status_check_event(
        self,
        *,
        attempt: int,
        max_attempts: int,
        status_check_attempt: int,
        elapsed_seconds: float,
        idle_elapsed_seconds: float,
        thread_id: str | None,
        process_alive: bool,
    ) -> None:
        thread_value = thread_id or "unknown"
        message = (
            f"attempt={attempt}/{max_attempts} status_check={status_check_attempt} "
            f"elapsed={elapsed_seconds:.2f}s idle_elapsed={idle_elapsed_seconds:.2f}s "
            f"thread_id={thread_value} process_alive={str(process_alive).lower()}"
        )
        self.store.add_event(None, "chat_turn_status_check", redact_text(message))

    def _is_chat_turn_process_alive(self, process: asyncio.subprocess.Process) -> bool:
        if process.returncode is not None:
            return False
        if process.pid is None:
            return True
        if self._pid_is_alive(process.pid):
            return True
        # Some test doubles do not map pid values to OS processes.
        return process.returncode is None

    def _record_chat_turn_timeout_event(
        self,
        *,
        timeout_kind: str,
        attempt: int,
        max_attempts: int,
        thread_id: str | None,
        assistant_text: str,
    ) -> None:
        partial_text = self._truncate_timeout_partial_text(assistant_text)
        partial_len = len(assistant_text.strip())
        thread_value = thread_id or "unknown"
        message = (
            f"attempt={attempt}/{max_attempts} kind={timeout_kind} "
            f"timeout={self.settings.chat_turn_timeout_seconds}s "
            f"progress_timeout={self.settings.chat_turn_progress_timeout_seconds}s "
            f"thread_id={thread_value} partial_len={partial_len} "
            f"partial_assistant={partial_text or '<none>'}"
        )
        self.store.add_event(None, "chat_turn_timeout_partial", redact_text(message))

    @staticmethod
    def _truncate_timeout_partial_text(text: str) -> str:
        collapsed = " ".join(text.split())
        if len(collapsed) <= CHAT_TIMEOUT_PARTIAL_TEXT_MAX_CHARS:
            return collapsed
        return collapsed[: CHAT_TIMEOUT_PARTIAL_TEXT_MAX_CHARS - 3] + "..."

    @staticmethod
    def _is_chat_progress_event_line(raw_line: str) -> bool:
        line = raw_line.strip()
        if not line:
            return False
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False
        event_name = payload.get("event_type") or payload.get("type") or payload.get("event")
        return isinstance(event_name, str) and bool(event_name.strip())

    @staticmethod
    def _extract_thread_id_from_jsonl(jsonl_text: str) -> str | None:
        thread_id: str | None = None
        for raw_line in jsonl_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event_name = payload.get("event_type") or payload.get("type") or payload.get("event")
            if event_name == "session":
                event_thread_id = payload.get("thread_id")
                if isinstance(event_thread_id, str) and event_thread_id.strip():
                    thread_id = event_thread_id.strip()
                continue
            if event_name != "thread.started":
                continue
            event_thread_id = payload.get("thread_id")
            if isinstance(event_thread_id, str) and event_thread_id.strip():
                thread_id = event_thread_id.strip()
                continue
            thread_obj = payload.get("thread")
            if isinstance(thread_obj, dict):
                nested = thread_obj.get("id") or thread_obj.get("thread_id")
                if isinstance(nested, str) and nested.strip():
                    thread_id = nested.strip()
        return thread_id

    @staticmethod
    def _read_chat_output(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @classmethod
    def _extract_assistant_text_from_jsonl(cls, jsonl_text: str) -> str:
        deltas: list[str] = []
        candidates: list[str] = []

        for raw_line in jsonl_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue

            event_name = payload.get("event_type") or payload.get("type") or payload.get("event")
            if event_name == "text_delta":
                delta = payload.get("message")
                if isinstance(delta, str) and delta:
                    deltas.append(delta)
                    continue
            if event_name == "text_done":
                text = payload.get("message")
                if isinstance(text, str) and text.strip():
                    candidates.append(text.strip())
                    continue
            if event_name == "response.output_text.delta":
                delta = payload.get("delta")
                if isinstance(delta, str) and delta:
                    deltas.append(delta)
                    continue
            if event_name == "response.output_text.done":
                text = payload.get("text")
                if isinstance(text, str) and text.strip():
                    candidates.append(text.strip())
                    continue

            if event_name == "item.completed":
                item = payload.get("item")
                text = cls._extract_assistant_text_from_item(item)
                if text:
                    candidates.append(text)
                    continue

            if event_name == "response.completed":
                response = payload.get("response")
                text = cls._extract_assistant_text_from_response(response)
                if text:
                    candidates.append(text)

        if deltas:
            text = "".join(deltas).strip()
            if text:
                return text
        if candidates:
            return candidates[-1]
        return ""

    @classmethod
    def _extract_assistant_text_from_item(cls, item: object) -> str | None:
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type", "")).strip().lower()
        role = str(item.get("role", "")).strip().lower()
        if item_type == "error":
            return None
        if role == "assistant" or item_type in {"assistant", "assistant_message", "agent_message", "message"}:
            text = cls._extract_text_from_content(item.get("content"))
            if text:
                return text
            direct = item.get("text")
            if isinstance(direct, str) and direct.strip():
                return direct.strip()
        return None

    @classmethod
    def _extract_assistant_text_from_response(cls, response: object) -> str | None:
        if not isinstance(response, dict):
            return None
        output = response.get("output")
        if not isinstance(output, list):
            return None

        for item in reversed(output):
            text = cls._extract_assistant_text_from_item(item)
            if text:
                return text
        return None

    @staticmethod
    def _extract_text_from_content(content: object) -> str | None:
        if not isinstance(content, list):
            return None
        texts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type", "")).strip().lower()
            if part_type not in {"text", "output_text"}:
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
                continue
            if isinstance(text, dict):
                nested = text.get("value")
                if isinstance(nested, str) and nested.strip():
                    texts.append(nested.strip())
                continue
            value = part.get("value")
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
        if not texts:
            return None
        return "\n".join(texts)

    async def _monitor_job(
        self,
        job_id: int,
        process: asyncio.subprocess.Process,
        *,
        command: str,
        prompt: str,
        owner_key: str,
    ) -> None:
        fallback_command = self._fallback_codex_command()
        fallback_attempted = False
        current_process = process

        while True:
            recent_stdout: list[str] = []
            recent_stderr: list[str] = []
            stdout_task = asyncio.create_task(
                self._stream_output(job_id, "stdout", current_process.stdout, capture_buffer=recent_stdout)
            )
            stderr_task = asyncio.create_task(
                self._stream_output(job_id, "stderr", current_process.stderr, capture_buffer=recent_stderr)
            )
            timed_out = False

            try:
                await asyncio.wait_for(current_process.wait(), timeout=float(self.settings.job_timeout_seconds))
            except asyncio.TimeoutError:
                timed_out = True
                self.store.add_event(job_id, "job_timeout", "Job timed out")
                await self._cancel_process(current_process)

            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

            if timed_out:
                status = "TIMED_OUT"
            elif self._cancel_requested_for_owner(owner_key):
                status = "CANCELLED"
            elif current_process.returncode == 0:
                status = "SUCCEEDED"
            else:
                status = "FAILED"

            diagnostic_text = "\n".join(recent_stderr[-30:] + recent_stdout[-10:])
            is_network_restricted = is_dns_network_restriction_error(diagnostic_text)
            should_retry_with_fallback = (
                is_network_restricted
                and status in {"FAILED", "SUCCEEDED"}
                and not fallback_attempted
                and fallback_command is not None
                and not self._cancel_requested_for_owner(owner_key)
            )

            if should_retry_with_fallback:
                self.store.add_event(job_id, "job_network_restricted", "Detected DNS/network restriction pattern")
                self.store.add_event(job_id, "job_fallback_retry", "Retrying with CODEX_COMMAND_FALLBACK")
                await self._safe_notify(
                    "Primary execution failed due to DNS/network restrictions. Retrying with fallback command.",
                    job_id=job_id,
                )
                try:
                    fallback_argv = self._build_argv(command, prompt, codex_command=fallback_command)
                    replacement = await self._spawn_process(fallback_argv)
                except Exception as exc:
                    self.store.add_event(job_id, "job_fallback_spawn_failed", redact_text(str(exc)))
                    await self._safe_notify(
                        f"Fallback command failed to start: {redact_text(str(exc))}",
                        job_id=job_id,
                    )
                else:
                    fallback_attempted = True
                    current_process = replacement
                    async with self._lock:
                        if owner_key == "global" and self._active_job_id == job_id:
                            self._process = replacement
                            self._cancel_requested = False
                        elif owner_key != "global":
                            execution = self._owned_executions.get(owner_key)
                            if execution is not None and execution.job_id == job_id:
                                execution.process = replacement
                                execution.cancel_requested = False
                    self.store.set_job_pid(
                        job_id,
                        replacement.pid,
                        pid_start_token=self._read_pid_start_token(replacement.pid),
                    )
                    self.store.add_event(job_id, "job_fallback_started", f"pid={replacement.pid}")
                    continue

            self.store.set_job_status(job_id, status, exit_code=current_process.returncode)
            self.store.add_event(job_id, "job_finished", f"status={status} exit_code={current_process.returncode}")
            if is_network_restricted:
                self.store.add_event(job_id, "job_network_restricted", "Detected DNS/network restriction pattern")
                await self._safe_notify(
                    build_dns_network_restriction_guidance(),
                    job_id=job_id,
                )
            await self._safe_notify(
                f"Job #{job_id} finished: {status} (exit={current_process.returncode})",
                job_id=job_id,
            )

            async with self._lock:
                if owner_key == "global" and self._active_job_id == job_id:
                    self._process = None
                    self._active_job_id = None
                    self._job_task = None
                    self._cancel_requested = False
                elif owner_key != "global":
                    execution = self._owned_executions.get(owner_key)
                    if execution is not None and execution.job_id == job_id:
                        del self._owned_executions[owner_key]
            return

    async def _stream_output(
        self,
        job_id: int,
        stream_name: str,
        stream: asyncio.StreamReader | None,
        *,
        capture_buffer: list[str] | None = None,
    ) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            text = redact_text(line.decode(errors="replace").rstrip())
            if not text:
                continue
            if capture_buffer is not None:
                capture_buffer.append(text)
                if len(capture_buffer) > 100:
                    del capture_buffer[: len(capture_buffer) - 100]
            self.store.add_event(job_id, f"process_{stream_name}", text)
            await self._safe_notify(f"[{stream_name}] {text}", job_id=job_id)

    async def _notify(self, text: str, *, job_id: int | None = None) -> None:
        if self._notifier is None:
            return
        safe_text = redact_text(text)
        for chunk in chunk_text(safe_text, max_size=self.settings.message_chunk_size):
            if chunk:
                await self._notifier(RunnerNotification(text=chunk, job_id=job_id))

    async def _safe_notify(self, text: str, *, job_id: int | None = None) -> None:
        try:
            await self._notify(text, job_id=job_id)
        except Exception as exc:
            self.store.add_event(
                job_id,
                "notify_error",
                redact_text(str(exc)),
            )

    async def _cancel_process(
        self,
        process: asyncio.subprocess.Process,
        *,
        timeout_budget: float = CANCEL_SLA_SECONDS,
    ) -> bool:
        if process.returncode is not None:
            return True

        deadline = time.monotonic() + max(0.0, timeout_budget)
        stage_timeout = lambda cap: min(cap, max(0.0, deadline - time.monotonic()))  # noqa: E731

        self._signal_process_group(process, signal.SIGINT)
        if await self._wait_exit(process, stage_timeout(5.0)):
            return True

        self._signal_process_group(process, signal.SIGTERM)
        if await self._wait_exit(process, stage_timeout(7.0)):
            return True

        self._signal_process_group(process, signal.SIGKILL)
        await self._wait_exit(process, max(0.0, deadline - time.monotonic()))
        if process.returncode is None:
            process.kill()
            await self._wait_exit(process, max(0.0, deadline - time.monotonic()))
        return process.returncode is not None

    async def _wait_exit(self, process: asyncio.subprocess.Process, timeout: float) -> bool:
        if timeout <= 0:
            return process.returncode is not None
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _cancel_orphan_job(
        self,
        *,
        job_id: int,
        pid: int,
        pid_start_token: str | None,
        timeout_budget: float,
        owner_key: str,
    ) -> bool:
        if not self._pid_is_alive(pid):
            self.store.set_job_status(
                job_id,
                "INTERRUPTED_RECOVERED",
                error=f"Orphan pid={pid} was already not alive during cancel",
            )
            self.store.add_event(
                job_id,
                "orphan_already_not_alive",
                f"pid={pid}",
            )
            await self._safe_notify(
                f"Job #{job_id} recovered: orphan pid={pid} was already stopped",
                job_id=job_id,
            )
            return True

        if not self._pid_matches_token(pid, pid_start_token):
            self.store.set_job_status(
                job_id,
                "INTERRUPTED_RECOVERED",
                error=f"Orphan pid identity mismatch for pid={pid}; did not signal unknown process",
            )
            self.store.add_event(
                job_id,
                "cancel_blocked_identity_mismatch",
                f"pid={pid} start_token={pid_start_token}",
            )
            await self._safe_notify(
                f"Job #{job_id} cancellation blocked: pid identity mismatch for pid={pid}",
                job_id=job_id,
            )
            return False

        terminated = await self._cancel_pid_group(pid, timeout_budget=timeout_budget)
        if terminated:
            self.store.set_job_status(job_id, "CANCELLED")
            self.store.add_event(job_id, "job_finished", "status=CANCELLED exit_code=None recovered_orphan=true")
            await self._safe_notify(
                f"Job #{job_id} finished: CANCELLED (orphan pid={pid})",
                job_id=job_id,
            )
            async with self._lock:
                if owner_key == "global":
                    self._process = None
                    self._active_job_id = None
                    self._job_task = None
                    self._cancel_requested = False
                else:
                    self._owned_executions.pop(owner_key, None)
            return True

        self.store.add_event(
            job_id,
            "cancel_sla_miss",
            f"Unable to terminate orphan pid={pid} within {CANCEL_SLA_SECONDS:.1f}s",
        )
        await self._safe_notify(
            f"Job #{job_id} cancellation exceeded SLA; orphan pid={pid} may still be running",
            job_id=job_id,
        )
        return False

    async def _cancel_pid_group(self, pid: int, *, timeout_budget: float) -> bool:
        if pid <= 0 or not self._pid_is_alive(pid):
            return True

        deadline = time.monotonic() + max(0.0, timeout_budget)
        stage_timeout = lambda cap: min(cap, max(0.0, deadline - time.monotonic()))  # noqa: E731

        self._signal_pid_group(pid, signal.SIGINT)
        if await self._wait_pid_exit(pid, stage_timeout(5.0)):
            return True

        self._signal_pid_group(pid, signal.SIGTERM)
        if await self._wait_pid_exit(pid, stage_timeout(7.0)):
            return True

        self._signal_pid_group(pid, signal.SIGKILL)
        await self._wait_pid_exit(pid, max(0.0, deadline - time.monotonic()))
        return not self._pid_is_alive(pid)

    async def _wait_pid_exit(self, pid: int, timeout: float) -> bool:
        if timeout <= 0:
            return not self._pid_is_alive(pid)

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self._pid_is_alive(pid):
                return True
            await asyncio.sleep(0.1)
        return not self._pid_is_alive(pid)

    def _signal_process_group(self, process: asyncio.subprocess.Process, signum: int) -> None:
        pid = process.pid
        if pid:
            try:
                os.killpg(pid, signum)
                return
            except ProcessLookupError:
                return
            except PermissionError:
                pass
            except OSError:
                pass
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            return

    def _signal_pid_group(self, pid: int, signum: int) -> None:
        try:
            os.killpg(pid, signum)
        except ProcessLookupError:
            return
        except PermissionError:
            try:
                os.kill(pid, signum)
            except OSError:
                return
        except OSError:
            try:
                os.kill(pid, signum)
            except OSError:
                return

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _read_pid_start_token(pid: int | None) -> str | None:
        if not isinstance(pid, int) or pid <= 0:
            return None
        try:
            output = subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        token = output.strip()
        return token or None

    @classmethod
    def _pid_matches_token(cls, pid: int, expected_token: str | None) -> bool:
        if not expected_token:
            return False
        current_token = cls._read_pid_start_token(pid)
        return current_token == expected_token
