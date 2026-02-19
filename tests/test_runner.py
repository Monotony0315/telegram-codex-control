from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import asyncio
import signal
import sys

import pytest

from telegram_codex_control.config import Settings
from telegram_codex_control.runner import Runner
from telegram_codex_control.store import ActiveJobExistsError, Store


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
    assert kwargs["env"]["HOME"] == str(settings.workspace_root)
    assert "shell" not in kwargs


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
