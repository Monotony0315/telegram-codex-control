#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

OUTPUT_PATH="${1:-dist/sbom.cdx.json}"
mkdir -p "$(dirname "${OUTPUT_PATH}")"

if command -v syft >/dev/null 2>&1; then
  syft "dir:${PROJECT_DIR}" -o "cyclonedx-json=${OUTPUT_PATH}"
  echo "SBOM generated with syft: ${OUTPUT_PATH}"
  exit 0
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

"${PYTHON_BIN}" - <<'PY' "${OUTPUT_PATH}" "${PROJECT_DIR}"
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import uuid

out = Path(sys.argv[1])
project_dir = Path(sys.argv[2])

try:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--format=json"],
        check=True,
        capture_output=True,
        text=True,
    )
    packages = json.loads(proc.stdout)
except Exception:
    packages = []

components = []
for item in packages:
    name = item.get("name")
    version = item.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        continue
    components.append(
        {
            "type": "library",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{name}@{version}",
        }
    )

sbom = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.5",
    "version": 1,
    "serialNumber": f"urn:uuid:{uuid.uuid4()}",
    "metadata": {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "component": {
            "type": "application",
            "name": "telegram-codex-control",
            "version": "0.3.0",
            "bom-ref": "telegram-codex-control",
        },
        "tools": [
            {
                "vendor": "custom",
                "name": "generate-sbom.sh",
            }
        ],
    },
    "components": components,
}

out.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
print(f"SBOM generated: {out}")
print(f"components: {len(components)}")
PY
