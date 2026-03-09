#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python not found: ${PYTHON_BIN}" >&2
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  "${PYTHON_BIN}" -m venv .venv
fi

VENV_PY=".venv/bin/python"
"${VENV_PY}" -m pip install --upgrade pip
"${VENV_PY}" -m pip install -e ".[dev]"

if [[ ! -f ".env" ]]; then
  cp ".env.example" ".env"
  chmod 600 ".env" || true
fi

if command -v codex >/dev/null 2>&1; then
  CODEX_PATH="$(command -v codex)"
  perl -0pi -e "s#^CODEX_COMMAND=codex\$#CODEX_COMMAND=${CODEX_PATH}#m" ".env"
fi

if [[ ! -d "${HOME}/Projects" ]]; then
  perl -0pi -e 's#^WORKSPACE_ROOT=\\$HOME/Projects$#WORKSPACE_ROOT=$HOME#m' ".env"
fi

mkdir -p ".data" "logs"
chmod 700 ".data" "logs" || true

LIVE_CORE_PATH=""
if command -v cargo >/dev/null 2>&1; then
  if LIVE_CORE_PATH="$("${PROJECT_DIR}/scripts/build-live-core.sh" 2>/dev/null)"; then
    if [[ -n "${LIVE_CORE_PATH}" ]]; then
      perl -0pi -e "s#^CODEX_LIVE_CORE_COMMAND=\$#CODEX_LIVE_CORE_COMMAND=${LIVE_CORE_PATH}#m" ".env"
    fi
  else
    echo "Skipping live-core helper build; cargo build did not succeed." >&2
  fi
fi

echo "Bootstrap complete."
echo "Edit ${PROJECT_DIR}/.env and set:"
echo "  TELEGRAM_BOT_TOKEN"
echo "  ALLOWED_USER_ID"
echo "  ALLOWED_CHAT_ID"
echo
echo "Run locally:"
echo "  set -a; source .env; set +a"
echo "  PYTHONPATH=src .venv/bin/python -m telegram_codex_control.main"
if [[ -n "${LIVE_CORE_PATH}" ]]; then
  echo
  echo "Rust live-core helper installed:"
  echo "  ${LIVE_CORE_PATH}"
fi
echo
echo "Install as a background service:"
echo "  ./scripts/install-service.sh"
