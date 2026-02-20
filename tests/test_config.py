from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from telegram_codex_control.config import ConfigError, Settings


def test_subprocess_env_is_allowlisted(monkeypatch, settings: Settings, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    home_path = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home_path))
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "should-not-leak")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing-config-dir"))

    env = settings.subprocess_env()

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == str(home_path)
    assert "SUPER_SECRET_TOKEN" not in env
    assert "CLAUDE_CONFIG_DIR" not in env


def test_subprocess_env_allows_explicit_subprocess_home_override(
    monkeypatch,
    settings: Settings,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "inherited-home"))
    override = tmp_path / "sandbox-home"
    monkeypatch.setenv("SUBPROCESS_HOME", str(override))

    env = settings.subprocess_env()
    assert env["HOME"] == str(override.resolve())


def test_subprocess_env_includes_default_ai_env_keys(monkeypatch, settings: Settings) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")

    env = settings.subprocess_env()

    assert env["OPENAI_API_KEY"] == "sk-test-openai"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant"


def test_subprocess_env_can_include_custom_allowlist_keys(monkeypatch, settings: Settings) -> None:
    monkeypatch.setenv("CUSTOM_TEAM_TOKEN", "team-secret")
    custom_settings = replace(settings, subprocess_env_allowlist=("CUSTOM_TEAM_TOKEN",))

    env = custom_settings.subprocess_env()
    assert env["CUSTOM_TEAM_TOKEN"] == "team-secret"


def test_subprocess_env_can_include_prefix_allowlist_keys(monkeypatch, settings: Settings) -> None:
    monkeypatch.setenv("MYAPP_API_KEY", "value-123")
    monkeypatch.setenv("MYAPP_REGION", "us-east-1")
    custom_settings = replace(settings, subprocess_env_prefix_allowlist=("MYAPP_",))

    env = custom_settings.subprocess_env()
    assert env["MYAPP_API_KEY"] == "value-123"
    assert env["MYAPP_REGION"] == "us-east-1"


def test_subprocess_env_blocks_telegram_bot_token_even_with_prefix(monkeypatch, settings: Settings) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:secret")
    custom_settings = replace(settings, subprocess_env_prefix_allowlist=("TELEGRAM_",))

    env = custom_settings.subprocess_env()
    assert "TELEGRAM_BOT_TOKEN" not in env


def test_subprocess_env_includes_valid_claude_config_dir(
    monkeypatch,
    settings: Settings,
    workspace_root: Path,
) -> None:
    claude_config = workspace_root / "claude-config"
    claude_config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))

    env = settings.subprocess_env()
    assert env["CLAUDE_CONFIG_DIR"] == str(claude_config.resolve())


def test_subprocess_env_external_claude_config_requires_opt_in(
    monkeypatch,
    settings: Settings,
    tmp_path: Path,
) -> None:
    external_config = tmp_path / "external-claude-config"
    external_config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(external_config))

    env = settings.subprocess_env()
    assert "CLAUDE_CONFIG_DIR" not in env

    monkeypatch.setenv("ALLOW_EXTERNAL_CLAUDE_CONFIG_DIR", "1")
    env = settings.subprocess_env()
    assert env["CLAUDE_CONFIG_DIR"] == str(external_config.resolve())


def test_from_env_rejects_non_https_telegram_api_base(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "TELEGRAM_API_BASE": "http://api.telegram.org",
    }
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, base_dir=workspace_root)


def test_from_env_rejects_untrusted_telegram_api_base_without_opt_in(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "TELEGRAM_API_BASE": "https://evil.example",
    }
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, base_dir=workspace_root)


def test_from_env_allows_untrusted_telegram_api_base_with_opt_in(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "TELEGRAM_API_BASE": "https://self-hosted.telegram.internal",
        "ALLOW_UNTRUSTED_TELEGRAM_API_BASE": "1",
    }
    settings = Settings.from_env(env=env, base_dir=workspace_root)
    assert settings.telegram_api_base == "https://self-hosted.telegram.internal"


def test_from_env_default_workspace_root_prefers_home_projects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    projects = home / "Projects"
    projects.mkdir(parents=True)
    monkeypatch.setattr("telegram_codex_control.config.Path.home", lambda: home)

    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
    }
    settings = Settings.from_env(env=env, base_dir=tmp_path)
    assert settings.workspace_root == projects.resolve()


def test_from_env_default_workspace_root_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setattr("telegram_codex_control.config.Path.home", lambda: home)

    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
    }
    settings = Settings.from_env(env=env, base_dir=tmp_path)
    assert settings.workspace_root == home.resolve()


