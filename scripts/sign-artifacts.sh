#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

DIST_DIR="${DIST_DIR:-dist}"
REQUIRE_SIGS="${REQUIRE_ARTIFACT_SIGNATURES:-0}"

if [[ ! -d "${DIST_DIR}" ]]; then
  echo "dist directory not found: ${DIST_DIR}" >&2
  exit 1
fi

TARGETS=()
while IFS= read -r line; do
  TARGETS+=("${line}")
done < <(find "${DIST_DIR}" -maxdepth 1 -type f \( -name "*.whl" -o -name "*.tar.gz" -o -name "*.json" \) | sort)
if [[ ${#TARGETS[@]} -eq 0 ]]; then
  echo "No artifacts found in ${DIST_DIR}" >&2
  exit 1
fi

(
  cd "${DIST_DIR}"
  shasum -a 256 ./* > SHA256SUMS.txt
)
echo "Checksums generated: ${DIST_DIR}/SHA256SUMS.txt"

if [[ -n "${RELEASE_PRIVATE_KEY_PATH:-}" ]]; then
  if [[ ! -f "${RELEASE_PRIVATE_KEY_PATH}" ]]; then
    echo "RELEASE_PRIVATE_KEY_PATH not found: ${RELEASE_PRIVATE_KEY_PATH}" >&2
    exit 1
  fi
  for file in "${TARGETS[@]}"; do
    openssl dgst -sha256 -sign "${RELEASE_PRIVATE_KEY_PATH}" -out "${file}.sig" "${file}"
    echo "Signed (openssl): ${file}.sig"
  done
  openssl pkey -in "${RELEASE_PRIVATE_KEY_PATH}" -pubout -out "${DIST_DIR}/release-public-key.pem"
  echo "Public key exported: ${DIST_DIR}/release-public-key.pem"
  exit 0
fi

if [[ -n "${RELEASE_GPG_KEY_ID:-}" ]]; then
  if ! command -v gpg >/dev/null 2>&1; then
    echo "gpg not found, but RELEASE_GPG_KEY_ID is set" >&2
    exit 1
  fi
  for file in "${TARGETS[@]}"; do
    gpg --batch --yes --armor --local-user "${RELEASE_GPG_KEY_ID}" --detach-sign --output "${file}.asc" "${file}"
    echo "Signed (gpg): ${file}.asc"
  done
  exit 0
fi

if [[ "${REQUIRE_SIGS}" == "1" ]]; then
  echo "Artifact signing is required, but no signing method is configured." >&2
  echo "Set RELEASE_PRIVATE_KEY_PATH or RELEASE_GPG_KEY_ID." >&2
  exit 1
fi

echo "No signing key configured. Signatures were skipped (checksums only)."
