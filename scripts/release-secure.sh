#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

./scripts/release-build.sh
./scripts/generate-sbom.sh dist/sbom.cdx.json
./scripts/sign-artifacts.sh

echo
echo "Secure release bundle created in dist/:"
ls -lh dist/
