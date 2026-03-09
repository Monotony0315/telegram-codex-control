#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OFFSITE_START_SCRIPT="${PROJECT_DIR}/scripts/offsite-start.sh"
PROFILE_PATH_RAW="${OFFSITE_LOGIN_PROFILE_PATH:-${HOME}/.zprofile}"
PROFILE_PATH="${PROFILE_PATH_RAW/#\~/${HOME}}"
BEGIN_MARKER="# >>> telegram-codex-control offsite login autostart >>>"
END_MARKER="# <<< telegram-codex-control offsite login autostart <<<"

if [[ ! -x "${OFFSITE_START_SCRIPT}" ]]; then
  echo "offsite start script is missing or not executable: ${OFFSITE_START_SCRIPT}" >&2
  exit 1
fi

mkdir -p "$(dirname -- "${PROFILE_PATH}")"
touch "${PROFILE_PATH}"

tmp_file="$(mktemp "${PROFILE_PATH}.tmp.XXXXXX")"
trap 'rm -f "${tmp_file}"' EXIT

awk -v begin="${BEGIN_MARKER}" -v end="${END_MARKER}" '
$0 == begin { skip = 1; next }
$0 == end { skip = 0; next }
!skip { print }
' "${PROFILE_PATH}" > "${tmp_file}"

if [[ -s "${tmp_file}" ]]; then
  printf '\n' >> "${tmp_file}"
fi

cat >> "${tmp_file}" <<BLOCK
${BEGIN_MARKER}
if [[ -x "${OFFSITE_START_SCRIPT}" ]]; then
  "${OFFSITE_START_SCRIPT}" >/dev/null 2>&1 || true
fi
${END_MARKER}
BLOCK

mv "${tmp_file}" "${PROFILE_PATH}"
trap - EXIT

echo "Installed offsite login autostart block into ${PROFILE_PATH}"
