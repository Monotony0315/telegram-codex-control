#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_SCRIPT="${PROJECT_DIR}/scripts/run-daemon.sh"
LABEL="${SERVICE_LABEL:-telegram-codex-control}"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_PATH="${UNIT_DIR}/${LABEL}.service"
LOG_DIR="${PROJECT_DIR}/logs"
OUT_LOG="${LOG_DIR}/systemd.out.log"
ERR_LOG="${LOG_DIR}/systemd.err.log"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found. This script requires systemd user services." >&2
  exit 1
fi

if [[ ! -x "${RUN_SCRIPT}" ]]; then
  echo "Run script is missing or not executable: ${RUN_SCRIPT}" >&2
  exit 1
fi

mkdir -p "${UNIT_DIR}" "${LOG_DIR}"
chmod 700 "${LOG_DIR}" || true
touch "${OUT_LOG}" "${ERR_LOG}"
chmod 600 "${OUT_LOG}" "${ERR_LOG}" || true

cat > "${UNIT_PATH}" <<EOF
[Unit]
Description=Telegram Codex Control Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${RUN_SCRIPT}
Restart=always
RestartSec=5
StandardOutput=append:${OUT_LOG}
StandardError=append:${ERR_LOG}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "${LABEL}.service"

echo "Installed and started ${LABEL}.service"
echo "unit: ${UNIT_PATH}"
echo "stdout: ${OUT_LOG}"
echo "stderr: ${ERR_LOG}"
systemctl --user --no-pager --full status "${LABEL}.service" | sed -n '1,60p'
