from __future__ import annotations

from pathlib import Path

import pytest

from telegram_codex_control.config import Settings
from telegram_codex_control.store import Store


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def settings(tmp_path: Path, workspace_root: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        allowed_user_id=1111,
        allowed_chat_id=2222,
        workspace_root=workspace_root,
        db_path=tmp_path / "state.db",
        audit_log_path=tmp_path / "audit.jsonl",
        codex_command="codex",
        poll_timeout_seconds=1,
        poll_retry_base_seconds=0.01,
        poll_retry_max_seconds=0.1,
        job_timeout_seconds=30,
        confirmation_ttl_seconds=120,
        message_chunk_size=3500,
        telegram_api_base="https://api.telegram.org",
        telegram_transport="polling",
        telegram_webhook_public_url=None,
        telegram_webhook_listen_host="127.0.0.1",
        telegram_webhook_listen_port=8080,
        telegram_webhook_path="/telegram/webhook",
        telegram_webhook_secret_token=None,
        command_policy_path=None,
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    instance = Store(settings.db_path, settings.audit_log_path)
    instance.initialize()
    yield instance
    instance.close()
