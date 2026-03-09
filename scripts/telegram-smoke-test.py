#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class EnvConfig:
    telegram_bot_token: str
    allowed_chat_id: str
    telegram_api_base: str


def _load_env(project_dir: Path) -> None:
    env_path = project_dir / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _require_env() -> EnvConfig:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ALLOWED_CHAT_ID", "").strip()
    api_base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").strip().rstrip("/")
    missing: list[str] = []
    if not token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not chat_id:
        missing.append("ALLOWED_CHAT_ID")
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
    return EnvConfig(telegram_bot_token=token, allowed_chat_id=chat_id, telegram_api_base=api_base)


def _api_url(config: EnvConfig, method: str) -> str:
    return f"{config.telegram_api_base}/bot{config.telegram_bot_token}/{method}"


def _call_api(config: EnvConfig, method: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        encoded = urlencode(payload).encode("utf-8")
        data = encoded
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(_api_url(config, method), data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload_obj = json.loads(body)
        except json.JSONDecodeError:
            payload_obj = {"ok": False, "description": body}
        return exc.code, payload_obj
    except URLError as exc:
        raise SystemExit(f"Network error calling Telegram API: {exc}") from exc


def _print_result(label: str, status_code: int, payload: dict[str, object]) -> None:
    ok = payload.get("ok")
    description = payload.get("description", "")
    print(f"[{label}] http={status_code} ok={ok!r}")
    if description:
        print(f"  description={description}")
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("id", "is_bot", "username", "type", "title", "url", "pending_update_count"):
            if key in result:
                print(f"  {key}={result[key]!r}")
    elif result is not None:
        print(f"  result={result!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test Telegram bot connectivity using the local .env.")
    parser.add_argument("--project-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--send-ping", action="store_true", help="Send a diagnostic message to ALLOWED_CHAT_ID.")
    parser.add_argument(
        "--check-get-updates",
        action="store_true",
        help="Call getUpdates once. Useful for confirming polling conflicts (409) or idle readiness.",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    _load_env(project_dir)
    config = _require_env()

    status_code, payload = _call_api(config, "getMe")
    _print_result("getMe", status_code, payload)
    if status_code != 200 or payload.get("ok") is not True:
        return 1

    status_code, payload = _call_api(config, "getWebhookInfo")
    _print_result("getWebhookInfo", status_code, payload)

    status_code, payload = _call_api(config, "getChat", {"chat_id": config.allowed_chat_id})
    _print_result("getChat", status_code, payload)
    if status_code != 200 or payload.get("ok") is not True:
        return 1

    if args.send_ping:
        timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        text = f"telegram-codex-control smoke test {timestamp}"
        status_code, payload = _call_api(
            config,
            "sendMessage",
            {"chat_id": config.allowed_chat_id, "text": text},
        )
        _print_result("sendMessage", status_code, payload)
        if status_code != 200 or payload.get("ok") is not True:
            return 1

    if args.check_get_updates:
        status_code, payload = _call_api(config, "getUpdates", {"timeout": 0})
        _print_result("getUpdates", status_code, payload)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
