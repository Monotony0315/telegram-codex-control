# Deployment Guide

## 1) Prerequisites
- Python 3.12+
- Codex CLI installed and authenticated
- Telegram bot token + allowed chat/user IDs

## 2) Bootstrap
```bash
./scripts/bootstrap.sh
```

Edit `.env`:
- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_USER_ID`
- `ALLOWED_CHAT_ID`
- `WORKSPACE_ROOT`
- `CODEX_COMMAND` (absolute path recommended)

## 3) Local Smoke Test
```bash
set -a; source .env; set +a
PYTHONPATH=src .venv/bin/python -m telegram_codex_control.main
```

In Telegram:
- Send `/status`
- Send `/autopilot hello` then `/confirm <nonce>`

## 4) Install Background Service
Cross-platform wrapper:
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

## 5) Platform Details

### macOS (`launchd`)
- Uses `scripts/install-launch-agent.sh`
- Plist path: `~/Library/LaunchAgents/<SERVICE_LABEL>.plist`
- Default label: `io.telegram-codex-control.bot`

### Linux (`systemd --user`)
- Uses `scripts/install-systemd-user.sh`
- Unit path: `~/.config/systemd/user/<SERVICE_LABEL>.service`
- Default label: `telegram-codex-control`
- Optional for boot without user login:
```bash
loginctl enable-linger "$USER"
```

## 6) Build Release Artifacts
```bash
./scripts/release-build.sh
```

Output:
- `dist/*.whl`
- `dist/*.tar.gz`

## 7) Recommended Production Settings
- `CODEX_COMMAND` absolute path (avoid PATH mismatch in services)
- `WORKSPACE_ROOT` dedicated directory
- Separate Telegram bot token per environment
- Rotate tokens periodically