def test_from_env_parses_upload_dir_and_size_limits(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "UPLOAD_DIR": ".data/incoming",
        "MAX_DOWNLOAD_FILE_SIZE_BYTES": "2048",
        "MAX_UPLOAD_FILE_SIZE_BYTES": "1024",
    }
    settings = Settings.from_env(env=env, base_dir=workspace_root)
    assert settings.upload_dir == (workspace_root / ".data" / "incoming").resolve()
    assert settings.max_download_file_size_bytes == 2048
    assert settings.max_upload_file_size_bytes == 1024


def test_from_env_parses_chat_turn_timeout(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "CHAT_TURN_TIMEOUT_SECONDS": "45",
    }
    settings = Settings.from_env(env=env, base_dir=workspace_root)
    assert settings.chat_turn_timeout_seconds == 45


def test_from_env_rejects_invalid_chat_turn_timeout(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "CHAT_TURN_TIMEOUT_SECONDS": "0",
    }
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, base_dir=workspace_root)


def test_from_env_rejects_upload_dir_outside_workspace(workspace_root: Path, tmp_path: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "UPLOAD_DIR": str(tmp_path / "outside"),
    }
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, base_dir=workspace_root)


def test_from_env_webhook_requires_public_url(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "TELEGRAM_TRANSPORT": "webhook",
        "TELEGRAM_WEBHOOK_SECRET_TOKEN": "0123456789abcdef",
    }
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, base_dir=workspace_root)


def test_from_env_webhook_validates_public_url_scheme(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "TELEGRAM_TRANSPORT": "webhook",
        "TELEGRAM_WEBHOOK_PUBLIC_URL": "http://bot.example.com",
        "TELEGRAM_WEBHOOK_SECRET_TOKEN": "0123456789abcdef",
    }
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, base_dir=workspace_root)


def test_from_env_accepts_webhook_settings(workspace_root: Path, tmp_path: Path) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text('{"rules":[]}', encoding="utf-8")
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "TELEGRAM_TRANSPORT": "webhook",
        "TELEGRAM_WEBHOOK_PUBLIC_URL": "https://bot.example.com",
        "TELEGRAM_WEBHOOK_LISTEN_PORT": "8088",
        "TELEGRAM_WEBHOOK_PATH": "/tg/inbound",
        "TELEGRAM_WEBHOOK_SECRET_TOKEN": "0123456789abcdef",
        "COMMAND_POLICY_PATH": str(policy),
    }
    settings = Settings.from_env(env=env, base_dir=workspace_root)
    assert settings.telegram_transport == "webhook"
    assert settings.telegram_webhook_url == "https://bot.example.com/tg/inbound"
    assert settings.telegram_webhook_listen_port == 8088
    assert settings.command_policy_path == policy.resolve()


def test_from_env_webhook_requires_secret_token(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "TELEGRAM_TRANSPORT": "webhook",
        "TELEGRAM_WEBHOOK_PUBLIC_URL": "https://bot.example.com",
    }
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, base_dir=workspace_root)


def test_from_env_rejects_missing_command_policy_path(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "COMMAND_POLICY_PATH": "does-not-exist.json",
    }
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, base_dir=workspace_root)


def test_from_env_defaults_interactive_mode_to_true(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
    }
    settings = Settings.from_env(env=env, base_dir=workspace_root)
    assert settings.telegram_interactive_mode is True


def test_from_env_allows_disabling_interactive_mode(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "TELEGRAM_INTERACTIVE_MODE": "false",
    }
    settings = Settings.from_env(env=env, base_dir=workspace_root)
    assert settings.telegram_interactive_mode is False


def test_from_env_parses_subprocess_allowlists(workspace_root: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_VALUE_xxxxxxxxxxxxxxxxx",
        "ALLOWED_USER_ID": "1",
        "ALLOWED_CHAT_ID": "2",
        "WORKSPACE_ROOT": str(workspace_root),
        "SUBPROCESS_ENV_ALLOWLIST": "FOO_TOKEN, BAR_KEY",
        "SUBPROCESS_ENV_PREFIX_ALLOWLIST": "MYAPP_,  TEAM_ ",
    }
    settings = Settings.from_env(env=env, base_dir=workspace_root)
    assert settings.subprocess_env_allowlist == ("FOO_TOKEN", "BAR_KEY")
    assert settings.subprocess_env_prefix_allowlist == ("MYAPP_", "TEAM_")
