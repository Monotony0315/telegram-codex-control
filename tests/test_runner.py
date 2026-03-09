from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import asyncio
import signal
import sys

import pytest

from telegram_codex_control.config import Settings
from telegram_codex_control.live_events import ExecutionEvent
from telegram_codex_control.runner import Runner
from telegram_codex_control.store import ActiveJobExistsError, Store


class _TimedStream:
    def __init__(self, lines: list[tuple[float, str]] | None = None) -> None:
        self._lines = list(lines or [])

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        delay, payload = self._lines.pop(0)
        if delay > 0:
            await asyncio.sleep(delay)
        return payload.encode("utf-8")


class _ChatProcess:
    def __init__(
        self,
        *,
        pid: int,
        stdout_lines: list[tuple[float, str]] | None = None,
        stderr_lines: list[tuple[float, str]] | None = None,
        wait_delay_seconds: float = 0.0,
        returncode: int = 0,
    ) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._wait_delay_seconds = wait_delay_seconds
        self._final_returncode = returncode
        self._complete_at: float | None = None
        self.stdout = _TimedStream(stdout_lines)
        self.stderr = _TimedStream(stderr_lines)

    async def wait(self) -> int:
        if self.returncode is None and self._wait_delay_seconds > 0:
            loop = asyncio.get_running_loop()
            if self._complete_at is None:
                self._complete_at = loop.time() + self._wait_delay_seconds
            remaining = self._complete_at - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def send_signal(self, _sig: int) -> None:
        if self.returncode is None:
            self.returncode = 130

    def kill(self) -> None:
        self.returncode = -9


def test_runner_enforces_single_active_job(settings: Settings, store: Store) -> None:
    fake_script = Path(__file__).parent / "fakes" / "fake_codex.py"
    local_settings = replace(settings, codex_command=sys.executable, job_timeout_seconds=10)

    async def scenario() -> int:
        runner = Runner(local_settings, store)
        job = await runner.start_run(str(fake_script))
        with pytest.raises(ActiveJobExistsError):
            await runner.start_run(str(fake_script))
        assert await runner.cancel_active_job() is True
        await runner.wait_for_current_job()
        return job.id

    job_id = asyncio.run(scenario())
    terminal = store.get_job(job_id)
    assert terminal is not None
    assert terminal.status in {"CANCELLED", "SUCCEEDED"}


def test_runner_uses_exec_not_shell(monkeypatch: pytest.MonkeyPatch, settings: Settings, store: Store) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setenv("HOME", "/tmp/runner-home")

    class _FakeStream:
        async def readline(self) -> bytes:
            return b""

    class _FakeProcess:
        def __init__(self) -> None:
            self.pid = 4321
            self.returncode = None
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()

        async def wait(self) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def send_signal(self, _sig: int) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return _FakeProcess()

    async def fail_shell(*args, **kwargs):
        raise AssertionError("create_subprocess_shell must not be used")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fail_shell)

    async def scenario() -> None:
        runner = Runner(settings, store)
        await runner.start_run("hello")
        await runner.wait_for_current_job()

    asyncio.run(scenario())

    args = seen["args"]
    kwargs = seen["kwargs"]
    assert isinstance(args, tuple)
    assert args[0] == settings.codex_command
    assert args[1] == "--"
    assert args[2] == "hello"
    assert kwargs["cwd"] == str(settings.workspace_root)
    assert kwargs["env"]["HOME"] == "/tmp/runner-home"
    assert "shell" not in kwargs


def test_runner_allows_parallel_jobs_for_different_owners(settings: Settings, store: Store) -> None:
    fake_script = Path(__file__).parent / "fakes" / "fake_codex.py"
    local_settings = replace(settings, codex_command=sys.executable, job_timeout_seconds=10)

    async def scenario() -> None:
        runner = Runner(local_settings, store)
        job_a = await runner.start_run(str(fake_script), owner_key="chat:1")
        job_b = await runner.start_run(str(fake_script), owner_key="chat:2")
        assert job_a.id != job_b.id
        with pytest.raises(ActiveJobExistsError):
            await runner.start_run(str(fake_script), owner_key="chat:1")
        await runner.cancel_active_job(owner_key="chat:1")
        await runner.cancel_active_job(owner_key="chat:2")
        await runner.wait_for_current_job(owner_key="chat:1")
        await runner.wait_for_current_job(owner_key="chat:2")

    asyncio.run(scenario())


