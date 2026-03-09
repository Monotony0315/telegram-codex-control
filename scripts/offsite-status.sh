#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

DATA_DIR="${PROJECT_DIR}/.data"
PID_FILE="${DATA_DIR}/offsite-supervisor.pid"
AUDIT_LOG="${AUDIT_LOG_PATH:-${DATA_DIR}/audit.jsonl}"

get_mtime_epoch() {
  local file="$1"
  local mtime

  if [[ ! -f "${file}" ]]; then
    echo 0
    return
  fi

  if mtime="$(stat -f '%m' "${file}" 2>/dev/null)"; then
    echo "${mtime}"
  elif mtime="$(stat -c '%Y' "${file}" 2>/dev/null)"; then
    echo "${mtime}"
  else
    echo 0
  fi
}

get_mtime_human() {
  local file="$1"
  local mtime

  if [[ ! -f "${file}" ]]; then
    echo "n/a"
    return
  fi

  if mtime="$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S %z' "${file}" 2>/dev/null)"; then
    echo "${mtime}"
  elif mtime="$(stat -c '%y' "${file}" 2>/dev/null)"; then
    echo "${mtime}"
  else
    echo "n/a"
  fi
}

redact_secrets() {
  sed -E 's#bot[0-9]{6,}:[A-Za-z0-9_-]+#bot***:REDACTED#g'
}

echo "== offsite supervisor =="
if [[ -f "${PID_FILE}" ]]; then
  supervisor_pid="$(tr -d '[:space:]' < "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${supervisor_pid}" ]] && kill -0 "${supervisor_pid}" 2>/dev/null; then
    echo "state: running"
    echo "pid: ${supervisor_pid}"
  else
    echo "state: not running (stale pid file)"
  fi
else
  echo "state: not running"
fi

echo
echo "== audit activity =="
if [[ -f "${AUDIT_LOG}" ]]; then
  now_epoch="$(date +%s)"
  audit_mtime_epoch="$(get_mtime_epoch "${AUDIT_LOG}")"
  audit_mtime_human="$(get_mtime_human "${AUDIT_LOG}")"
  if (( audit_mtime_epoch > 0 )); then
    echo "file: ${AUDIT_LOG}"
    echo "last_modified: ${audit_mtime_human}"
    echo "age_seconds: $((now_epoch - audit_mtime_epoch))"
  else
    echo "file: ${AUDIT_LOG}"
    echo "last_modified: unavailable"
  fi

  echo "recent_events:"
  tail -n 5 "${AUDIT_LOG}" | redact_secrets || true
else
  echo "file: ${AUDIT_LOG} (missing)"
fi
