from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import asyncio
import sys

import pytest

from telegram_codex_control.config import Settings
from telegram_codex_control.runner import Runner
from telegram_codex_control.store import ActiveJobExistsError, Store
from telegram_codex_control.utils import chunk_text


def test_at_08_chunks_never_exceed_telegram_limit() -> None:
    source = "a" * 12000
    chunks = chunk_text(source, max_size=3500)
    assert all(len(chunk) <= 3500 for chunk in chunks)


def test_at_10_one_active_job_only(settings: Settings, store: Store) -> None:
    fake_script = Path(__file__).parent / "fakes" / "fake_codex.py"
    local_settings = replace(settings, codex_command=sys.executable)

    async def scenario() -> None:
        runner = Runner(local_settings, store)
        await runner.start_run(str(fake_script))
        with pytest.raises(ActiveJobExistsError):
            await runner.start_run(str(fake_script))
        await runner.cancel_active_job()
        await runner.wait_for_current_job()

    asyncio.run(scenario())


def test_at_12_no_shell_interpolation(monkeypatch: pytest.MonkeyPatch, settings: Settings, store: Store) -> None:
    called = {"exec": 0, "shell": 0}

    class _FakeStream:
        async def readline(self) -> bytes:
            return b""

    class _FakeProcess:
        pid = 77
        returncode = 0
        stdout = _FakeStream()
        stderr = _FakeStream()

        async def wait(self) -> int:
            return 0

        def send_signal(self, _sig: int) -> None:
            return None

        def kill(self) -> None:
            return None

    async def fake_exec(*args, **kwargs):
        del args, kwargs
        called["exec"] += 1
        return _FakeProcess()

    async def fake_shell(*args, **kwargs):
        del args, kwargs
        called["shell"] += 1
        raise AssertionError("shell path must not be used")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)

    async def scenario() -> None:
        runner = Runner(settings, store)
        await runner.start_run("hello")
        await runner.wait_for_current_job()

    asyncio.run(scenario())
    assert called["exec"] == 1
    assert called["shell"] == 0
