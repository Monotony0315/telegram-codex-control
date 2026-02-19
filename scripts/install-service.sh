#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

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
