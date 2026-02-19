# Telegram Codex Control

Telegram-based remote control plane for Codex CLI.  
Use Telegram commands to run development tasks (`/run`, `/autopilot`), monitor status, and operate a persistent background daemon.

Keywords: `telegram bot`, `codex cli`, `remote development`, `developer automation`, `autopilot coding`.

## Features
- Long polling via Telegram Bot API (`getUpdates`)
- Single-user allowlist (`ALLOWED_USER_ID`, `ALLOWED_CHAT_ID`)
- Safety confirmation flow (`/run` and `/autopilot` require `/confirm <nonce>`)
- One active job at a time (in-memory + SQLite uniqueness)
- Shell-injection resistant execution (`asyncio.create_subprocess_exec`, argv-only)
- Event/audit persistence (`.data/state.db`, `.data/audit.jsonl`)
- Service automation:
  - macOS: `launchd` (`RunAtLoad`, `KeepAlive`)
  - Linux: `systemd --user` (`Restart=always`)

## Bot Commands
- `/status`
- `/run <prompt>`
- `/autopilot <task>`
- `/confirm <nonce>`
- `/cancel`
- `/logs`

## Quick Start
```bash
cd /path/to/telegram-codex-control
./scripts/bootstrap.sh
```

Then update `.env`:
```env
TELEGRAM_BOT_TOKEN=123456:replace-me
ALLOWED_USER_ID=123456789
ALLOWED_CHAT_ID=123456789
WORKSPACE_ROOT=$HOME/Projects
CODEX_COMMAND=/absolute/path/to/codex
```

Run locally:
```bash
set -a; source .env; set +a
PYTHONPATH=src .venv/bin/python -m telegram_codex_control.main
```

## Background Service (Auto-start)
Install/start:
```bash
./scripts/install-service.sh
```

Check status/logs:
```bash
./scripts/status-service.sh
```

Uninstall:
```bash
./scripts/uninstall-service.sh
```

## Release Build
Create distributable artifacts (`sdist`, `wheel`):
```bash
./scripts/release-build.sh
```

Artifacts are generated in `dist/`.

## Environment Variables
- `TELEGRAM_BOT_TOKEN` (required)
- `ALLOWED_USER_ID` (required int)
- `ALLOWED_CHAT_ID` (required int)
- `WORKSPACE_ROOT` (default: `~/Projects` if present, otherwise `~`)
- `DB_PATH` (default: `.data/state.db`)
- `AUDIT_LOG_PATH` (default: `.data/audit.jsonl`)
- `CODEX_COMMAND` (default: `codex`)
- `POLL_TIMEOUT_SECONDS` (default: `30`)
- `POLL_RETRY_BASE_SECONDS` (default: `1.0`)
- `POLL_RETRY_MAX_SECONDS` (default: `30.0`)
- `JOB_TIMEOUT_SECONDS` (default: `7200`)
- `CONFIRMATION_TTL_SECONDS` (default: `300`)
- `MESSAGE_CHUNK_SIZE` (default: `3500`)
- `TELEGRAM_API_BASE` (default: `https://api.telegram.org`)

## Security Notes
- Bot token and API keys are redacted from logs/messages.
- Job execution is restricted to `WORKSPACE_ROOT`.
- `/run` blocks autopilot-like prompts; use `/autopilot` with explicit confirmation.
- `.env`, runtime DB, and logs are git-ignored by default.

See `docs/SECURITY.md` for hardening checklist.

## Docs
- Deployment guide: `docs/DEPLOYMENT.md`
- Security checklist: `docs/SECURITY.md`
- GitHub SEO checklist: `docs/SEO.md`
- Changelog: `CHANGELOG.md`
