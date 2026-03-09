#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

set -a
if [[ -f ".env" ]]; then
  # shellcheck disable=SC1091
  source ".env"
fi
set +a

if [[ -n "${CODEX_COMMAND:-}" ]]; then
  CODEX_BIN="${CODEX_COMMAND}"
elif command -v codex >/dev/null 2>&1; then
  CODEX_BIN="$(command -v codex)"
else
  echo "codex executable not found" >&2
  exit 127
fi

exec "${CODEX_BIN}" --dangerously-bypass-approvals-and-sandbox --search "$@"
