#!/usr/bin/env bash
set -Eeuo pipefail

PROFILE_PATH_RAW="${OFFSITE_LOGIN_PROFILE_PATH:-${HOME}/.zprofile}"
PROFILE_PATH="${PROFILE_PATH_RAW/#\~/${HOME}}"
BEGIN_MARKER="# >>> telegram-codex-control offsite login autostart >>>"
END_MARKER="# <<< telegram-codex-control offsite login autostart <<<"

if [[ ! -f "${PROFILE_PATH}" ]]; then
  echo "Profile not found, nothing to uninstall: ${PROFILE_PATH}"
  exit 0
fi

tmp_file="$(mktemp "${PROFILE_PATH}.tmp.XXXXXX")"
trap 'rm -f "${tmp_file}"' EXIT

awk -v begin="${BEGIN_MARKER}" -v end="${END_MARKER}" '
$0 == begin { skip = 1; next }
$0 == end { skip = 0; next }
!skip { print }
' "${PROFILE_PATH}" > "${tmp_file}"

mv "${tmp_file}" "${PROFILE_PATH}"
trap - EXIT

echo "Removed offsite login autostart block from ${PROFILE_PATH}"