def test_runner_builds_raw_codex_argv(settings: Settings, store: Store) -> None:
    runner = Runner(settings, store)
    argv = runner._build_argv("codex", '--help --model gpt-5 "build feature"')
    assert argv == [settings.codex_command, "--help", "--model", "gpt-5", "build feature"]


def test_runner_rejects_invalid_raw_codex_argv(settings: Settings, store: Store) -> None:
    runner = Runner(settings, store)
    with pytest.raises(ValueError):
        runner._build_argv("codex", '"unterminated')


def test_runner_builds_chat_resume_argv(settings: Settings, store: Store) -> None:
    runner = Runner(settings, store)
    argv = runner._build_chat_argv(prompt="hello", output_path=None, thread_id="thread-123")
    assert argv == [
        settings.codex_command,
        "exec",
        "resume",
        "--json",
        "thread-123",
        "--",
        "hello",
    ]


def test_runner_builds_live_core_chat_argv(settings: Settings, store: Store) -> None:
    runner = Runner(replace(settings, codex_live_core_command="tgcc-live-core --fast"), store)
    argv = runner._build_live_core_chat_argv(prompt="hello", thread_id="thread-123")
    assert argv == [
        "tgcc-live-core",
        "--fast",
        "--workspace-root",
        str(settings.workspace_root),
        "--codex-bin",
        settings.codex_command,
        "resume",
        "thread-123",
        "--",
        "hello",
    ]


