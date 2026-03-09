#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${PROJECT_DIR}/.data"
PID_FILE="${DATA_DIR}/offsite-supervisor.pid"
CHILD_PID_FILE="${DATA_DIR}/offsite-daemon.pid"

if [[ ! -f "${PID_FILE}" ]]; then
  echo "offsite supervisor is not running"
  exit 0
fi

supervisor_pid="$(tr -d '[:space:]' < "${PID_FILE}" 2>/dev/null || true)"
if [[ -z "${supervisor_pid}" ]]; then
  rm -f "${PID_FILE}"
  echo "offsite supervisor pid file was empty; cleaned up"
  exit 0
fi

if ! kill -0 "${supervisor_pid}" 2>/dev/null; then
  rm -f "${PID_FILE}" "${CHILD_PID_FILE}"
  echo "offsite supervisor pid ${supervisor_pid} not running; cleaned up stale pid files"
  exit 0
fi

kill "${supervisor_pid}" 2>/dev/null || true

for _ in {1..20}; do
  if ! kill -0 "${supervisor_pid}" 2>/dev/null; then
    rm -f "${PID_FILE}" "${CHILD_PID_FILE}"
    echo "offsite supervisor stopped"
    exit 0
  fi
  sleep 1
done

echo "offsite supervisor did not stop within timeout" >&2
exit 1
