#!/usr/bin/env bash
set -Eeuo pipefail

LABEL="${SERVICE_LABEL:-io.telegram-codex-control.bot}"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"

DOMAIN="gui/$(id -u)"
if ! launchctl print "${DOMAIN}" >/dev/null 2>&1; then
  DOMAIN="user/$(id -u)"
fi

launchctl bootout "${DOMAIN}" "${PLIST_PATH}" >/dev/null 2>&1 || true
rm -f "${PLIST_PATH}"

echo "Uninstalled ${LABEL}"
