# Deployment Guide

## 1) Prerequisites
- Python 3.12+
- Codex CLI installed and authenticated
- Telegram bot token and chat/user IDs

## 2) Bootstrap
```bash
./scripts/bootstrap.sh
```

Edit `.env` with at least:
- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_USER_ID`
- `ALLOWED_CHAT_ID`
- `WORKSPACE_ROOT`
- `CODEX_COMMAND`

Optional:
- `COMMAND_POLICY_PATH`
- webhook variables (`TELEGRAM_TRANSPORT=webhook`, ...)

## 3) Local Smoke Test
```bash
set -a; source .env; set +a
PYTHONPATH=src .venv/bin/python -m telegram_codex_control.main
```

In Telegram:
1. `/status`
2. `/codex --help` then `/confirm <nonce>`

## 4) Production Transport

### Polling (default)
- No inbound port required.
- Service automatically calls `deleteWebhook` to avoid API mode conflicts.

### Webhook
Set:
```env
TELEGRAM_TRANSPORT=webhook
TELEGRAM_WEBHOOK_PUBLIC_URL=https://bot.example.com
TELEGRAM_WEBHOOK_LISTEN_HOST=127.0.0.1
TELEGRAM_WEBHOOK_LISTEN_PORT=8080
TELEGRAM_WEBHOOK_PATH=/telegram/webhook
TELEGRAM_WEBHOOK_SECRET_TOKEN=replace-me
```

Requirements:
- Public URL must be reachable by Telegram over HTTPS.
- Reverse proxy should route `WEBHOOK_PATH` to local listener.
- `TELEGRAM_WEBHOOK_SECRET_TOKEN` is required (minimum 16 chars).

## 5) Background Service
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

Platform notes:
- macOS: LaunchAgent under `~/Library/LaunchAgents`
- Linux: systemd user unit under `~/.config/systemd/user`

## 6) Command Policy
Template:
```bash
cp command-policy.example.json command-policy.json
```

Point `.env`:
```env
COMMAND_POLICY_PATH=./command-policy.json
```

Recommendation:
- Keep one admin rule with `allow: ["*"]`
- Give non-admin identities only `/status`, `/logs`, `/help`, `/cancel`

## 7) Secure Release
```bash
./scripts/release-secure.sh
```

Output in `dist/`:
- wheel + sdist
- SBOM (`sbom.cdx.json`)
- checksums (`SHA256SUMS.txt`)
- signatures (`.sig` or `.asc`) when key configured

Signing options:
- OpenSSL: `RELEASE_PRIVATE_KEY_PATH=/path/to/private.pem`
- GPG: `RELEASE_GPG_KEY_ID=<key-id>`

CI tag releases (`.github/workflows/release.yml`) require `RELEASE_PRIVATE_KEY_PEM` secret.
