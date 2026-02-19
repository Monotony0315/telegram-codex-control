from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import os
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when required environment configuration is invalid."""


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        raise ConfigError(f"Missing required environment variable: {name}")
    return value.strip()


def _parse_int(env: Mapping[str, str], name: str, default: int | None = None) -> int:
    raw = env.get(name)
    if raw is None:
        if default is None:
            raise ConfigError(f"Missing required integer variable: {name}")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid integer for {name}: {raw}") from exc


def _parse_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid float for {name}: {raw}") from exc


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"Invalid boolean for {name}: {raw}")


def _resolve_path(path_value: str, base_dir: Path) -> Path:
    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    allowed_user_id: int
    allowed_chat_id: int
    workspace_root: Path
    db_path: Path
    audit_log_path: Path
    codex_command: str
    telegram_interactive_mode: bool
    poll_timeout_seconds: int
    poll_retry_base_seconds: float
    poll_retry_max_seconds: float
    job_timeout_seconds: int
    confirmation_ttl_seconds: int
    message_chunk_size: int
    telegram_api_base: str
    telegram_transport: str
    telegram_webhook_public_url: str | None
    telegram_webhook_listen_host: str
    telegram_webhook_listen_port: int
    telegram_webhook_path: str
    telegram_webhook_secret_token: str | None
    command_policy_path: Path | None

    @property
    def telegram_base_url(self) -> str:
        return f"{self.telegram_api_base}/bot{self.telegram_bot_token}"

    @property
    def telegram_webhook_url(self) -> str:
        if self.telegram_webhook_public_url is None:
            raise ConfigError("TELEGRAM_WEBHOOK_PUBLIC_URL is required for webhook mode")
        return f"{self.telegram_webhook_public_url}{self.telegram_webhook_path}"

    def subprocess_env(self) -> dict[str, str]:
        """Allowlist env passthrough for subprocess jobs."""
        allowed: dict[str, str] = {}
        for key in ("PATH",):
            value = os.environ.get(key)
            if value:
                allowed[key] = value

        # Pin HOME to workspace to reduce accidental filesystem spillover.
        allowed["HOME"] = str(self.workspace_root)

        claude_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if claude_config_dir:
            try:
                candidate = Path(claude_config_dir).expanduser().resolve()
                allow_external = os.environ.get("ALLOW_EXTERNAL_CLAUDE_CONFIG_DIR", "").strip() == "1"
                in_workspace = candidate.is_relative_to(self.workspace_root)
                if candidate.is_dir() and (in_workspace or allow_external):
                    allowed["CLAUDE_CONFIG_DIR"] = str(candidate)
            except OSError:
                pass

        return allowed

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, base_dir: Path | None = None) -> "Settings":
        raw_env = os.environ if env is None else env
        root_dir = Path.cwd() if base_dir is None else base_dir

        default_projects_dir = Path.home() / "Projects"
        default_workspace_root = default_projects_dir if default_projects_dir.exists() else Path.home()
        workspace_root_raw = raw_env.get("WORKSPACE_ROOT", str(default_workspace_root))
        workspace_root = Path(workspace_root_raw).expanduser().resolve()
        if not workspace_root.exists():
            raise ConfigError(f"WORKSPACE_ROOT does not exist: {workspace_root}")
        if not workspace_root.is_dir():
            raise ConfigError(f"WORKSPACE_ROOT is not a directory: {workspace_root}")

        db_path = _resolve_path(raw_env.get("DB_PATH", ".data/state.db"), root_dir)
        audit_log_path = _resolve_path(raw_env.get("AUDIT_LOG_PATH", ".data/audit.jsonl"), root_dir)
        command_policy_raw = raw_env.get("COMMAND_POLICY_PATH", "").strip()
        command_policy_path: Path | None = None
        if command_policy_raw:
            command_policy_path = _resolve_path(command_policy_raw, root_dir)

        chunk_size = _parse_int(raw_env, "MESSAGE_CHUNK_SIZE", 3500)
        if chunk_size <= 0:
            raise ConfigError("MESSAGE_CHUNK_SIZE must be greater than 0")
        if chunk_size > 3500:
            chunk_size = 3500

        telegram_transport = raw_env.get("TELEGRAM_TRANSPORT", "polling").strip().lower()
        telegram_webhook_public_url = raw_env.get("TELEGRAM_WEBHOOK_PUBLIC_URL", "").strip().rstrip("/")
        webhook_public_url: str | None = telegram_webhook_public_url or None
        telegram_webhook_listen_host = raw_env.get("TELEGRAM_WEBHOOK_LISTEN_HOST", "127.0.0.1").strip()
        telegram_webhook_listen_port = _parse_int(raw_env, "TELEGRAM_WEBHOOK_LISTEN_PORT", 8080)
        telegram_webhook_path = raw_env.get("TELEGRAM_WEBHOOK_PATH", "/telegram/webhook").strip()
        telegram_webhook_secret_token = raw_env.get("TELEGRAM_WEBHOOK_SECRET_TOKEN", "").strip() or None

        settings = cls(
            telegram_bot_token=_require(raw_env, "TELEGRAM_BOT_TOKEN"),
            allowed_user_id=_parse_int(raw_env, "ALLOWED_USER_ID"),
            allowed_chat_id=_parse_int(raw_env, "ALLOWED_CHAT_ID"),
            workspace_root=workspace_root,
            db_path=db_path,
            audit_log_path=audit_log_path,
            codex_command=raw_env.get("CODEX_COMMAND", "codex"),
            telegram_interactive_mode=_parse_bool(raw_env, "TELEGRAM_INTERACTIVE_MODE", True),
            poll_timeout_seconds=_parse_int(raw_env, "POLL_TIMEOUT_SECONDS", 30),
            poll_retry_base_seconds=_parse_float(raw_env, "POLL_RETRY_BASE_SECONDS", 1.0),
            poll_retry_max_seconds=_parse_float(raw_env, "POLL_RETRY_MAX_SECONDS", 30.0),
            job_timeout_seconds=_parse_int(raw_env, "JOB_TIMEOUT_SECONDS", 7200),
            confirmation_ttl_seconds=_parse_int(raw_env, "CONFIRMATION_TTL_SECONDS", 300),
            message_chunk_size=chunk_size,
            telegram_api_base=raw_env.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
            telegram_transport=telegram_transport,
            telegram_webhook_public_url=webhook_public_url,
            telegram_webhook_listen_host=telegram_webhook_listen_host,
            telegram_webhook_listen_port=telegram_webhook_listen_port,
            telegram_webhook_path=telegram_webhook_path,
            telegram_webhook_secret_token=telegram_webhook_secret_token,
            command_policy_path=command_policy_path,
        )

        parsed_api_base = urlparse(settings.telegram_api_base)
        if parsed_api_base.scheme != "https":
            raise ConfigError("TELEGRAM_API_BASE must use https")
        if not parsed_api_base.hostname:
            raise ConfigError("TELEGRAM_API_BASE must include a valid host")
        allow_untrusted = raw_env.get("ALLOW_UNTRUSTED_TELEGRAM_API_BASE", "").strip() == "1"
        if parsed_api_base.hostname != "api.telegram.org" and not allow_untrusted:
            raise ConfigError(
                "TELEGRAM_API_BASE host must be api.telegram.org unless ALLOW_UNTRUSTED_TELEGRAM_API_BASE=1"
            )

        if settings.poll_timeout_seconds <= 0:
            raise ConfigError("POLL_TIMEOUT_SECONDS must be greater than 0")
        if settings.poll_retry_base_seconds <= 0:
            raise ConfigError("POLL_RETRY_BASE_SECONDS must be greater than 0")
        if settings.poll_retry_max_seconds < settings.poll_retry_base_seconds:
            raise ConfigError("POLL_RETRY_MAX_SECONDS must be >= POLL_RETRY_BASE_SECONDS")
        if settings.job_timeout_seconds <= 0:
            raise ConfigError("JOB_TIMEOUT_SECONDS must be greater than 0")
        if settings.confirmation_ttl_seconds <= 0:
            raise ConfigError("CONFIRMATION_TTL_SECONDS must be greater than 0")
        if not settings.codex_command.strip():
            raise ConfigError("CODEX_COMMAND must not be empty")
        if settings.telegram_transport not in {"polling", "webhook"}:
            raise ConfigError("TELEGRAM_TRANSPORT must be either 'polling' or 'webhook'")
        if not settings.telegram_webhook_listen_host:
            raise ConfigError("TELEGRAM_WEBHOOK_LISTEN_HOST must not be empty")
        if settings.telegram_webhook_listen_port <= 0 or settings.telegram_webhook_listen_port > 65535:
            raise ConfigError("TELEGRAM_WEBHOOK_LISTEN_PORT must be between 1 and 65535")
        if not settings.telegram_webhook_path.startswith("/"):
            raise ConfigError("TELEGRAM_WEBHOOK_PATH must start with '/'")
        if " " in settings.telegram_webhook_path:
            raise ConfigError("TELEGRAM_WEBHOOK_PATH must not contain spaces")
        if settings.telegram_webhook_secret_token and len(settings.telegram_webhook_secret_token) > 256:
            raise ConfigError("TELEGRAM_WEBHOOK_SECRET_TOKEN must be <= 256 characters")
        if settings.telegram_transport == "webhook":
            if settings.telegram_webhook_public_url is None:
                raise ConfigError("TELEGRAM_WEBHOOK_PUBLIC_URL is required when TELEGRAM_TRANSPORT=webhook")
            parsed_webhook = urlparse(settings.telegram_webhook_public_url)
            if parsed_webhook.scheme != "https":
                raise ConfigError("TELEGRAM_WEBHOOK_PUBLIC_URL must use https")
            if not parsed_webhook.hostname:
                raise ConfigError("TELEGRAM_WEBHOOK_PUBLIC_URL must include a valid host")
            if not settings.telegram_webhook_secret_token:
                raise ConfigError("TELEGRAM_WEBHOOK_SECRET_TOKEN is required when TELEGRAM_TRANSPORT=webhook")
            if len(settings.telegram_webhook_secret_token) < 16:
                raise ConfigError("TELEGRAM_WEBHOOK_SECRET_TOKEN must be at least 16 characters")
        if settings.command_policy_path is not None:
            if not settings.command_policy_path.exists():
                raise ConfigError(f"COMMAND_POLICY_PATH does not exist: {settings.command_policy_path}")
            if not settings.command_policy_path.is_file():
                raise ConfigError(f"COMMAND_POLICY_PATH is not a file: {settings.command_policy_path}")

        return settings
