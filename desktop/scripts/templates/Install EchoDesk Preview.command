#!/bin/bash

set -euo pipefail
IFS=$'\n\t'

readonly BUNDLE_NAME="EchoDesk Preview.app"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PACKAGE_DIR="${ECHODESK_PREVIEW_PACKAGE_DIR:-${SCRIPT_DIR}}"

if [[ "${1:-}" == "--package-dir" ]]; then
  [[ "$#" -eq 2 ]] || {
    printf 'Usage: %s [--package-dir /path/to/extracted-package]\n' "$0" >&2
    exit 2
  }
  PACKAGE_DIR="$2"
elif [[ "$#" -ne 0 ]]; then
  printf 'Usage: %s [--package-dir /path/to/extracted-package]\n' "$0" >&2
  exit 2
fi

fail() {
  printf 'EchoDesk Preview installation failed: %s\n' "$*" >&2
  exit 1
}

[[ "${PACKAGE_DIR}" == /* ]] || fail "package directory must be an absolute path"
PACKAGE_DIR="$(cd -- "${PACKAGE_DIR}" && pwd -P)"
readonly PACKAGE_DIR
readonly SOURCE_BUNDLE="${PACKAGE_DIR}/Payload/${BUNDLE_NAME}"
readonly MANIFEST_PATH="${PACKAGE_DIR}/manifest.json"
readonly MANIFEST_SUMS="${PACKAGE_DIR}/manifest.sha256"
readonly PAYLOAD_SUMS="${PACKAGE_DIR}/payload.sha256"
readonly TARGET_DIR="${ECHODESK_PREVIEW_INSTALL_DIR:-/Applications}"
readonly TARGET_BUNDLE="${TARGET_DIR}/${BUNDLE_NAME}"
readonly WORKFLOW_PORT="${ECHODESK_PREVIEW_WORKFLOW_PORT:-18769}"

[[ -d "${SOURCE_BUNDLE}" ]] || fail "packaged app is missing: ${SOURCE_BUNDLE}"
[[ -f "${MANIFEST_PATH}" ]] || fail "manifest is missing: ${MANIFEST_PATH}"
[[ -f "${MANIFEST_SUMS}" ]] || fail "manifest checksum is missing: ${MANIFEST_SUMS}"
[[ -f "${PAYLOAD_SUMS}" ]] || fail "payload checksum is missing: ${PAYLOAD_SUMS}"
[[ "${TARGET_DIR}" == /* ]] || fail "install directory must be an absolute path"
[[ ! -L "${TARGET_DIR}" ]] || fail "install directory must not be a symbolic link"
[[ "${WORKFLOW_PORT}" =~ ^[0-9]+$ ]] || fail "workflow port must be numeric"

for tool in /usr/bin/codesign /usr/bin/curl /usr/bin/grep /usr/bin/mktemp \
  /usr/bin/plutil /usr/bin/shasum /bin/kill /bin/mv /usr/bin/ditto; do
  [[ -x "${tool}" ]] || fail "required tool is unavailable: ${tool}"
done

verify_package() {
  local release_sha bundle_path checksum_path
  (
    cd -- "${PACKAGE_DIR}"
    /usr/bin/shasum -a 256 --check --strict "$(basename -- "${MANIFEST_SUMS}")"
    /usr/bin/shasum -a 256 --check --strict "$(basename -- "${PAYLOAD_SUMS}")"
  ) || fail "manifest or payload SHA-256 verification failed"

  release_sha="$(/usr/bin/plutil -extract release_sha raw -o - "${MANIFEST_PATH}")" ||
    fail "manifest release_sha is unreadable"
  [[ "${release_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "manifest release_sha is invalid"
  bundle_path="$(/usr/bin/plutil -extract bundle_path raw -o - "${MANIFEST_PATH}")" ||
    fail "manifest bundle_path is unreadable"
  [[ "${bundle_path}" == "Payload/${BUNDLE_NAME}" ]] ||
    fail "manifest bundle_path is not the expected payload"
  checksum_path="$(/usr/bin/plutil -extract payload_checksums raw -o - "${MANIFEST_PATH}")" ||
    fail "manifest payload checksum reference is unreadable"
  [[ "${checksum_path}" == "payload.sha256" ]] ||
    fail "manifest payload checksum reference is invalid"
}

confirm_install() {
  if [[ "${ECHODESK_CONFIRM_INSTALL:-}" == "INSTALL" ]]; then
    return
  fi
  [[ -t 0 ]] || fail "one explicit confirmation is required; rerun interactively and type INSTALL"
  printf 'This will install %s into %s and start a local fused workflow.\n' \
    "${BUNDLE_NAME}" "${TARGET_DIR}"
  printf 'Type INSTALL to continue: '
  local answer
  IFS= read -r answer || fail "confirmation input was interrupted"
  [[ "${answer}" == "INSTALL" ]] || fail "explicit confirmation did not match INSTALL"
}

TRANSACTION_DIR=""
STAGED_BUNDLE=""
BACKUP_BUNDLE=""
WORKFLOW_LOG=""
previous_moved=0
new_installed=0
workflow_pid=""

stop_workflow() {
  if [[ -n "${workflow_pid}" ]] && /bin/kill -0 "${workflow_pid}" 2>/dev/null; then
    /bin/kill -TERM "${workflow_pid}" 2>/dev/null || true
  fi
  workflow_pid=""
}

rollback() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [[ "${status}" -ne 0 ]]; then
    stop_workflow
    if [[ "${new_installed}" -eq 1 && -e "${TARGET_BUNDLE}" ]]; then
      /bin/mv -- "${TARGET_BUNDLE}" "${TRANSACTION_DIR}/failed.app" 2>/dev/null ||
        /bin/rm -rf -- "${TARGET_BUNDLE}"
    fi
    if [[ "${previous_moved}" -eq 1 && -e "${BACKUP_BUNDLE}" ]]; then
      /bin/mv -- "${BACKUP_BUNDLE}" "${TARGET_BUNDLE}" 2>/dev/null || true
    fi
  fi
  if [[ -n "${TRANSACTION_DIR}" ]]; then
    /bin/rm -rf -- "${TRANSACTION_DIR}"
  fi
  exit "${status}"
}
trap rollback EXIT HUP INT TERM

verify_package
confirm_install

/bin/mkdir -p -- "${TARGET_DIR}" || fail "cannot create install directory: ${TARGET_DIR}"
[[ -w "${TARGET_DIR}" ]] || fail "install directory is not writable: ${TARGET_DIR}"

TRANSACTION_DIR="$(/usr/bin/mktemp -d "${TARGET_DIR}/.echodesk-preview-install.XXXXXX")"
STAGED_BUNDLE="${TRANSACTION_DIR}/${BUNDLE_NAME}"
BACKUP_BUNDLE="${TRANSACTION_DIR}/previous.app"
WORKFLOW_LOG="${TRANSACTION_DIR}/workflow.log"
readonly TRANSACTION_DIR STAGED_BUNDLE BACKUP_BUNDLE WORKFLOW_LOG

/usr/bin/ditto -- "${SOURCE_BUNDLE}" "${STAGED_BUNDLE}"

# Only the transaction-local target bundle is de-quarantined. No global
# Gatekeeper preference or unrelated application attribute is changed.
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

# Start the installed executable directly so the diagnostic/local environment
# is explicit and the packaged fused-worker connection can be observed. This
# is a loopback-only smoke; it never downloads or executes a remote script.
ECHO_RUNTIME_MODE=diagnostic \
ECHO_PRINCIPAL_MODE=local \
ECHO_BACKEND_PORT="${WORKFLOW_PORT}" \
ECHO_BACKEND_BIND_HOST=127.0.0.1 \
ECHODESK_DISABLE_AUTO_UPDATE_DOWNLOAD=1 \
WORKSPACE_SCAN_ON_STARTUP=false \
DIARIZER_ENABLED=false \
TTS_ENABLED=false \
AGENT_OS_ENABLED=false \
"${TARGET_BUNDLE}/Contents/MacOS/EchoDesk" --smoke-exit-on-window-close \
  >"${WORKFLOW_LOG}" 2>&1 &
workflow_pid=$!

workflow_deadline=$((SECONDS + 60))
workflow_ok=0
while (( SECONDS < workflow_deadline )); do
  if ! /bin/kill -0 "${workflow_pid}" 2>/dev/null; then
    break
  fi
  if /usr/bin/curl --fail --silent --show-error --max-time 2 \
      "http://127.0.0.1:${WORKFLOW_PORT}/healthz/full" >"${TRANSACTION_DIR}/health.json" 2>/dev/null &&
    /usr/bin/curl --fail --silent --show-error --max-time 2 \
      "http://127.0.0.1:${WORKFLOW_PORT}/bootstrap" >"${TRANSACTION_DIR}/bootstrap.json" 2>/dev/null &&
    /usr/bin/grep -Fq '"workflow_kernel":"dispatcher-v1"' "${TRANSACTION_DIR}/bootstrap.json" &&
    /usr/bin/grep -Fq '[runtime] packaged fused worker bridge connected' "${WORKFLOW_LOG}"; then
    workflow_ok=1
    break
  fi
  /bin/sleep 0.25
done
if [[ "${workflow_ok}" -ne 1 ]]; then
  /usr/bin/grep -E 'backend|fused|runtime|error|Error' "${WORKFLOW_LOG}" >&2 || true
  fail "minimal fused workflow did not start and pass loopback readback"
fi

# The new App is committed only after signature, post-install signature, and
# fused workflow readback all pass. Until this point rollback restores the old
# bundle; only now is the backup removed.
stop_workflow
if [[ "${previous_moved}" -eq 1 ]]; then
  /bin/rm -rf -- "${BACKUP_BUNDLE}"
  previous_moved=0
fi
new_installed=0

printf 'EchoDesk Preview installed and fused workflow verified at %s\n' "${TARGET_BUNDLE}"
