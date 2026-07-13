#!/usr/bin/env bash

set -euo pipefail

repo_root="${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
runner_temp="${RUNNER_TEMP:?RUNNER_TEMP is required for the pinned historical APKs}"
cd "$repo_root"

version="$(node -p "require('./desktop/package.json').version")"
version_code="$(node -p "require('./desktop/android/version-codes.json').releases.at(-1).versionCode")"
signing_manifest="$repo_root/desktop/release/EchoDesk-${version}-android-signing.json"
history="$runner_temp/echodesk-history-v0.2.34"

test -f "$signing_manifest"
test -d "$history"

legacy_cert="$(node -p "require(process.argv[1]).lineage.legacyCertificateSha256" "$signing_manifest")"
current_cert="$(node -p "require(process.argv[1]).lineage.currentCertificateSha256" "$signing_manifest")"
rotation_min_sdk="$(node -p "require(process.argv[1]).rotationMinSdkVersion" "$signing_manifest")"

run_upgrade_smoke() {
  local historical_apk="$1"
  local candidate_apk="$2"
  local application_id="$3"
  local historical_sha256="$4"
  local evidence_path="$5"

  ECHODESK_ANDROID_HISTORICAL_APK="$historical_apk" \
  ECHODESK_ANDROID_CANDIDATE_APK="$candidate_apk" \
  ECHODESK_ANDROID_EXPECTED_APPLICATION_ID="$application_id" \
  ECHODESK_ANDROID_EXPECTED_HISTORICAL_SHA256="$historical_sha256" \
  ECHODESK_ANDROID_EXPECTED_HISTORICAL_VERSION="0.2.34" \
  ECHODESK_ANDROID_EXPECTED_HISTORICAL_VERSION_CODE="234" \
  ECHODESK_ANDROID_EXPECTED_CANDIDATE_VERSION="$version" \
  ECHODESK_ANDROID_EXPECTED_CANDIDATE_VERSION_CODE="$version_code" \
  ECHODESK_ANDROID_EXPECTED_LEGACY_CERT_SHA256="$legacy_cert" \
  ECHODESK_ANDROID_EXPECTED_CURRENT_CERT_SHA256="$current_cert" \
  ECHODESK_ANDROID_ROTATION_MIN_SDK_VERSION="$rotation_min_sdk" \
    node desktop/scripts/android-candidate-upgrade-smoke.cjs \
    | tee "$evidence_path"
}

run_upgrade_smoke \
  "$history/EchoDesk-0.2.34-android.apk" \
  "$repo_root/desktop/release/EchoDesk-${version}-android.apk" \
  "com.echodesk.app" \
  "d7533401cb0e0a1dd8cad0d0feab2d8fe4f18dc813df455544eb3f26fb86f6c2" \
  "$repo_root/desktop/release/EchoDesk-${version}-android-upgrade-smoke.json"

run_upgrade_smoke \
  "$history/EchoDesk-0.2.34-android-tv.apk" \
  "$repo_root/desktop/release/EchoDesk-${version}-android-tv.apk" \
  "com.echodesk.tv" \
  "be8b0c08004a13dc0e347c0d6edd14653e5c3864db4f78948efb7d80572a6653" \
  "$repo_root/desktop/release/EchoDesk-${version}-android-tv-upgrade-smoke.json"
