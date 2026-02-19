from __future__ import annotations

from dataclasses import replace
import asyncio
import json
import re

import pytest

from telegram_codex_control.bot import TelegramBotDaemon
from telegram_codex_control.config import Settings
from telegram_codex_control.runner import ChatTurnResult
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
        self.codex_calls: list[str] = []
        self.chat_calls: list[tuple[str, str | None]] = []
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

    async def start_codex(self, raw_args: str):
        self.codex_calls.append(raw_args)
        return type("Job", (), {"id": 303})()

    async def run_chat_turn(self, *, prompt: str, thread_id: str | None = None) -> ChatTurnResult:
        self.chat_calls.append((prompt, thread_id))
        return ChatTurnResult(
            thread_id=thread_id or "thread-new",
            assistant_text=f"chat-reply: {prompt}",
        )

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


def test_codex_requires_confirm_then_runs(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)

    request = {
        "update_id": 1,
        "message": {
            "chat": {"id": settings.allowed_chat_id},
            "from": {"id": settings.allowed_user_id},
            "text": "/codex --help",
        },
    }
    _run(bot.handle_update(request))

    assert runner.codex_calls == []
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
    assert runner.codex_calls == ["--help"]


def test_help_lists_supported_commands(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)

    _run(
        bot.handle_command(
            chat_id=settings.allowed_chat_id,
            user_id=settings.allowed_user_id,
            text="/help",
        )
    )
    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts
    assert "/chat" in sent_texts[-1]
    assert "/codex" in sent_texts[-1]


def test_plain_text_routes_to_chat_turn(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)

    _run(
        bot.handle_update(
            {
                "update_id": 40,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": "hello from telegram",
                },
            }
        )
    )

    assert runner.chat_calls == [("hello from telegram", None)]
    assert store.get_chat_session_thread(user_id=settings.allowed_user_id, chat_id=settings.allowed_chat_id) == "thread-new"
    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts[-1] == "chat-reply: hello from telegram"


def test_plain_text_chat_resumes_saved_thread(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)
    store.set_chat_session_thread(user_id=settings.allowed_user_id, chat_id=settings.allowed_chat_id, thread_id="thread-prev")

    _run(
        bot.handle_update(
            {
                "update_id": 41,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": "continue please",
                },
            }
        )
    )

    assert runner.chat_calls == [("continue please", "thread-prev")]


def test_chat_reset_clears_saved_thread(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)
    store.set_chat_session_thread(user_id=settings.allowed_user_id, chat_id=settings.allowed_chat_id, thread_id="thread-prev")

    _run(
        bot.handle_update(
            {
                "update_id": 42,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": "/chat reset",
                },
            }
        )
    )

    assert store.get_chat_session_thread(user_id=settings.allowed_user_id, chat_id=settings.allowed_chat_id) is None
    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts[-1] == "Chat session reset."


def test_plain_text_chat_guides_when_interactive_mode_disabled(settings: Settings, store: Store) -> None:
    local_settings = replace(settings, telegram_interactive_mode=False)
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(local_settings, store, runner, client)

    _run(
        bot.handle_update(
            {
                "update_id": 43,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": "plain text command",
                },
            }
        )
    )

    assert runner.chat_calls == []
    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts
    assert sent_texts[-1] == "Interactive chat is disabled. Use slash commands or set TELEGRAM_INTERACTIVE_MODE=true."


def test_plain_text_chat_reports_runner_failure(settings: Settings, store: Store) -> None:
    class _FailingRunner(DummyRunner):
        async def run_chat_turn(self, *, prompt: str, thread_id: str | None = None) -> ChatTurnResult:
            del prompt, thread_id
            raise RuntimeError("network down")

    client = FakeTelegramClient()
    runner = _FailingRunner()
    bot = _make_bot(settings, store, runner, client)

    _run(
        bot.handle_update(
            {
                "update_id": 45,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": "plain text command",
                },
            }
        )
    )

    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts
    assert sent_texts[-1] == "Chat turn failed: network down"


def test_command_policy_can_deny_specific_command(settings: Settings, store: Store, tmp_path) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "user_id": settings.allowed_user_id,
                        "chat_id": settings.allowed_chat_id,
                        "allow": ["/status", "/logs"],
                        "deny": ["/run"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    local_settings = replace(settings, command_policy_path=policy_path)
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(local_settings, store, runner, client)

    _run(
        bot.handle_update(
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": "/run do-something",
                },
            }
        )
    )
    assert runner.run_calls == []
    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts
    assert "Command denied by policy: /run" == sent_texts[-1]


