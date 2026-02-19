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
from .store import ActiveJobExistsError, Job, Store
from .utils import chunk_text, redact_text


Notifier = Callable[[str], Awaitable[None]]
CANCEL_SLA_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class ChatTurnResult:
    thread_id: str
    assistant_text: str


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
        self._notifier: Notifier | None = None
        self._started_at_monotonic = time.monotonic()

    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self._started_at_monotonic)

    async def start_run(self, prompt: str) -> Job:
        return await self._start_job(command="run", prompt=prompt)

    async def start_autopilot(self, task: str) -> Job:
        return await self._start_job(command="autopilot", prompt=task)

    async def start_codex(self, raw_args: str) -> Job:
        return await self._start_job(command="codex", prompt=raw_args)

    async def run_chat_turn(self, *, prompt: str, thread_id: str | None = None) -> ChatTurnResult:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("Chat prompt must not be empty")
        resume_thread_id = thread_id.strip() if thread_id and thread_id.strip() else None

        async with self._lock:
            if self._process is not None and self._process.returncode is None:
                raise ActiveJobExistsError("An active job is already running")

        output_path: Path | None = None
        if resume_thread_id is None:
            fd, output_path_raw = tempfile.mkstemp(prefix="telegram-codex-chat-", suffix=".txt")
            os.close(fd)
            output_path = Path(output_path_raw)
        argv = self._build_chat_argv(prompt=clean_prompt, output_path=output_path, thread_id=resume_thread_id)
        try:
            process = await self._spawn_process(argv)
            try:
                stdout_raw, stderr_raw = await asyncio.wait_for(
                    process.communicate(),
                    timeout=float(self.settings.job_timeout_seconds),
                )
            except asyncio.TimeoutError as exc:
                await self._cancel_process(process)
                raise RuntimeError("Chat turn timed out") from exc

            stdout_text = stdout_raw.decode(errors="replace")
            stderr_text = stderr_raw.decode(errors="replace")
            if process.returncode != 0:
                detail = redact_text(stderr_text.strip() or stdout_text.strip() or f"exit={process.returncode}")
                raise RuntimeError(f"Chat turn failed: {detail}")

            all_text = f"{stdout_text}\n{stderr_text}"
            resolved_thread_id = self._extract_thread_id_from_jsonl(all_text) or resume_thread_id
            if not resolved_thread_id:
                raise RuntimeError("Chat turn missing thread.started thread_id")

            assistant_text = self._extract_assistant_text_from_jsonl(all_text)
            if not assistant_text and output_path is not None:
                assistant_text = self._read_chat_output(output_path)
            if not assistant_text:
                assistant_text = "No response."
            return ChatTurnResult(thread_id=resolved_thread_id, assistant_text=assistant_text)
        finally:
            if output_path is not None:
                try:
                    output_path.unlink()
                except FileNotFoundError:
                    pass

    async def wait_for_current_job(self) -> None:
        task = self._job_task
        if task:
            await task

    async def cancel_active_job(self) -> bool:
        deadline = time.monotonic() + CANCEL_SLA_SECONDS
        finished_task: asyncio.Task[None] | None = None
        async with self._lock:
            process = self._process
            job_id = self._active_job_id
            job_task = self._job_task
            if process is not None:
                if process.returncode is None:
                    self._cancel_requested = True
                    orphan_job = None
                else:
                    orphan_job = None
                    finished_task = job_task
                    process = None
                    job_id = None
                    job_task = None
            else:
                orphan_job = self.store.get_active_job()
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
        )

    async def _start_job(self, *, command: str, prompt: str) -> Job:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("Prompt/task must not be empty")

        async with self._lock:
            if self._process is not None and self._process.returncode is None:
                raise ActiveJobExistsError("An active job is already running")

            job = self.store.create_job(command=command, prompt=clean_prompt, status="RUNNING")
            argv = self._build_argv(command, clean_prompt)

            try:
                process = await self._spawn_process(argv)
            except Exception as exc:
                self.store.set_job_status(job.id, "FAILED", error=str(exc))
                self.store.add_event(job.id, "spawn_failed", redact_text(str(exc)))
                raise

            self._process = process
            self._active_job_id = job.id
            self._cancel_requested = False
            self.store.set_job_pid(
                job.id,
                process.pid,
                pid_start_token=self._read_pid_start_token(process.pid),
            )
            self.store.add_event(job.id, "job_started", f"command={command}")
            self._job_task = asyncio.create_task(self._monitor_job(job.id, process))

        await self._safe_notify(
            f"Started job #{job.id}: {command} {clean_prompt[:80]}".rstrip(),
            job_id=job.id,
        )
        return job

    def _build_argv(self, command: str, prompt: str) -> list[str]:
        if command == "run":
            return [self.settings.codex_command, "--", prompt]
        if command == "autopilot":
            return [self.settings.codex_command, "--", f"$autopilot {prompt}"]
        if command == "codex":
            parsed_args = self._parse_codex_args(prompt)
            return [self.settings.codex_command, *parsed_args]
        raise ValueError(f"Unsupported command: {command}")

    def _build_chat_argv(self, *, prompt: str, output_path: Path | None, thread_id: str | None) -> list[str]:
        if thread_id:
            return [
                self.settings.codex_command,
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
            self.settings.codex_command,
            "exec",
            "--json",
            "-o",
            str(output_path),
            "--",
            prompt,
        ]

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
            event_name = payload.get("type") or payload.get("event")
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

            event_name = payload.get("type") or payload.get("event")
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
        if role == "assistant" or item_type in {"assistant", "assistant_message", "message"}:
            return cls._extract_text_from_content(item.get("content"))
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

    async def _monitor_job(self, job_id: int, process: asyncio.subprocess.Process) -> None:
        stdout_task = asyncio.create_task(self._stream_output(job_id, "stdout", process.stdout))
        stderr_task = asyncio.create_task(self._stream_output(job_id, "stderr", process.stderr))
        timed_out = False

        try:
            await asyncio.wait_for(process.wait(), timeout=float(self.settings.job_timeout_seconds))
        except asyncio.TimeoutError:
            timed_out = True
            self.store.add_event(job_id, "job_timeout", "Job timed out")
            await self._cancel_process(process)

        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        if timed_out:
            status = "TIMED_OUT"
        elif self._cancel_requested:
            status = "CANCELLED"
        elif process.returncode == 0:
            status = "SUCCEEDED"
        else:
            status = "FAILED"

        self.store.set_job_status(job_id, status, exit_code=process.returncode)
        self.store.add_event(job_id, "job_finished", f"status={status} exit_code={process.returncode}")
        await self._safe_notify(
            f"Job #{job_id} finished: {status} (exit={process.returncode})",
            job_id=job_id,
        )

        async with self._lock:
            if self._active_job_id == job_id:
                self._process = None
                self._active_job_id = None
                self._job_task = None
                self._cancel_requested = False

    async def _stream_output(
        self,
        job_id: int,
        stream_name: str,
        stream: asyncio.StreamReader | None,
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
            self.store.add_event(job_id, f"process_{stream_name}", text)
            await self._safe_notify(f"[{stream_name}] {text}", job_id=job_id)

    async def _notify(self, text: str) -> None:
        if self._notifier is None:
            return
        safe_text = redact_text(text)
        for chunk in chunk_text(safe_text, max_size=self.settings.message_chunk_size):
            if chunk:
                await self._notifier(chunk)

    async def _safe_notify(self, text: str, *, job_id: int | None = None) -> None:
        try:
            await self._notify(text)
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
