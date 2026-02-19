from __future__ import annotations

from dataclasses import replace
import asyncio
import re

from telegram_codex_control.bot import TelegramBotDaemon
from telegram_codex_control.config import Settings
from telegram_codex_control.safety import SafetyManager
from telegram_codex_control.store import ActiveJobExistsError, Store


def _run(coro):
    return asyncio.run(coro)


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeTelegramClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []

    async def post(self, url: str, json: dict) -> FakeResponse:  # noqa: A002
        self.calls.append((url, json))
        if url.endswith("/getUpdates"):
            return FakeResponse({"ok": True, "result": list(self.updates)})
        if url.endswith("/sendMessage"):
            return FakeResponse({"ok": True, "result": {"message_id": len(self.calls)}})
        return FakeResponse({"ok": True, "result": {}})

    async def aclose(self) -> None:
        return None


class DummyRunner:
    def __init__(self) -> None:
        self.run_calls: list[str] = []
        self.autopilot_calls: list[str] = []
        self.cancel_calls = 0
        self._notifier = None

    def set_notifier(self, notifier) -> None:
        self._notifier = notifier

    async def start_run(self, prompt: str):
        self.run_calls.append(prompt)
        return type("Job", (), {"id": 101})()

    async def start_autopilot(self, task: str):
        self.autopilot_calls.append(task)
        return type("Job", (), {"id": 202})()

    async def cancel_active_job(self) -> bool:
        self.cancel_calls += 1
        return True

    def uptime_seconds(self) -> int:
        return 12


def _make_bot(settings: Settings, store: Store, runner: DummyRunner, client: FakeTelegramClient) -> TelegramBotDaemon:
    safety = SafetyManager(store, confirmation_ttl_seconds=settings.confirmation_ttl_seconds)
    return TelegramBotDaemon(settings, store, runner, safety, client=client)


def test_unauthorized_update_does_not_start_runner(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)

    unauthorized = {
        "update_id": 1,
        "message": {
            "chat": {"id": settings.allowed_chat_id + 1},
            "from": {"id": settings.allowed_user_id},
            "text": "/run do not run",
        },
    }
    _run(bot.handle_update(unauthorized))

    assert runner.run_calls == []
    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts and sent_texts[-1] == "Unauthorized."


def test_unauthorized_updates_are_rate_limited(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)

    first = {
        "update_id": 1,
        "message": {
            "chat": {"id": settings.allowed_chat_id + 9},
            "from": {"id": settings.allowed_user_id + 9},
            "text": "/run nope",
        },
    }
    second = {
        "update_id": 2,
        "message": {
            "chat": {"id": settings.allowed_chat_id + 9},
            "from": {"id": settings.allowed_user_id + 9},
            "text": "/run nope-again",
        },
    }

    _run(bot.handle_update(first))
    _run(bot.handle_update(second))

    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts.count("Unauthorized.") == 1
    auth_denied = [row for row in store.list_events(limit=50) if row["event_type"] == "auth_denied"]
    assert len(auth_denied) == 2


def test_run_rejects_autopilot_triggers(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)

    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": settings.allowed_chat_id},
            "from": {"id": settings.allowed_user_id},
            "text": "/run $autopilot do risky thing",
        },
    }
    _run(bot.handle_update(update))

    assert runner.run_calls == []
    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts
    assert "Autopilot-like prompts are blocked on /run." in sent_texts[-1]


def test_autopilot_requires_confirm_then_runs(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)

    request = {
        "update_id": 1,
        "message": {
            "chat": {"id": settings.allowed_chat_id},
            "from": {"id": settings.allowed_user_id},
            "text": "/autopilot build feature",
        },
    }
    _run(bot.handle_update(request))

    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts
    match = re.search(r"/confirm ([a-f0-9]+)", sent_texts[-1])
    assert match is not None
    nonce = match.group(1)

    confirm = {
        "update_id": 2,
        "message": {
            "chat": {"id": settings.allowed_chat_id},
            "from": {"id": settings.allowed_user_id},
            "text": f"/confirm {nonce}",
        },
    }
    _run(bot.handle_update(confirm))

    assert runner.autopilot_calls == ["build feature"]


def test_run_requires_confirm_then_runs(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)

    request = {
        "update_id": 1,
        "message": {
            "chat": {"id": settings.allowed_chat_id},
            "from": {"id": settings.allowed_user_id},
            "text": "/run summarize this",
        },
    }
    _run(bot.handle_update(request))

    assert runner.run_calls == []
    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    match = re.search(r"/confirm ([a-f0-9]+)", sent_texts[-1])
    assert match is not None
    nonce = match.group(1)

    confirm = {
        "update_id": 2,
        "message": {
            "chat": {"id": settings.allowed_chat_id},
            "from": {"id": settings.allowed_user_id},
            "text": f"/confirm {nonce}",
        },
    }
    _run(bot.handle_update(confirm))
    assert runner.run_calls == ["summarize this"]


