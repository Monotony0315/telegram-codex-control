#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${SERVICE_LABEL:-telegram-codex-control}"
LOG_DIR="${PROJECT_DIR}/logs"
OUT_LOG="${LOG_DIR}/systemd.out.log"
ERR_LOG="${LOG_DIR}/systemd.err.log"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found." >&2
  exit 1
fi

echo "== systemd service =="
systemctl --user --no-pager --full status "${LABEL}.service" | sed -n '1,120p'

echo
echo "== recent stdout =="
tail -n 40 "${OUT_LOG}" 2>/dev/null || true

echo
echo "== recent stderr =="
tail -n 40 "${ERR_LOG}" 2>/dev/null || true