def test_runner_chat_turn_reads_output_and_thread_id(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    runner = Runner(settings, store)
    seen: dict[str, list[str]] = {}

    async def fake_spawn(argv: list[str]):
        seen["argv"] = argv
        output_path = Path(argv[argv.index("-o") + 1])
        output_path.write_text("Assistant reply text", encoding="utf-8")
        return _ChatProcess(
            pid=3333,
            stderr_lines=[(0.0, '{"type":"thread.started","thread_id":"thread-abc"}\n')],
        )

    monkeypatch.setattr(runner, "_spawn_process", fake_spawn)

    async def scenario() -> None:
        result = await runner.run_chat_turn(prompt="how are you?")
        assert result.thread_id == "thread-abc"
        assert result.assistant_text == "Assistant reply text"

    asyncio.run(scenario())
    argv = seen["argv"]
    assert argv[0] == settings.codex_command
    assert argv[1] == "exec"
    assert argv[2:4] == ["--json", "-o"]
    assert argv[4].endswith(".txt")
    assert argv[5] == "--"
    assert argv[6] == "how are you?"


def test_runner_chat_turn_resume_uses_existing_thread_when_no_started_event(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    runner = Runner(settings, store)
    seen: dict[str, list[str]] = {}

    async def fake_spawn(argv: list[str]):
        seen["argv"] = argv
        return _ChatProcess(
            pid=4444,
            stdout_lines=[(0.0, '{"type":"response.output_text.done","text":"Resumed response"}\n')],
        )

    monkeypatch.setattr(runner, "_spawn_process", fake_spawn)

    async def scenario() -> None:
        result = await runner.run_chat_turn(prompt="continue", thread_id="thread-prev")
        assert result.thread_id == "thread-prev"
        assert result.assistant_text == "Resumed response"

    asyncio.run(scenario())
    argv = seen["argv"]
    assert argv[:4] == [settings.codex_command, "exec", "resume", "--json"]
    assert argv[4] == "thread-prev"
    assert argv[5] == "--"
    assert argv[6] == "continue"


def test_runner_chat_turn_resume_extracts_agent_message_item_text(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    runner = Runner(settings, store)

    async def fake_spawn(_argv: list[str]):
        return _ChatProcess(
            pid=5555,
            stdout_lines=[
                (0.0, '{"type":"item.completed","item":{"id":"item_1","type":"reasoning","text":"thinking"}}\n'),
                (0.0, '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"파싱 성공"}}\n'),
                (0.0, '{"type":"turn.completed","usage":{"output_tokens":10}}\n'),
            ],
        )

    monkeypatch.setattr(runner, "_spawn_process", fake_spawn)

    async def scenario() -> None:
        result = await runner.run_chat_turn(prompt="resume", thread_id="thread-prev")
        assert result.thread_id == "thread-prev"
        assert result.assistant_text == "파싱 성공"

    asyncio.run(scenario())


def test_runner_chat_turn_emits_live_events(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    runner = Runner(settings, store)
    seen_events: list[ExecutionEvent] = []

    async def fake_spawn(_argv: list[str]):
        return _ChatProcess(
            pid=6666,
            stdout_lines=[
                (0.0, '{"type":"thread.started","thread_id":"thread-live"}\n'),
                (0.0, '{"type":"agent.updated","status":"running","message":"Thinking"}\n'),
                (0.0, '{"type":"response.output_text.delta","delta":"Hello"}\n'),
                (0.0, '{"type":"response.output_text.done","text":"Hello world"}\n'),
            ],
        )

    async def on_event(event: ExecutionEvent) -> None:
        seen_events.append(event)

    monkeypatch.setattr(runner, "_spawn_process", fake_spawn)

    async def scenario() -> None:
        result = await runner.run_chat_turn(prompt="hello", event_callback=on_event)
        assert result.thread_id == "thread-live"
        assert result.assistant_text == "Hello"

    asyncio.run(scenario())

    assert seen_events == [
        ExecutionEvent(event_type="session", thread_id="thread-live"),
        ExecutionEvent(event_type="status", status="running", message="Thinking"),
        ExecutionEvent(event_type="text_delta", message="Hello"),
        ExecutionEvent(event_type="text_done", message="Hello world"),
    ]


def test_runner_chat_turn_uses_live_core_helper_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    runner = Runner(replace(settings, codex_live_core_command="tgcc-live-core --fast"), store)
    seen: dict[str, list[str]] = {}
    seen_events: list[ExecutionEvent] = []

    async def fake_spawn(argv: list[str]):
        seen["argv"] = argv
        return _ChatProcess(
            pid=7777,
            stdout_lines=[
                (0.0, '{"event_type":"session","thread_id":"thread-helper"}\n'),
                (0.0, '{"event_type":"status","status":"running","message":"helper"}\n'),
                (0.0, '{"event_type":"text_done","message":"Hello from helper"}\n'),
                (0.0, '{"event_type":"done"}\n'),
            ],
        )

    async def on_event(event: ExecutionEvent) -> None:
        seen_events.append(event)

    monkeypatch.setattr(runner, "_spawn_process", fake_spawn)

    async def scenario() -> None:
        result = await runner.run_chat_turn(prompt="hello", event_callback=on_event)
        assert result.thread_id == "thread-helper"
        assert result.assistant_text == "Hello from helper"

    asyncio.run(scenario())

    assert seen["argv"] == [
        "tgcc-live-core",
        "--fast",
        "--workspace-root",
        str(settings.workspace_root),
        "--codex-bin",
        settings.codex_command,
        "exec",
        "--",
        "hello",
    ]
    assert seen_events == [
        ExecutionEvent(event_type="session", thread_id="thread-helper"),
        ExecutionEvent(event_type="status", status="running", message="helper"),
        ExecutionEvent(event_type="text_done", message="Hello from helper"),
        ExecutionEvent(event_type="done"),
    ]


def test_runner_chat_turn_retries_with_fallback_command_on_dns_restriction(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    local_settings = replace(settings, codex_command="primary-codex", codex_command_fallback="fallback-codex")
    runner = Runner(local_settings, store)
    seen_commands: list[str] = []

    async def fake_spawn(argv: list[str]):
        seen_commands.append(argv[0])
        if argv[0] == "primary-codex":
            return _ChatProcess(
                pid=7001,
                returncode=1,
                stderr_lines=[(0.0, "Could not resolve host: api.notion.com\n")],
            )
        output_path = Path(argv[argv.index("-o") + 1])
        output_path.write_text("fallback chat success", encoding="utf-8")
        return _ChatProcess(
            pid=7002,
            stderr_lines=[(0.0, '{"type":"thread.started","thread_id":"thread-fallback"}\n')],
        )

    monkeypatch.setattr(runner, "_spawn_process", fake_spawn)

    async def scenario() -> None:
        result = await runner.run_chat_turn(prompt="need internet")
        assert result.thread_id == "thread-fallback"
        assert result.assistant_text == "fallback chat success"

    asyncio.run(scenario())
    assert seen_commands == ["primary-codex", "fallback-codex"]
    rows = store.list_events(limit=30)
    assert any(row["event_type"] == "chat_turn_fallback_retry" for row in rows)
    assert any(row["event_type"] == "chat_turn_fallback_succeeded" for row in rows)


def test_runner_chat_turn_retries_with_fallback_on_dns_restriction_assistant_text(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    local_settings = replace(settings, codex_command="primary-codex", codex_command_fallback="fallback-codex")
    runner = Runner(local_settings, store)
    seen_commands: list[str] = []

    async def fake_spawn(argv: list[str]):
        seen_commands.append(argv[0])
        if argv[0] == "primary-codex":
            return _ChatProcess(
                pid=7011,
                stdout_lines=[
                    (
                        0.0,
                        '{"type":"response.output_text.done","text":"Could not resolve host: api.notion.com"}\n',
                    )
                ],
            )
        return _ChatProcess(
            pid=7012,
            stdout_lines=[(0.0, '{"type":"response.output_text.done","text":"fallback chat success"}\n')],
        )

    monkeypatch.setattr(runner, "_spawn_process", fake_spawn)

    async def scenario() -> None:
        result = await runner.run_chat_turn(prompt="notion upload", thread_id="thread-prev")
        assert result.thread_id == "thread-prev"
        assert result.assistant_text == "fallback chat success"

    asyncio.run(scenario())
    assert seen_commands == ["primary-codex", "fallback-codex"]
    rows = store.list_events(limit=30)
    assert any(row["event_type"] == "chat_turn_fallback_retry" for row in rows)
    assert any(row["event_type"] == "chat_turn_fallback_succeeded" for row in rows)


def test_runner_chat_turn_timeout_retries_and_resets_session(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    local_settings = replace(
        settings,
        chat_turn_timeout_seconds=2,
        chat_turn_progress_timeout_seconds=1,
        chat_turn_retry_count=1,
        chat_turn_reset_session_on_timeout=True,
    )
    runner = Runner(local_settings, store)
    seen_argv: list[list[str]] = []
    cancelled: list[int] = []
    attempts = {"count": 0}

    async def fake_spawn(argv: list[str]):
        attempts["count"] += 1
        seen_argv.append(list(argv))
        if attempts["count"] == 1:
            return _ChatProcess(
                pid=6111,
                stdout_lines=[
                    (0.0, '{"type":"thread.started","thread_id":"thread-timeout"}\n'),
                    (0.0, '{"type":"response.output_text.delta","delta":"partial"}\n'),
                ],
                wait_delay_seconds=10.0,
            )
        if "-o" in argv:
            output_path = Path(argv[argv.index("-o") + 1])
            output_path.write_text("retry success", encoding="utf-8")
        return _ChatProcess(
            pid=6112,
            stderr_lines=[(0.0, '{"type":"thread.started","thread_id":"thread-new"}\n')],
        )

    async def fake_cancel(process: object, timeout_budget: float = 15.0) -> bool:  # noqa: ARG001
        cancelled.append(1)
        if isinstance(process, _ChatProcess):
            process.returncode = 130
        return True

    monkeypatch.setattr(runner, "_spawn_process", fake_spawn)
    monkeypatch.setattr(runner, "_cancel_process", fake_cancel)

    async def scenario() -> None:
        result = await runner.run_chat_turn(prompt="retry me", thread_id="thread-prev")
        assert result.thread_id == "thread-new"
        assert result.assistant_text == "retry success"

    asyncio.run(scenario())
    assert cancelled == [1]
    assert seen_argv[0][:4] == [settings.codex_command, "exec", "resume", "--json"]
    assert seen_argv[0][4] == "thread-prev"
    assert seen_argv[1][0] == settings.codex_command
    assert seen_argv[1][1:4] == ["exec", "--json", "-o"]


def test_runner_chat_turn_timeout_raises_after_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    local_settings = replace(
        settings,
        chat_turn_timeout_seconds=2,
        chat_turn_progress_timeout_seconds=1,
        chat_turn_retry_count=0,
        chat_turn_reset_session_on_timeout=True,
    )
    runner = Runner(local_settings, store)
    cancelled: list[int] = []

    async def fake_spawn(_argv: list[str]):
        return _ChatProcess(
            pid=6222,
            stdout_lines=[(0.0, '{"type":"thread.started","thread_id":"thread-6222"}\n')],
            wait_delay_seconds=10.0,
        )

    async def fake_cancel(process: object, timeout_budget: float = 15.0) -> bool:  # noqa: ARG001
        cancelled.append(1)
        if isinstance(process, _ChatProcess):
            process.returncode = 130
        return True

    monkeypatch.setattr(runner, "_spawn_process", fake_spawn)
    monkeypatch.setattr(runner, "_cancel_process", fake_cancel)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="Chat turn timed out"):
            await runner.run_chat_turn(prompt="timeout")

    asyncio.run(scenario())
    assert cancelled == [1]


def test_runner_chat_turn_status_check_allows_continuation_through_silence(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    local_settings = replace(
        settings,
        chat_turn_timeout_seconds=2,
        chat_turn_progress_timeout_seconds=1,
        chat_turn_retry_count=0,
    )
    runner = Runner(local_settings, store)

    async def fake_spawn(_argv: list[str]):
        return _ChatProcess(
            pid=6333,
            stdout_lines=[
                (0.0, '{"type":"thread.started","thread_id":"thread-progress"}\n'),
                (1.2, '{"type":"response.output_text.delta","delta":"Hel"}\n'),
                (0.0, '{"type":"response.output_text.delta","delta":"lo"}\n'),
                (0.2, '{"type":"response.output_text.done","text":"Hello"}\n'),
            ],
            wait_delay_seconds=1.6,
        )

    monkeypatch.setattr(runner, "_spawn_process", fake_spawn)

    async def scenario() -> None:
        result = await runner.run_chat_turn(prompt="stay active")
        assert result.thread_id == "thread-progress"
        assert result.assistant_text == "Hello"

    asyncio.run(scenario())
    rows = store.list_events(limit=20)
    status_rows = [row for row in rows if row["event_type"] == "chat_turn_status_check"]
    assert status_rows
    assert "attempt=1/1" in status_rows[-1]["message"]
    assert "thread_id=thread-progress" in status_rows[-1]["message"]
    assert not any(row["event_type"] == "chat_turn_timeout_partial" for row in rows)


def test_runner_chat_turn_absolute_timeout_records_partial_metadata(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    local_settings = replace(
        settings,
        chat_turn_timeout_seconds=2,
        chat_turn_progress_timeout_seconds=1,
        chat_turn_retry_count=0,
    )
    runner = Runner(local_settings, store)
    cancelled: list[int] = []

    async def fake_spawn(_argv: list[str]):
        return _ChatProcess(
            pid=6444,
            stdout_lines=[
                (0.0, '{"type":"thread.started","thread_id":"thread-stall"}\n'),
                (0.0, '{"type":"response.output_text.delta","delta":"partial reply"}\n'),
            ],
            wait_delay_seconds=10.0,
        )

    async def fake_cancel(process: object, timeout_budget: float = 15.0) -> bool:  # noqa: ARG001
        cancelled.append(1)
        if isinstance(process, _ChatProcess):
            process.returncode = 130
        return True

    monkeypatch.setattr(runner, "_spawn_process", fake_spawn)
    monkeypatch.setattr(runner, "_cancel_process", fake_cancel)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="Chat turn timed out"):
            await runner.run_chat_turn(prompt="stall and timeout")

    asyncio.run(scenario())
    assert cancelled == [1]
    status_rows = [row for row in store.list_events(limit=40) if row["event_type"] == "chat_turn_status_check"]
    assert status_rows
    assert "attempt=1/1" in status_rows[-1]["message"]
    assert "thread_id=thread-stall" in status_rows[-1]["message"]
    rows = [row for row in store.list_events(limit=30) if row["event_type"] == "chat_turn_timeout_partial"]
    assert rows
    assert "kind=absolute" in rows[-1]["message"]
    assert "thread_id=thread-stall" in rows[-1]["message"]
    assert "partial_assistant=partial reply" in rows[-1]["message"]


def test_runner_chat_turn_blocks_while_background_job_active(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    class _FakeStream:
        async def readline(self) -> bytes:
            return b""

    class _FakeProcess:
        pid = 2222
        returncode = None
        stdout = _FakeStream()
        stderr = _FakeStream()

        async def wait(self) -> int:
            await asyncio.sleep(10)
            return 0

        def send_signal(self, _sig: int) -> None:
            self.returncode = 130

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # noqa: ARG001
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def scenario() -> None:
        runner = Runner(settings, store)
        await runner.start_run("hello")
        with pytest.raises(ActiveJobExistsError):
            await runner.run_chat_turn(prompt="should fail")
        await runner.cancel_active_job()

    asyncio.run(scenario())


def test_runner_cancel_orphan_running_job(monkeypatch: pytest.MonkeyPatch, settings: Settings, store: Store) -> None:
    job = store.create_job(command="run", prompt="orphan", status="RUNNING")
    store.set_job_pid(job.id, 9001, pid_start_token="token-9001")
    runner = Runner(settings, store)

    signals: list[int] = []
    alive = {"value": True}

    def fake_signal_pid_group(pid: int, signum: int) -> None:
        assert pid == 9001
        signals.append(signum)
        if signum == signal.SIGTERM:
            alive["value"] = False

    def fake_pid_is_alive(pid: int) -> bool:
        assert pid == 9001
        return alive["value"]

    async def fake_wait_pid_exit(pid: int, timeout: float) -> bool:
        assert pid == 9001
        assert timeout >= 0
        return not alive["value"]

    monkeypatch.setattr(runner, "_signal_pid_group", fake_signal_pid_group)
    monkeypatch.setattr(runner, "_pid_is_alive", fake_pid_is_alive)
    monkeypatch.setattr(runner, "_wait_pid_exit", fake_wait_pid_exit)
    monkeypatch.setattr(runner, "_pid_matches_token", lambda pid, token: pid == 9001 and token == "token-9001")

    async def scenario() -> None:
        assert await runner.cancel_active_job() is True

    asyncio.run(scenario())
    terminal = store.get_job(job.id)
    assert terminal is not None
    assert terminal.status == "CANCELLED"
    assert signals[:2] == [signal.SIGINT, signal.SIGTERM]


def test_runner_cancel_orphan_blocks_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    job = store.create_job(command="run", prompt="orphan", status="RUNNING")
    store.set_job_pid(job.id, 9001, pid_start_token="stale-token")
    runner = Runner(settings, store)

    monkeypatch.setattr(runner, "_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(runner, "_pid_matches_token", lambda _pid, _token: False)

    async def scenario() -> None:
        assert await runner.cancel_active_job() is False

    asyncio.run(scenario())
    terminal = store.get_job(job.id)
    assert terminal is not None
    assert terminal.status == "INTERRUPTED_RECOVERED"
    recovered = store.create_job(command="run", prompt="unblocked", status="RUNNING")
    assert recovered.id > job.id
    rows = store.list_events(limit=20)
    assert any(row["event_type"] == "cancel_blocked_identity_mismatch" for row in rows)


def test_runner_cancel_orphan_already_dead_recovers_job(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    job = store.create_job(command="run", prompt="orphan", status="RUNNING")
    store.set_job_pid(job.id, 9001, pid_start_token="token-9001")
    runner = Runner(settings, store)

    monkeypatch.setattr(runner, "_pid_is_alive", lambda _pid: False)

    async def scenario() -> None:
        assert await runner.cancel_active_job() is True

    asyncio.run(scenario())
    terminal = store.get_job(job.id)
    assert terminal is not None
    assert terminal.status == "INTERRUPTED_RECOVERED"
    rows = store.list_events(limit=20)
    assert any(row["event_type"] == "orphan_already_not_alive" for row in rows)


def test_cancel_does_not_recover_when_process_already_exited_but_monitor_pending(
    settings: Settings,
    store: Store,
) -> None:
    runner = Runner(settings, store)
    job = store.create_job(command="run", prompt="already-exited", status="RUNNING")

    class _ExitedProcess:
        pid = 2222
        returncode = 0

    async def finalize() -> None:
        await asyncio.sleep(0)
        store.set_job_status(job.id, "SUCCEEDED", exit_code=0)
        store.add_event(job.id, "job_finished", "status=SUCCEEDED exit_code=0")

    async def scenario() -> None:
        runner._process = _ExitedProcess()  # type: ignore[assignment]
        runner._active_job_id = job.id
        runner._job_task = asyncio.create_task(finalize())
        cancelled = await runner.cancel_active_job()
        assert cancelled is False
        if runner._job_task:
            await runner._job_task

    asyncio.run(scenario())
    final = store.get_job(job.id)
    assert final is not None
    assert final.status == "SUCCEEDED"


def test_cancel_process_respects_sla_budget(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    class _FakeStream:
        async def readline(self) -> bytes:
            return b""

    class _FakeProcess:
        def __init__(self) -> None:
            self.pid = 12345
            self.returncode = None
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()

        async def wait(self) -> int:
            return 0

        def send_signal(self, _sig: int) -> None:
            return None

        def kill(self) -> None:
            return None

    runner = Runner(settings, store)
    process = _FakeProcess()
    seen_timeouts: list[float] = []
    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    async def fake_wait_exit(_process: object, timeout: float) -> bool:
        seen_timeouts.append(timeout)
        clock["now"] += timeout
        return False

    def fake_signal_group(_process: object, _signum: int) -> None:
        return None

    monkeypatch.setattr("telegram_codex_control.runner.time.monotonic", fake_monotonic)
    monkeypatch.setattr(runner, "_wait_exit", fake_wait_exit)
    monkeypatch.setattr(runner, "_signal_process_group", fake_signal_group)
    asyncio.run(runner._cancel_process(process, timeout_budget=15.0))

    assert seen_timeouts
    assert sum(seen_timeouts) <= 15.0 + 1e-6


def test_runner_notification_failures_do_not_break_job_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    class _FakeStream:
        async def readline(self) -> bytes:
            return b""

    class _FakeProcess:
        def __init__(self) -> None:
            self.pid = 2222
            self.returncode = None
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()

        async def wait(self) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def send_signal(self, _sig: int) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

    async def fake_exec(*args, **kwargs):  # noqa: ARG001
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = Runner(settings, store)

    async def bad_notifier(_text: str) -> None:
        raise RuntimeError("notify failed")

    runner.set_notifier(bad_notifier)

    async def scenario() -> int:
        job = await runner.start_run("hello")
        await runner.wait_for_current_job()
        return job.id

    job_id = asyncio.run(scenario())
    job = store.get_job(job_id)
    assert job is not None
    assert job.status == "SUCCEEDED"
    rows = store.list_events(limit=50)
    assert any(row["event_type"] == "notify_error" for row in rows)


def test_runner_retries_job_with_fallback_command_on_dns_restriction(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    local_settings = replace(settings, codex_command="primary-codex", codex_command_fallback="fallback-codex")
    runner = Runner(local_settings, store)
    started_commands: list[str] = []

    async def fake_exec(*args, **kwargs):  # noqa: ARG001
        command = args[0]
        started_commands.append(command)
        if command == "primary-codex":
            return _ChatProcess(
                pid=8101,
                returncode=1,
                stderr_lines=[(0.0, "curl: (6) Could not resolve host: api.unsplash.com\n")],
            )
        return _ChatProcess(
            pid=8102,
            returncode=0,
            stdout_lines=[(0.0, "fallback completed\n")],
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    notifications: list[object] = []

    async def notifier(message) -> None:
        notifications.append(message)

    runner.set_notifier(notifier)

    async def scenario() -> int:
        job = await runner.start_run("do the task")
        await runner.wait_for_current_job()
        return job.id

    job_id = asyncio.run(scenario())
    job = store.get_job(job_id)
    assert job is not None
    assert job.status == "SUCCEEDED"
    assert started_commands == ["primary-codex", "fallback-codex"]
    rows = store.list_events(limit=80)
    assert any(row["event_type"] == "job_fallback_retry" for row in rows)
    assert any(row["event_type"] == "job_fallback_started" for row in rows)
    assert any("Retrying with fallback command" in getattr(line, "text", "") for line in notifications)


def test_runner_retries_successful_job_with_fallback_when_output_shows_dns_restriction(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    store: Store,
) -> None:
    local_settings = replace(settings, codex_command="primary-codex", codex_command_fallback="fallback-codex")
    runner = Runner(local_settings, store)
    started_commands: list[str] = []

    async def fake_exec(*args, **kwargs):  # noqa: ARG001
        command = args[0]
        started_commands.append(command)
        if command == "primary-codex":
            return _ChatProcess(
                pid=8201,
                returncode=0,
                stdout_lines=[(0.0, "Could not resolve host: api.notion.com\n")],
            )
        return _ChatProcess(
            pid=8202,
            returncode=0,
            stdout_lines=[(0.0, "fallback completed\n")],
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def scenario() -> int:
        job = await runner.start_run("do the task")
        await runner.wait_for_current_job()
        return job.id

    job_id = asyncio.run(scenario())
    job = store.get_job(job_id)
    assert job is not None
    assert job.status == "SUCCEEDED"
    assert started_commands == ["primary-codex", "fallback-codex"]
    rows = store.list_events(limit=80)
    assert any(row["event_type"] == "job_fallback_retry" for row in rows)
