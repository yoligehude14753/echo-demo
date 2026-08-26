const assert = require("node:assert/strict");
const { existsSync, readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const {
  hasNonEmptyPastSignatures,
} = require("../../scripts/android-signing-rotation-smoke.cjs");
const {
  validateAndroidGradleVersionContract,
} = require("../../scripts/android-gradle-version-contract.cjs");

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
  assert.deepEqual(validateAndroidGradleVersionContract(gradle), []);
  assert.doesNotMatch(gradle, /versionCode\s+\d+/);

  const versionCheck = readDesktop("scripts/check-version-sync.cjs");
  assert.match(versionCheck, /version-codes\.json/);
  assert.doesNotMatch(versionCheck, /minor\s*\*\s*100\s*\+\s*patch/);
});

test("Android Gradle version contract rejects drift in either variant", () => {
  const gradle = readDesktop("android/app/build.gradle");
  const mutations = [
    gradle.replace(
      "previewVersionCode.toInteger()",
      "currentAndroidRelease.versionCode as Integer",
    ),
    gradle.replace(
      "currentAndroidRelease.versionCode as Integer",
      "303",
    ),
    gradle.replace(
      /previewSigningRequested(\r?\n            )\?/,
      "true$1?",
    ),
    gradle.replace(
      "versionName previewSigningRequested",
      "versionName currentAndroidRelease.version.toString()\n        versionName previewSigningRequested",
    ),
  ];

  for (const mutation of mutations) {
    assert.notDeepEqual(validateAndroidGradleVersionContract(mutation), []);
  }
});

test("retired TV one-click installer cannot re-enter package scripts", () => {
  const pkg = JSON.parse(readDesktop("package.json"));
  assert.equal(pkg.scripts["app:package:tv"], undefined);
  assert.equal(
    existsSync(path.join(desktopRoot, "scripts/package-tv-installer.cjs")),
    false,
  );
  assert.equal(existsSync(path.join(repoRoot, "docs/tv-install.html")), false);
});

test("Android upgrade evidence requires a non-empty signing history", () => {
  assert.equal(hasNonEmptyPastSignatures("past signatures:[]"), false);
  assert.equal(hasNonEmptyPastSignatures("past signatures: [   ]"), false);
  assert.equal(
    hasNonEmptyPastSignatures("past signatures: [d9a53185a571243e flags=0]"),
    true,
  );
});

test("retired signed Android workflow stays absent while upgrade logic remains testable", () => {
  assert.equal(
    existsSync(
      path.join(repoRoot, ".github/workflows/build-android-tv-release.yml"),
    ),
    false,
  );
  const upgradeRunner = readDesktop(
    "scripts/run-android-signed-upgrade-smoke.sh",
  );
  assert.match(upgradeRunner, /set -euo pipefail/);
  assert.equal(
    upgradeRunner
      .split(/\r?\n/)
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
});
