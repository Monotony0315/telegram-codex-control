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
- `CODEX_LIVE_CORE_COMMAND` (optional, recommended if you built the Rust helper)

Optional:
- `COMMAND_POLICY_PATH`
- webhook variables (`TELEGRAM_TRANSPORT=webhook`, ...)

## 3) Local Smoke Test
```bash
set -a; source .env; set +a
PYTHONPATH=src .venv/bin/python -m telegram_codex_control.main
```

Optional helper build:
```bash
./scripts/build-live-core.sh
```

Notes:
- `bootstrap.sh` builds the helper automatically when `cargo` is available.
- `install-service.sh` refreshes the helper before installing the service when `cargo` is available.
- `run-daemon.sh` auto-uses `./.data/bin/tgcc-live-core` when present, even if `.env` leaves `CODEX_LIVE_CORE_COMMAND` empty.

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

## 5b) Offsite Operation (No launchctl/systemd/ps)
Note:
- Run either background service mode (`install-service`) or offsite mode, not both at once.

Start workspace-local supervisor:
```bash
./scripts/offsite-start.sh
```

Check health and recent audit events:
```bash
./scripts/offsite-status.sh
```

Stop supervisor:
```bash
./scripts/offsite-stop.sh
```

Enable login-shell autostart (default profile: `~/.zprofile`):
```bash
./scripts/install-offsite-login-autostart.sh
```

Disable login-shell autostart:
```bash
./scripts/uninstall-offsite-login-autostart.sh
```

Optional profile override for testing/special cases:
```bash
OFFSITE_LOGIN_PROFILE_PATH=~/.zshrc ./scripts/install-offsite-login-autostart.sh
```

Offsite stale timeout examples (`.env`):
```env
OFFSITE_STALE_TIMEOUT_SECONDS=600
OFFSITE_CHECK_INTERVAL_SECONDS=10
```

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
