#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST_PATH="${PROJECT_DIR}/rust/live-core/Cargo.toml"
TARGET_DIR="${PROJECT_DIR}/rust/live-core/target/release"
INSTALL_DIR="${PROJECT_DIR}/.data/bin"
BIN_NAME="tgcc-live-core"
SOURCE_BIN="${TARGET_DIR}/${BIN_NAME}"
DEST_BIN="${INSTALL_DIR}/${BIN_NAME}"

if ! command -v cargo >/dev/null 2>&1; then
  echo "cargo not found; skipping live-core build" >&2
  exit 1
fi

if [[ ! -f "${MANIFEST_PATH}" ]]; then
  echo "live-core manifest not found: ${MANIFEST_PATH}" >&2
  exit 1
fi

mkdir -p "${INSTALL_DIR}"

cargo build --release --manifest-path "${MANIFEST_PATH}"

install -m 755 "${SOURCE_BIN}" "${DEST_BIN}"

echo "${DEST_BIN}"