def test_confirm_nonce_not_consumed_before_successful_admission(settings: Settings, store: Store) -> None:
    class _FlakyRunner(DummyRunner):
        def __init__(self) -> None:
            super().__init__()
            self._fail_once = True

        async def start_autopilot(self, task: str):
            if self._fail_once:
                self._fail_once = False
                raise ActiveJobExistsError("busy")
            return await super().start_autopilot(task)

    client = FakeTelegramClient()
    runner = _FlakyRunner()
    bot = _make_bot(settings, store, runner, client)

    _run(
        bot.handle_update(
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": "/autopilot build feature",
                },
            }
        )
    )
    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    match = re.search(r"/confirm ([a-f0-9]+)", sent_texts[-1])
    assert match is not None
    nonce = match.group(1)

    _run(
        bot.handle_update(
            {
                "update_id": 2,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": f"/confirm {nonce}",
                },
            }
        )
    )
    assert runner.autopilot_calls == []
    assert bot.safety.get_confirmation(
        nonce=nonce,
        user_id=settings.allowed_user_id,
        chat_id=settings.allowed_chat_id,
    ) is not None

    _run(
        bot.handle_update(
            {
                "update_id": 3,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": f"/confirm {nonce}",
                },
            }
        )
    )
    assert runner.autopilot_calls == ["build feature"]


def test_poll_forever_continues_after_per_update_failure(
    settings: Settings,
    store: Store,
    monkeypatch,
) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)
    stop_event = asyncio.Event()
    calls = {"count": 0}
    handled: list[int] = []

    async def fake_get_updates(*, offset: int) -> list[dict]:  # noqa: ARG001
        if calls["count"] == 0:
            calls["count"] += 1
            return [{"update_id": 1}, {"update_id": 2}]
        stop_event.set()
        return []

    async def flaky_handle_update(update: dict) -> None:
        if update["update_id"] == 1:
            raise RuntimeError("boom")
        handled.append(update["update_id"])

    monkeypatch.setattr(bot, "_get_updates", fake_get_updates)
    monkeypatch.setattr(bot, "handle_update", flaky_handle_update)
    _run(bot.poll_forever(stop_event=stop_event))

    assert handled == [2]
    rows = store.list_events(limit=20)
    assert any(row["event_type"] == "update_error" for row in rows)


def test_handle_update_records_command_metadata_before_execution_failure(
    settings: Settings,
    store: Store,
) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()

    async def fail_start_run(_prompt: str):
        raise RuntimeError("start failure")

    runner.start_run = fail_start_run  # type: ignore[assignment]
    bot = _make_bot(settings, store, runner, client)
    _run(
        bot.handle_update(
            {
                "update_id": 11,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": "/run api_key super-secret-value",
                },
            }
        )
    )

    assert store.get_last_update_id() == 11
    rows = store.list_events(limit=50)
    command_events = [row for row in rows if row["event_type"] == "command_received"]
    assert command_events
    assert "super-secret-value" not in command_events[-1]["message"]
    assert "command=/run" in command_events[-1]["message"]


def test_logs_are_chunked_and_redacted(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(replace(settings, message_chunk_size=200), store, runner, client)

    store.add_event(None, "test", "token=super-secret " + ("x" * 900))
    store.add_event(None, "process_stdout", "password=hunter2")
    _run(
        bot.handle_command(
            chat_id=settings.allowed_chat_id,
            user_id=settings.allowed_user_id,
            text="/logs",
        )
    )

    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts
    assert all(len(chunk) <= 200 for chunk in sent_texts)
    assert "super-secret" not in "".join(sent_texts)
    assert "hunter2" not in "".join(sent_texts)
    assert "[process output omitted]" in "".join(sent_texts)


def test_cancel_reports_blocked_when_active_job_remains(settings: Settings, store: Store) -> None:
    class _BlockedRunner(DummyRunner):
        async def cancel_active_job(self) -> bool:
            return False

    client = FakeTelegramClient()
    runner = _BlockedRunner()
    bot = _make_bot(settings, store, runner, client)
    store.create_job(command="run", prompt="active", status="RUNNING")

    _run(
        bot.handle_command(
            chat_id=settings.allowed_chat_id,
            user_id=settings.allowed_user_id,
            text="/cancel",
        )
    )

    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts
    assert sent_texts[-1] == "Cancellation blocked or still in progress. Check /logs."
