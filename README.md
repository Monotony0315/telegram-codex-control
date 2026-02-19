# Telegram Codex Control

Telegram remote control for Codex CLI with policy-based access control, polling/webhook transports, and secure release tooling.

Keywords: `telegram bot`, `codex cli`, `remote development`, `developer automation`, `webhook`, `sbom`, `signed release`.

## Features
- Command surface:
  - `/status`
  - `/run <prompt>` (confirm required)
  - `/autopilot <task>` (confirm required)
  - `/codex <raw args...>` (confirm required)
  - `/confirm <nonce>`
  - `/cancel`
  - `/logs`
  - `/help`
- Transport modes:
  - `polling` (default)
  - `webhook` (Telegram `setWebhook` + local HTTP receiver)
- Security:
  - strict user/chat allowlist
  - optional command policy (`COMMAND_POLICY_PATH`)
  - argv-only subprocess execution (`create_subprocess_exec`, no shell)
  - token/secret redaction in logs and outbound messages
- Operations:
  - SQLite state + JSONL audit
  - macOS `launchd` and Linux `systemd --user` service scripts
  - release pipeline with SBOM + artifact signing/checksums

## Quick Start
```bash
cd /path/to/telegram-codex-control
./scripts/bootstrap.sh
```

Update `.env`:
```env
TELEGRAM_BOT_TOKEN=123456:replace-me
ALLOWED_USER_ID=123456789
ALLOWED_CHAT_ID=123456789
WORKSPACE_ROOT=$HOME/Projects
CODEX_COMMAND=/absolute/path/to/codex
TELEGRAM_TRANSPORT=polling
# COMMAND_POLICY_PATH=./command-policy.example.json
```

Run locally:
```bash
set -a; source .env; set +a
PYTHONPATH=src .venv/bin/python -m telegram_codex_control.main
```

## Webhook Mode
Set:
```env
TELEGRAM_TRANSPORT=webhook
TELEGRAM_WEBHOOK_PUBLIC_URL=https://your-public-host.example.com
TELEGRAM_WEBHOOK_LISTEN_HOST=127.0.0.1
TELEGRAM_WEBHOOK_LISTEN_PORT=8080
TELEGRAM_WEBHOOK_PATH=/telegram/webhook
TELEGRAM_WEBHOOK_SECRET_TOKEN=replace-me
```

Notes:
- `TELEGRAM_WEBHOOK_PUBLIC_URL` must be HTTPS and reachable by Telegram.
- `TELEGRAM_WEBHOOK_SECRET_TOKEN` is required in webhook mode.
- Route `PUBLIC_URL + WEBHOOK_PATH` to this service.
- In polling mode, webhook registration is removed automatically for compatibility.

## Command Policy
Set `COMMAND_POLICY_PATH` to a JSON file.  
Use `command-policy.example.json` as a template.

Example:
```json
{
  "default": { "allow": ["/status", "/logs", "/help"] },
  "rules": [
    { "user_id": 123, "chat_id": 456, "allow": ["*"], "deny": [] },
    { "user_id": 111, "chat_id": -100222, "allow": ["/status", "/logs"], "deny": ["/run", "/autopilot", "/codex"] }
  ]
}
```

## Background Service (Auto-start)
Install/start:
```bash
./scripts/install-service.sh
```

Status:
```bash
./scripts/status-service.sh
```

Uninstall:
```bash
./scripts/uninstall-service.sh
```

## Secure Release
Build + SBOM + signatures/checksums:
```bash
./scripts/release-secure.sh
```

Optional signing setup:
- OpenSSL key:
  - `RELEASE_PRIVATE_KEY_PATH=/path/to/private.pem`
- GPG key:
  - `RELEASE_GPG_KEY_ID=<key-id>`
- Enforce signatures:
  - `REQUIRE_ARTIFACT_SIGNATURES=1`

For GitHub tag releases, workflow requires `RELEASE_PRIVATE_KEY_PEM` secret and enforces signatures.

## Environment Variables
- `TELEGRAM_BOT_TOKEN` (required)
- `ALLOWED_USER_ID` (required int)
- `ALLOWED_CHAT_ID` (required int)
- `WORKSPACE_ROOT` (default: `~/Projects` if exists, else `~`)
- `DB_PATH` (default: `.data/state.db`)
- `AUDIT_LOG_PATH` (default: `.data/audit.jsonl`)
- `CODEX_COMMAND` (default: `codex`)
- `COMMAND_POLICY_PATH` (optional)
- `TELEGRAM_TRANSPORT` (`polling` or `webhook`, default `polling`)
- `TELEGRAM_WEBHOOK_PUBLIC_URL` (required for webhook mode)
- `TELEGRAM_WEBHOOK_LISTEN_HOST` (default `127.0.0.1`)
- `TELEGRAM_WEBHOOK_LISTEN_PORT` (default `8080`)
- `TELEGRAM_WEBHOOK_PATH` (default `/telegram/webhook`)
- `TELEGRAM_WEBHOOK_SECRET_TOKEN` (optional)
- `POLL_TIMEOUT_SECONDS` (default `30`)
- `POLL_RETRY_BASE_SECONDS` (default `1.0`)
- `POLL_RETRY_MAX_SECONDS` (default `30.0`)
- `JOB_TIMEOUT_SECONDS` (default `7200`)
- `CONFIRMATION_TTL_SECONDS` (default `300`)
- `MESSAGE_CHUNK_SIZE` (default `3500`)
- `TELEGRAM_API_BASE` (default `https://api.telegram.org`)

## Testing
```bash
pytest -q
```

## Docs
- Deployment guide: `docs/DEPLOYMENT.md`
- Security checklist: `docs/SECURITY.md`
- SEO checklist: `docs/SEO.md`
- Changelog: `CHANGELOG.md`
