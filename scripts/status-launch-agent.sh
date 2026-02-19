#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${SERVICE_LABEL:-io.telegram-codex-control.bot}"
LOG_DIR="${PROJECT_DIR}/logs"
OUT_LOG="${LOG_DIR}/launchd.out.log"
ERR_LOG="${LOG_DIR}/launchd.err.log"

DOMAIN="gui/$(id -u)"
if ! launchctl print "${DOMAIN}" >/dev/null 2>&1; then
  DOMAIN="user/$(id -u)"
fi

echo "== launchd service =="
launchctl print "${DOMAIN}/${LABEL}" | sed -n '1,120p'

echo
echo "== recent stdout =="
tail -n 40 "${OUT_LOG}" 2>/dev/null || true

echo
echo "== recent stderr =="
tail -n 40 "${ERR_LOG}" 2>/dev/null || true
