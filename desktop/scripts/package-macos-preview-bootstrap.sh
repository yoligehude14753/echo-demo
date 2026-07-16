#!/bin/bash

set -euo pipefail
IFS=$'\n\t'

usage() {
  cat <<'EOF'
Usage:
  package-macos-preview-bootstrap.sh \
    --app /path/to/EchoDesk.app \
    --release-sha 0123456789abcdef0123456789abcdef01234567 \
    --version 0.3.3-preview.2 \
    [--output-dir /path/to/output]

Creates a uniquely named macOS Preview bootstrap ZIP plus its external
manifest and SHA256SUMS file. The ZIP contains the supplied App as
"Payload/EchoDesk Preview.app" and a local ad-hoc signing installer.
EOF
}

fail() {
  printf 'macOS Preview bootstrap packaging failed: %s\n' "$*" >&2
  exit 1
}

app_path=""
release_sha=""
version=""
output_dir="${PWD}"

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --app)
      [[ "$#" -ge 2 ]] || fail "--app requires a value"
      app_path="$2"
      shift 2
      ;;
    --release-sha)
      [[ "$#" -ge 2 ]] || fail "--release-sha requires a value"
      release_sha="$2"
      shift 2
      ;;
    --version)
      [[ "$#" -ge 2 ]] || fail "--version requires a value"
      version="$2"
      shift 2
      ;;
    --output-dir)
      [[ "$#" -ge 2 ]] || fail "--output-dir requires a value"
      output_dir="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[[ -n "${app_path}" ]] || fail "--app is required"
[[ -n "${release_sha}" ]] || fail "--release-sha is required"
[[ -n "${version}" ]] || fail "--version is required"
[[ -d "${app_path}" ]] || fail "App path is not a directory: ${app_path}"
[[ "${app_path}" == *.app ]] || fail "App path must end in .app"
[[ "${release_sha}" =~ ^[0-9a-f]{40}$ ]] ||
  fail "release SHA must be a lowercase 40-character Git SHA"
[[ "${version}" =~ ^[0-9A-Za-z][0-9A-Za-z._+-]*$ ]] ||
  fail "version contains unsafe filename characters"

for tool in /usr/bin/ditto /usr/bin/mktemp /usr/bin/shasum; do
  [[ -x "${tool}" ]] || fail "required tool is unavailable: ${tool}"
done
readonly NODE_BIN="$(command -v node || true)"
[[ -n "${NODE_BIN}" && -x "${NODE_BIN}" ]] || fail "required tool is unavailable: node"

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
readonly INSTALLER_TEMPLATE="${SCRIPT_DIR}/templates/Install EchoDesk Preview.command"
[[ -f "${INSTALLER_TEMPLATE}" ]] ||
  fail "installer template is missing: ${INSTALLER_TEMPLATE}"

/bin/mkdir -p -- "${output_dir}"
readonly OUTPUT_DIR="$(cd -- "${output_dir}" && pwd -P)"
readonly APP_PATH="$(cd -- "$(dirname -- "${app_path}")" && pwd -P)/$(basename -- "${app_path}")"
readonly TIMESTAMP="$(/bin/date -u +%Y%m%dT%H%M%SZ)"
readonly SHORT_SHA="${release_sha:0:12}"
readonly TEMP_DIR="$(/usr/bin/mktemp -d "${OUTPUT_DIR}/.echodesk-preview-bootstrap.XXXXXX")"
readonly TOKEN="$(basename -- "${TEMP_DIR}" | /usr/bin/sed 's/^.*\.//')"
readonly STEM="EchoDesk-${version}-macOS-Preview-bootstrap-${SHORT_SHA}-${TIMESTAMP}-${TOKEN}"
readonly PACKAGE_ROOT="${TEMP_DIR}/${STEM}"
readonly PAYLOAD_DIR="${PACKAGE_ROOT}/Payload"
readonly PAYLOAD_BUNDLE="${PAYLOAD_DIR}/EchoDesk Preview.app"
readonly INTERNAL_MANIFEST="${PACKAGE_ROOT}/manifest.json"
readonly FINAL_ZIP="${OUTPUT_DIR}/${STEM}.zip"
readonly FINAL_MANIFEST="${OUTPUT_DIR}/${STEM}.manifest.json"
readonly FINAL_SUMS="${OUTPUT_DIR}/${STEM}.SHA256SUMS"
readonly TEMP_ZIP="${TEMP_DIR}/${STEM}.zip"

cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  /bin/rm -rf -- "${TEMP_DIR}"
  exit "${status}"
}
trap cleanup EXIT HUP INT TERM

[[ ! -e "${FINAL_ZIP}" && ! -e "${FINAL_MANIFEST}" && ! -e "${FINAL_SUMS}" ]] ||
  fail "unique output collision: ${STEM}"

/bin/mkdir -p -- "${PAYLOAD_DIR}"
/usr/bin/ditto -- "${APP_PATH}" "${PAYLOAD_BUNDLE}"
/bin/cp -- "${INSTALLER_TEMPLATE}" "${PACKAGE_ROOT}/Install EchoDesk Preview.command"
/bin/chmod 0755 "${PACKAGE_ROOT}/Install EchoDesk Preview.command"

ECHODESK_RELEASE_SHA="${release_sha}" \
ECHODESK_PACKAGE_VERSION="${version}" \
ECHODESK_PAYLOAD_BUNDLE="${PAYLOAD_BUNDLE}" \
ECHODESK_INSTALLER_PATH="${PACKAGE_ROOT}/Install EchoDesk Preview.command" \
ECHODESK_MANIFEST_PATH="${INTERNAL_MANIFEST}" \
"${NODE_BIN}" <<'NODE'
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const payloadBundle = process.env.ECHODESK_PAYLOAD_BUNDLE;
const installerPath = process.env.ECHODESK_INSTALLER_PATH;
const manifestPath = process.env.ECHODESK_MANIFEST_PATH;

function sha256(contents) {
  return crypto.createHash("sha256").update(contents).digest("hex");
}

function visit(root, relative = "") {
  const absolute = path.join(root, relative);
  const stat = fs.lstatSync(absolute);
  const normalized = relative.split(path.sep).join("/");
  const base = {
    path: normalized || ".",
    mode: (stat.mode & 0o7777).toString(8).padStart(4, "0"),
  };

  if (stat.isSymbolicLink()) {
    return [{ ...base, type: "symlink", target: fs.readlinkSync(absolute) }];
  }
  if (stat.isFile()) {
    const contents = fs.readFileSync(absolute);
    return [{
      ...base,
      type: "file",
      size: stat.size,
      sha256: sha256(contents),
    }];
  }
  if (!stat.isDirectory()) {
    throw new Error(`unsupported payload entry type: ${normalized}`);
  }

  const entries = [{ ...base, type: "directory" }];
  for (const name of fs.readdirSync(absolute).sort()) {
    entries.push(...visit(root, path.join(relative, name)));
  }
  return entries;
}

const entries = visit(payloadBundle);
const treeDigest = sha256(`${entries.map((entry) => JSON.stringify(entry)).join("\n")}\n`);
const manifest = {
  schema: "com.echodesk.macos-preview-bootstrap.v1",
  release_sha: process.env.ECHODESK_RELEASE_SHA,
  version: process.env.ECHODESK_PACKAGE_VERSION,
  bundle_path: "Payload/EchoDesk Preview.app",
  payload_tree_sha256: treeDigest,
  installer: {
    path: "Install EchoDesk Preview.command",
    sha256: sha256(fs.readFileSync(installerPath)),
  },
  payload_entries: entries,
};

fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, {
  encoding: "utf8",
  mode: 0o644,
});
NODE

/usr/bin/ditto -c -k --sequesterRsrc --keepParent "${PACKAGE_ROOT}" "${TEMP_ZIP}"
/bin/cp -- "${INTERNAL_MANIFEST}" "${FINAL_MANIFEST}"
/bin/mv -- "${TEMP_ZIP}" "${FINAL_ZIP}"

(
  cd -- "${OUTPUT_DIR}"
  /usr/bin/shasum -a 256 "$(basename -- "${FINAL_ZIP}")" "$(basename -- "${FINAL_MANIFEST}")"
) >"${FINAL_SUMS}"

printf '%s\n' \
  "ZIP=${FINAL_ZIP}" \
  "MANIFEST=${FINAL_MANIFEST}" \
  "SHA256SUMS=${FINAL_SUMS}" \
  "INSTALLER=Install EchoDesk Preview.command (inside ZIP)"