def test_command_policy_applies_to_plain_text_chat_as_chat_command(
    settings: Settings,
    store: Store,
    tmp_path,
) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "user_id": settings.allowed_user_id,
                        "chat_id": settings.allowed_chat_id,
                        "allow": ["/status", "/logs"],
                        "deny": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    local_settings = replace(settings, command_policy_path=policy_path)
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(local_settings, store, runner, client)

    _run(
        bot.handle_update(
            {
                "update_id": 44,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": "chat message denied by policy",
                },
            }
        )
    )

    assert runner.chat_calls == []
    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts[-1] == "Command denied by policy: /chat"


def test_configure_webhook_calls_set_webhook(settings: Settings, store: Store) -> None:
    local_settings = replace(
        settings,
        telegram_transport="webhook",
        telegram_webhook_public_url="https://bot.example.com",
        telegram_webhook_path="/telegram/inbound",
        telegram_webhook_secret_token="secret-token",
    )
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(local_settings, store, runner, client)
    _run(bot._configure_webhook())

    calls = [(url, payload) for url, payload in client.calls if url.endswith("/setWebhook")]
    assert calls
    _url, payload = calls[-1]
    assert payload["url"] == "https://bot.example.com/telegram/inbound"
    assert payload["secret_token"] == "secret-token"


def test_run_forever_dispatches_to_webhook_mode(monkeypatch, settings: Settings, store: Store) -> None:
    local_settings = replace(
        settings,
        telegram_transport="webhook",
        telegram_webhook_public_url="https://bot.example.com",
    )
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(local_settings, store, runner, client)
    called = {"webhook": 0}

    async def fake_webhook_forever(*, stop_event=None):  # noqa: ARG001
        called["webhook"] += 1

    monkeypatch.setattr(bot, "webhook_forever", fake_webhook_forever)
    _run(bot.run_forever())
    assert called["webhook"] == 1


def test_webhook_http_receiver_accepts_valid_update(settings: Settings, store: Store) -> None:
    local_settings = replace(
        settings,
        telegram_transport="webhook",
        telegram_webhook_public_url="https://bot.example.com",
        telegram_webhook_path="/telegram/webhook",
        telegram_webhook_secret_token="secret-token",
    )
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(local_settings, store, runner, client)

    async def scenario() -> str:
        try:
            server = await asyncio.start_server(bot._handle_webhook_connection, host="127.0.0.1", port=0)
        except PermissionError:
            pytest.skip("Socket bind not permitted in this environment")
        port = server.sockets[0].getsockname()[1]
        update = {
            "update_id": 77,
            "message": {
                "chat": {"id": settings.allowed_chat_id},
                "from": {"id": settings.allowed_user_id},
                "text": "/status",
            },
        }
        raw_body = json.dumps(update).encode("utf-8")
        request = (
            b"POST /telegram/webhook HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"X-Telegram-Bot-Api-Secret-Token: secret-token\r\n"
            + f"Content-Length: {len(raw_body)}\r\n\r\n".encode("ascii")
            + raw_body
        )
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()
        return response.decode("utf-8", errors="replace")

    response_text = _run(scenario())
    assert "200 OK" in response_text
    assert store.get_last_update_id() == 77


def test_policy_default_rules_apply_to_non_owner_identity(settings: Settings, store: Store, tmp_path) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "default": {"allow": ["/status"]},
                "rules": [],
            }
        ),
        encoding="utf-8",
    )
    local_settings = replace(settings, command_policy_path=policy_path)
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(local_settings, store, runner, client)

    _run(
        bot.handle_update(
            {
                "update_id": 88,
                "message": {
                    "chat": {"id": 9000},
                    "from": {"id": 9001},
                    "text": "/status",
                },
            }
        )
    )

    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts
    assert "health: ok" in sent_texts[-1]


def test_command_mentions_are_normalized(settings: Settings, store: Store) -> None:
    client = FakeTelegramClient()
    runner = DummyRunner()
    bot = _make_bot(settings, store, runner, client)

    _run(
        bot.handle_update(
            {
                "update_id": 99,
                "message": {
                    "chat": {"id": settings.allowed_chat_id},
                    "from": {"id": settings.allowed_user_id},
                    "text": "/status@MonotonyCodexBot",
                },
            }
        )
    )
    sent_texts = [payload["text"] for url, payload in client.calls if url.endswith("/sendMessage")]
    assert sent_texts
    assert "health: ok" in sent_texts[-1]


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
