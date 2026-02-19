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

echo "Bootstrap complete."
echo "Edit ${PROJECT_DIR}/.env and set:"
echo "  TELEGRAM_BOT_TOKEN"
echo "  ALLOWED_USER_ID"
echo "  ALLOWED_CHAT_ID"
echo
echo "Run locally:"
echo "  set -a; source .env; set +a"
echo "  PYTHONPATH=src .venv/bin/python -m telegram_codex_control.main"
echo
echo "Install as a background service:"
echo "  ./scripts/install-service.sh"
