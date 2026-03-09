#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v cargo >/dev/null 2>&1; then
  "${PROJECT_DIR}/scripts/build-live-core.sh" >/dev/null || true
fi

case "$(uname -s)" in
  Darwin)
    exec "${PROJECT_DIR}/scripts/install-launch-agent.sh"
    ;;
  Linux)
    exec "${PROJECT_DIR}/scripts/install-systemd-user.sh"
    ;;
  *)
    echo "Unsupported platform: $(uname -s)" >&2
    exit 1
    ;;
esac
