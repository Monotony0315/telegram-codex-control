#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

if ! "${PYTHON_BIN}" - <<'PY'
import importlib.util
import sys

required = ("build", "wheel")
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    print("MISSING", ",".join(missing))
    raise SystemExit(1)
PY
then
  "${PYTHON_BIN}" -m pip install --upgrade build wheel
fi

rm -rf dist/ build/
"${PYTHON_BIN}" -m build --no-isolation

echo "Release artifacts:"
ls -lh dist/
