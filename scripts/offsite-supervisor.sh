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
LOG_DIR="${PROJECT_DIR}/logs"
PID_FILE="${DATA_DIR}/offsite-supervisor.pid"
CHILD_PID_FILE="${DATA_DIR}/offsite-daemon.pid"
AUDIT_LOG="${AUDIT_LOG_PATH:-${DATA_DIR}/audit.jsonl}"
STALE_TIMEOUT_SECONDS="${OFFSITE_STALE_TIMEOUT_SECONDS:-900}"
CHECK_INTERVAL_SECONDS="${OFFSITE_CHECK_INTERVAL_SECONDS:-15}"

cd "${PROJECT_DIR}"
mkdir -p "${DATA_DIR}" "${LOG_DIR}"

if ! [[ "${STALE_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] || ! [[ "${CHECK_INTERVAL_SECONDS}" =~ ^[0-9]+$ ]]; then
  echo "OFFSITE_STALE_TIMEOUT_SECONDS and OFFSITE_CHECK_INTERVAL_SECONDS must be integers" >&2
  exit 1
fi

if (( STALE_TIMEOUT_SECONDS < 1 || CHECK_INTERVAL_SECONDS < 1 )); then
  echo "Timeout and interval values must be >= 1" >&2
  exit 1
fi

get_mtime_epoch() {
  local file="$1"
  if [[ ! -f "${file}" ]]; then
    echo 0
    return
  fi

  local mtime
  if mtime="$(stat -f '%m' "${file}" 2>/dev/null)"; then
    echo "${mtime}"
  elif mtime="$(stat -c '%Y' "${file}" 2>/dev/null)"; then
    echo "${mtime}"
  else
    echo 0
  fi
}

start_child() {
  "${PROJECT_DIR}/scripts/run-daemon.sh" &
  CHILD_PID=$!
  printf '%s\n' "${CHILD_PID}" > "${CHILD_PID_FILE}"
  echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') started daemon pid=${CHILD_PID}"
}

stop_child() {
  local pid="$1"
  if kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
  fi
}

cleanup() {
  local code=$?
  if [[ -n "${CHILD_PID:-}" ]]; then
    stop_child "${CHILD_PID}"
  fi
  rm -f "${CHILD_PID_FILE}" "${PID_FILE}"
  exit "${code}"
}

trap cleanup EXIT INT TERM

printf '%s\n' "$$" > "${PID_FILE}"
start_child

while true; do
  if ! kill -0 "${CHILD_PID}" 2>/dev/null; then
    wait "${CHILD_PID}" 2>/dev/null || true
    echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') daemon exited; restarting"
    start_child
    sleep "${CHECK_INTERVAL_SECONDS}"
    continue
  fi

  now_epoch="$(date +%s)"
  audit_mtime_epoch="$(get_mtime_epoch "${AUDIT_LOG}")"

  if (( audit_mtime_epoch > 0 && now_epoch - audit_mtime_epoch > STALE_TIMEOUT_SECONDS )); then
    echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') audit stale for $((now_epoch - audit_mtime_epoch))s; restarting daemon"
    stop_child "${CHILD_PID}"
    start_child
  fi

  sleep "${CHECK_INTERVAL_SECONDS}"
done
