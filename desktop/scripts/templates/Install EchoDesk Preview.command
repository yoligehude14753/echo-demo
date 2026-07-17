#!/bin/bash

set -euo pipefail
IFS=$'\n\t'

readonly BUNDLE_NAME="EchoDesk Preview.app"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
readonly SOURCE_BUNDLE="${SCRIPT_DIR}/Payload/${BUNDLE_NAME}"
readonly TARGET_DIR="${ECHODESK_PREVIEW_INSTALL_DIR:-/Applications}"
readonly TARGET_BUNDLE="${TARGET_DIR}/${BUNDLE_NAME}"

fail() {
  printf 'EchoDesk Preview installation failed: %s\n' "$*" >&2
  exit 1
}

[[ "${TARGET_DIR}" == /* ]] || fail "install directory must be an absolute path"
[[ -d "${SOURCE_BUNDLE}" ]] || fail "packaged app is missing: ${SOURCE_BUNDLE}"
[[ ! -L "${TARGET_DIR}" ]] || fail "install directory must not be a symbolic link"

/bin/mkdir -p -- "${TARGET_DIR}" ||
  fail "cannot create install directory: ${TARGET_DIR}"
[[ -w "${TARGET_DIR}" ]] ||
  fail "install directory is not writable: ${TARGET_DIR}"

readonly TRANSACTION_DIR="$(/usr/bin/mktemp -d "${TARGET_DIR}/.echodesk-preview-install.XXXXXX")"
readonly STAGED_BUNDLE="${TRANSACTION_DIR}/${BUNDLE_NAME}"
readonly BACKUP_BUNDLE="${TRANSACTION_DIR}/previous.app"

previous_moved=0
new_installed=0

rollback() {
  local status=$?
  trap - EXIT HUP INT TERM

  if [[ "${status}" -ne 0 ]]; then
    if [[ "${new_installed}" -eq 1 && -e "${TARGET_BUNDLE}" ]]; then
      /bin/mv -- "${TARGET_BUNDLE}" "${TRANSACTION_DIR}/failed.app" 2>/dev/null ||
        /bin/rm -rf -- "${TARGET_BUNDLE}"
    fi
    if [[ "${previous_moved}" -eq 1 && -e "${BACKUP_BUNDLE}" ]]; then
      /bin/mv -- "${BACKUP_BUNDLE}" "${TARGET_BUNDLE}" 2>/dev/null || true
    fi
  fi

  /bin/rm -rf -- "${TRANSACTION_DIR}"
  exit "${status}"
}
trap rollback EXIT HUP INT TERM

/usr/bin/ditto -- "${SOURCE_BUNDLE}" "${STAGED_BUNDLE}"

# Only the transaction-local target bundle is de-quarantined. No global
# Gatekeeper preference or source-package attribute is changed.
if ! /usr/bin/xattr -r -d com.apple.quarantine "${STAGED_BUNDLE}" 2>/dev/null; then
  if /usr/bin/xattr -r "${STAGED_BUNDLE}" 2>/dev/null |
    /usr/bin/grep -Fq 'com.apple.quarantine'; then
    fail "could not clear quarantine from the staged target bundle"
  fi
fi

/usr/bin/codesign --force --deep --sign - "${STAGED_BUNDLE}"
/usr/bin/codesign --verify --deep --strict --verbose=2 "${STAGED_BUNDLE}"

if [[ -L "${TARGET_BUNDLE}" ]]; then
  fail "refusing to replace a symbolic-link target: ${TARGET_BUNDLE}"
fi
if [[ -e "${TARGET_BUNDLE}" ]]; then
  /bin/mv -- "${TARGET_BUNDLE}" "${BACKUP_BUNDLE}"
  previous_moved=1
fi

/bin/mv -- "${STAGED_BUNDLE}" "${TARGET_BUNDLE}"
new_installed=1

/usr/bin/codesign --verify --deep --strict --verbose=2 "${TARGET_BUNDLE}"

if [[ "${previous_moved}" -eq 1 ]]; then
  /bin/rm -rf -- "${BACKUP_BUNDLE}"
  previous_moved=0
fi
new_installed=0

printf 'EchoDesk Preview installed at %s\n' "${TARGET_BUNDLE}"
