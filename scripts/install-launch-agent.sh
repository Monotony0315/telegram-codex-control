#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_SCRIPT="${PROJECT_DIR}/scripts/run-daemon.sh"
LABEL="${SERVICE_LABEL:-io.telegram-codex-control.bot}"
LEGACY_LABEL="com.angelhome.telegram-codex-control"
PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
LOG_DIR="${PROJECT_DIR}/logs"
OUT_LOG="${LOG_DIR}/launchd.out.log"
ERR_LOG="${LOG_DIR}/launchd.err.log"

if [[ ! -x "${RUN_SCRIPT}" ]]; then
  echo "Run script is missing or not executable: ${RUN_SCRIPT}" >&2
  exit 1
fi

mkdir -p "${PLIST_DIR}" "${LOG_DIR}"
chmod 700 "${LOG_DIR}" || true
touch "${OUT_LOG}" "${ERR_LOG}"
chmod 600 "${OUT_LOG}" "${ERR_LOG}" || true

cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${RUN_SCRIPT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>${OUT_LOG}</string>
  <key>StandardErrorPath</key>
  <string>${ERR_LOG}</string>
</dict>
</plist>
EOF

DOMAIN="gui/$(id -u)"
if ! launchctl print "${DOMAIN}" >/dev/null 2>&1; then
  DOMAIN="user/$(id -u)"
fi

if [[ "${LABEL}" != "${LEGACY_LABEL}" ]]; then
  LEGACY_PLIST="${PLIST_DIR}/${LEGACY_LABEL}.plist"
  launchctl bootout "${DOMAIN}" "${LEGACY_PLIST}" >/dev/null 2>&1 || true
  rm -f "${LEGACY_PLIST}"
fi

launchctl bootout "${DOMAIN}" "${PLIST_PATH}" >/dev/null 2>&1 || true
launchctl bootstrap "${DOMAIN}" "${PLIST_PATH}"
launchctl enable "${DOMAIN}/${LABEL}" || true
launchctl kickstart -k "${DOMAIN}/${LABEL}"

echo "Installed and started ${LABEL}"
echo "plist: ${PLIST_PATH}"
echo "stdout: ${OUT_LOG}"
echo "stderr: ${ERR_LOG}"
launchctl print "${DOMAIN}/${LABEL}" | sed -n '1,80p'
