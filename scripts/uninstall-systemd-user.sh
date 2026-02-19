#!/usr/bin/env bash
set -Eeuo pipefail

LABEL="${SERVICE_LABEL:-telegram-codex-control}"
UNIT_PATH="${HOME}/.config/systemd/user/${LABEL}.service"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found." >&2
  exit 1
fi

systemctl --user disable --now "${LABEL}.service" >/dev/null 2>&1 || true
rm -f "${UNIT_PATH}"
systemctl --user daemon-reload

echo "Uninstalled ${LABEL}.service"
