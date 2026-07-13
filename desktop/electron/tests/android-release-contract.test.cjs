const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const {
  hasNonEmptyPastSignatures,
} = require("../../scripts/android-signing-rotation-smoke.cjs");

const desktopRoot = path.resolve(__dirname, "..", "..");
const repoRoot = path.resolve(desktopRoot, "..");

function readDesktop(relativePath) {
  return readFileSync(path.join(desktopRoot, relativePath), "utf8");
}

test("Android version codes come from an append-only monotonic ledger", () => {
  const pkg = JSON.parse(readDesktop("package.json"));
  const ledger = JSON.parse(readDesktop("android/version-codes.json"));
  assert.equal(ledger.schemaVersion, 1);
  assert.ok(Array.isArray(ledger.releases));
  assert.ok(ledger.releases.length >= 2);
  assert.deepEqual(ledger.releases[0], {
    version: "0.2.34",
    versionCode: 234,
    status: "historical-release",
  });
  const versions = new Set();
  let previousCode = 0;
  for (const release of ledger.releases) {
    assert.match(release.version, /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/);
    assert.ok(Number.isSafeInteger(release.versionCode));
    assert.ok(release.versionCode > previousCode);
    assert.equal(versions.has(release.version), false);
    versions.add(release.version);
    previousCode = release.versionCode;
  }
  assert.equal(ledger.releases.at(-1).version, pkg.version);

  const gradle = readDesktop("android/app/build.gradle");
  assert.match(
    gradle,
    /versionCode currentAndroidRelease\.versionCode as Integer/,
  );
  assert.match(
    gradle,
    /versionName currentAndroidRelease\.version\.toString\(\)/,
  );
  assert.doesNotMatch(gradle, /versionCode\s+\d+/);

  const versionCheck = readDesktop("scripts/check-version-sync.cjs");
  assert.match(versionCheck, /version-codes\.json/);
  assert.doesNotMatch(versionCheck, /minor\s*\*\s*100\s*\+\s*patch/);
});

test("TV installer preserves the user's microphone permission decision", () => {
  const installer = readDesktop("scripts/package-tv-installer.cjs");
  assert.doesNotMatch(installer, /pm grant[^\n]*RECORD_AUDIO/);
  assert.doesNotMatch(installer, /appops set[^\n]*RECORD_AUDIO\s+allow/);
  assert.doesNotMatch(
    installer,
    /ECHODESK_TV_RESET_DATA|ECHODESK_TV_KEEP_DATA/,
  );
  assert.doesNotMatch(installer, /pm clear ["$]*\$?PKG/);
  assert.doesNotMatch(installer, /pm clear \$Pkg/);
  assert.doesNotMatch(installer, /install\s+-r\s+-d/);
  assert.match(installer, /安装器不会更改麦克风授权/);
  assert.match(installer, /不会允许版本降级/);
});

test("Android upgrade evidence requires a non-empty signing history", () => {
  assert.equal(hasNonEmptyPastSignatures("past signatures:[]"), false);
  assert.equal(hasNonEmptyPastSignatures("past signatures: [   ]"), false);
  assert.equal(
    hasNonEmptyPastSignatures("past signatures: [d9a53185a571243e flags=0]"),
    true,
  );
});

test("signed Android workflow upgrades real pinned historical APKs to candidates", () => {
  const workflow = readFileSync(
    path.join(repoRoot, ".github/workflows/build-android-tv-release.yml"),
    "utf8",
  );
  const upgradeRunner = readDesktop(
    "scripts/run-android-signed-upgrade-smoke.sh",
  );
  assert.match(workflow, /EchoDesk-0\.2\.34-android\.apk/);
  assert.match(workflow, /EchoDesk-0\.2\.34-android-tv\.apk/);
  assert.match(
    workflow,
    /d7533401cb0e0a1dd8cad0d0feab2d8fe4f18dc813df455544eb3f26fb86f6c2/,
  );
  assert.match(
    workflow,
    /be8b0c08004a13dc0e347c0d6edd14653e5c3864db4f78948efb7d80572a6653/,
  );
  assert.match(
    workflow,
    /reactivecircus\/android-emulator-runner@[0-9a-f]{40}/,
  );
  assert.match(
    workflow,
    /script: bash desktop\/scripts\/run-android-signed-upgrade-smoke\.sh/,
  );
  assert.doesNotMatch(workflow, /bash -euo pipefail <<'BASH'/);
  assert.match(upgradeRunner, /set -euo pipefail/);
  assert.equal(
    upgradeRunner
      .replace(/\r\n/g, "\n").split("\n")
      .filter((line) => line === "run_upgrade_smoke \\").length,
    2,
  );
  assert.equal(
    [...upgradeRunner.matchAll(/android-candidate-upgrade-smoke\.cjs/g)]
      .length,
    1,
  );
  assert.match(upgradeRunner, /com\.echodesk\.app/);
  assert.match(upgradeRunner, /com\.echodesk\.tv/);
  assert.match(upgradeRunner, /android-upgrade-smoke\.json/);
  assert.match(upgradeRunner, /android-tv-upgrade-smoke\.json/);
  assert.match(workflow, /ECHODESK_VERSION=%s/);
  assert.doesNotMatch(
    workflow,
    /(?:KEYSTORE|KEY_PASSWORD|CERT_SHA256|KEY_ALIAS)[^\n]*GITHUB_ENV/,
  );
  assert.match(workflow, /trap cleanup EXIT/);
  assert.doesNotMatch(workflow, /stable signed release/);

  const smoke = readDesktop("scripts/android-candidate-upgrade-smoke.cjs");
  const releaseBuilder = readDesktop("scripts/build-android-release.cjs");
  assert.match(smoke, /EXPECTED_HISTORICAL_SHA256/);
  assert.match(smoke, /candidate versionCode must be an integer above/);
  assert.match(smoke, /\["install", "-r", candidateApk\]/);
  assert.match(smoke, /after\.uid !== before\.uid/);
  assert.match(smoke, /after\.firstInstallTime !== before\.firstInstallTime/);
  assert.match(smoke, /after\.hasPastSignatures/);
  assert.match(smoke, /candidate did not launch successfully/);
  assert.doesNotMatch(smoke, /createSigningLineage|newOnlyApk|rotatedApk/);
  assert.doesNotMatch(
    smoke,
    /releaseSigningContract|verifySigningIdentities|KEYSTORE_PASSWORD|KEY_PASSWORD/,
  );
  assert.match(releaseBuilder, /"--no-daemon",\s*"clean",\s*"assembleRelease"/);
  assert.match(workflow, /gradlew --stop/);
  assert.match(workflow, /org\.gradle\.daemon=false/);
});
