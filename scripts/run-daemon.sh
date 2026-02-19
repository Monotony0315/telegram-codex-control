#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

if [[ ! -f ".env" ]]; then
  echo "Missing .env at ${PROJECT_DIR}/.env" >&2
  exit 1
fi

set -a
source ".env"
set +a

mkdir -p ".data" "logs"
chmod 700 ".data" "logs" || true

# launchd often has a minimal PATH; make codex resolution deterministic.
if [[ -z "${CODEX_COMMAND:-}" || "${CODEX_COMMAND}" == "codex" ]]; then
  if command -v codex >/dev/null 2>&1; then
    CODEX_COMMAND="$(command -v codex)"
    export CODEX_COMMAND
  fi
fi

DEFAULT_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH="${DEFAULT_PATH}:${PATH:-}"

if [[ -d "${HOME}/.nvm/versions/node" ]]; then
  NVM_BIN="$(ls -1d "${HOME}"/.nvm/versions/node/*/bin 2>/dev/null | tail -n1 || true)"
  if [[ -n "${NVM_BIN}" ]]; then
    export PATH="${NVM_BIN}:${PATH}"
  fi
fi

export PYTHONUNBUFFERED=1
export PYTHONPATH="${PROJECT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

exec "${PYTHON_BIN}" -m telegram_codex_control.main
