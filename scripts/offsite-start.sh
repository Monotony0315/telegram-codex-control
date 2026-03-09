#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${PROJECT_DIR}/.data"
LOG_DIR="${PROJECT_DIR}/logs"
PID_FILE="${DATA_DIR}/offsite-supervisor.pid"
LOG_FILE="${LOG_DIR}/offsite-supervisor.log"

mkdir -p "${DATA_DIR}" "${LOG_DIR}"

if [[ -f "${PID_FILE}" ]]; then
  existing_pid="$(tr -d '[:space:]' < "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${existing_pid}" ]] && kill -0 "${existing_pid}" 2>/dev/null; then
    echo "offsite supervisor already running (pid ${existing_pid})"
    exit 0
  fi
fi

nohup "${PROJECT_DIR}/scripts/offsite-supervisor.sh" >> "${LOG_FILE}" 2>&1 &
supervisor_pid=$!
printf '%s\n' "${supervisor_pid}" > "${PID_FILE}"

sleep 1
if kill -0 "${supervisor_pid}" 2>/dev/null; then
  echo "offsite supervisor started (pid ${supervisor_pid})"
  echo "log: ${LOG_FILE}"
else
  echo "failed to start offsite supervisor" >&2
  exit 1
fi
