const assert = require("node:assert/strict");
const {
  chmodSync,
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");
const yaml = require("js-yaml");
const { DebugLogger } = require("builder-util/out/DebugLogger");
const {
  validateConfiguration,
} = require("app-builder-lib/out/util/config/config");

const {
  normalizeFingerprint,
  releaseSigningContract,
} = require("../../scripts/android-build-common.cjs");
const {
  installAndroidGradleLocks,
} = require("../../scripts/prepare-android-gradle-locks.cjs");

const desktopRoot = path.resolve(__dirname, "../..");
const repoRoot = path.resolve(desktopRoot, "..");

function readSingleGradleBlock(source, blockName) {
  const matches = [
    ...source.matchAll(new RegExp(`\\b${blockName}\\s*\\{`, "g")),
  ];
  assert.equal(matches.length, 1, `expected one ${blockName} block`);

  const openBrace = source.indexOf("{", matches[0].index);
  let depth = 0;
  for (let index = openBrace; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(openBrace + 1, index);
  }
  assert.fail(`${blockName} block is not closed`);
}

test("electron-builder accepts the committed package configuration", async () => {
  const pkg = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  );
  assert.equal(pkg.desktopName, "com.echodesk.app.desktop");
  assert.equal(pkg.build.linux.syncDesktopName, true);
  await validateConfiguration(pkg.build, new DebugLogger(false));
});

test("public Android signing requires a legacy-to-current API 33 rotation contract", () => {
  const root = mkdtempSync(
    path.join(os.tmpdir(), "echodesk-signing-contract-"),
  );
  const legacy = path.join(root, "debug.keystore");
  const current = path.join(root, "stable-release.jks");
  writeFileSync(legacy, "legacy");
  writeFileSync(current, "current");
  const legacyFingerprint = "ab".repeat(32);
  const currentFingerprint = "cd".repeat(32);
  const valid = {
    ECHODESK_ANDROID_LEGACY_KEYSTORE: legacy,
    ECHODESK_ANDROID_LEGACY_KEY_ALIAS: "androiddebugkey",
    ECHODESK_ANDROID_LEGACY_KEYSTORE_PASSWORD: "secret",
    ECHODESK_ANDROID_LEGACY_KEY_PASSWORD: "secret",
    ECHODESK_ANDROID_EXPECTED_LEGACY_CERT_SHA256: legacyFingerprint,
    ECHODESK_ANDROID_CURRENT_KEYSTORE: current,
    ECHODESK_ANDROID_CURRENT_KEY_ALIAS: "echodesk-stable",
    ECHODESK_ANDROID_CURRENT_KEYSTORE_PASSWORD: "secret",
    ECHODESK_ANDROID_CURRENT_KEY_PASSWORD: "secret",
    ECHODESK_ANDROID_EXPECTED_CURRENT_CERT_SHA256: currentFingerprint,
    ECHODESK_ANDROID_ROTATION_MIN_SDK_VERSION: "33",
  };
  try {
    assert.throws(
      () => releaseSigningContract({}),
      /legacy\/current signing inputs/,
    );
    const contract = releaseSigningContract(valid);
    assert.equal(contract.rotationMinSdkVersion, 33);
    assert.equal(contract.legacy.expectedFingerprint, legacyFingerprint);
    assert.equal(contract.current.expectedFingerprint, currentFingerprint);
    assert.throws(
      () =>
        releaseSigningContract({
          ...valid,
          ECHODESK_ANDROID_CURRENT_KEYSTORE: legacy,
        }),
      /current public release signer must not be a debug identity/,
    );
    assert.throws(
      () =>
        releaseSigningContract({
          ...valid,
          ECHODESK_ANDROID_CURRENT_KEY_ALIAS: "androiddebugkey",
        }),
      /current public release signer must not be a debug identity/,
    );
    assert.throws(
      () =>
        releaseSigningContract({
          ...valid,
          ECHODESK_ANDROID_EXPECTED_CURRENT_CERT_SHA256: legacyFingerprint,
        }),
      /fingerprints must differ/,
    );
    assert.throws(
      () =>
        releaseSigningContract({
          ...valid,
          ECHODESK_ANDROID_ROTATION_MIN_SDK_VERSION: "28",
        }),
      /rotation min SDK 33/,
    );
    assert.equal(
      normalizeFingerprint(currentFingerprint.toUpperCase()),
      currentFingerprint,
    );
    assert.equal(releaseSigningContract(valid).legacy.keystore, legacy);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("Android Gradle and package scripts separate development from public release", () => {
  const gradle = readFileSync(
    path.join(desktopRoot, "android/app/build.gradle"),
    "utf8",
  );
  const rootGradle = readFileSync(
    path.join(desktopRoot, "android/build.gradle"),
    "utf8",
  );
  const pkg = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  );
  const debugScript = readFileSync(
    path.join(desktopRoot, "scripts/build-android-debug.cjs"),
    "utf8",
  );
  const releaseScript = readFileSync(
    path.join(desktopRoot, "scripts/build-android-release.cjs"),
    "utf8",
  );
  const prepareLocksScript = readFileSync(
    path.join(desktopRoot, "scripts/prepare-android-gradle-locks.cjs"),
    "utf8",
  );
  const rotationSmoke = readFileSync(
    path.join(desktopRoot, "scripts/android-signing-rotation-smoke.cjs"),
    "utf8",
  );

  assert.match(gradle, /releaseTaskRequested/);
  assert.match(gradle, /echoExternalSigning/);
  assert.match(gradle, /controlled v3\.1 external signing pipeline/);
  assert.match(gradle, /ECHODESK_ANDROID_EXPECTED_LEGACY_CERT_SHA256/);
  assert.match(gradle, /ECHODESK_ANDROID_EXPECTED_CURRENT_CERT_SHA256/);
  assert.match(gradle, /ROTATION_MIN_SDK_VERSION"\) != "33/);
  const signingConfigs = readSingleGradleBlock(gradle, "signingConfigs");
  assert.match(
    signingConfigs,
    /if \(previewSigningRequested\)\s*\{\s*preview\s*\{/,
  );
  for (const variable of [
    "ECHODESK_ANDROID_PREVIEW_KEYSTORE",
    "ECHODESK_ANDROID_PREVIEW_KEYSTORE_PASSWORD",
    "ECHODESK_ANDROID_PREVIEW_KEY_ALIAS",
    "ECHODESK_ANDROID_PREVIEW_KEY_PASSWORD",
  ]) {
    assert.match(signingConfigs, new RegExp(`System\\.getenv\\("${variable}"\\)`));
  }
  assert.equal(
    (signingConfigs.match(/System\.getenv\("ECHODESK_ANDROID_PREVIEW_/g) || [])
      .length,
    4,
  );
  assert.doesNotMatch(
    signingConfigs,
    /storeFile\s+file\(\s*["']|(?:storePassword|keyAlias|keyPassword)\s+["']/,
  );
  assert.doesNotMatch(
    signingConfigs,
    /ECHODESK_ANDROID_(?:LEGACY|CURRENT)_|signingConfigs\.(?:debug|development)/,
  );

  const buildTypes = readSingleGradleBlock(gradle, "buildTypes");
  assert.match(
    buildTypes,
    /if \(previewSigningRequested\)\s*\{\s*signingConfig signingConfigs\.preview\s*\}/,
  );
  assert.equal((buildTypes.match(/\bsigningConfig\b/g) || []).length, 1);
  assert.doesNotMatch(buildTypes, /signingConfigs\.(?:debug|development)/);
  assert.match(
    gradle,
    /if \(releaseTaskRequested && !previewSigningRequested\) \{[\s\S]*if \(!externalSigningRequested\)/,
  );
  assert.match(rootGradle, /kotlin-stdlib/);
  assert.match(rootGradle, /details\.useVersion '1\.8\.22'/);
  assert.match(rootGradle, /dependencyLocking/);
  assert.match(rootGradle, /lockAllConfigurations/);
  const wrapper = readFileSync(
    path.join(desktopRoot, "android/gradle/wrapper/gradle-wrapper.properties"),
    "utf8",
  );
  assert.match(wrapper, /gradle-8\.14\.5-all\.zip/);
  assert.match(
    wrapper,
    /distributionSha256Sum=62c3769155d7d17ea05084ad498067824c1804568a408a6faa78a5ef95ed67a8/,
  );
  for (const relative of [
    "android/app/gradle.lockfile",
    "android/gradle/locks/capacitor-android-8.4.0.lockfile",
    "android/gradle/locks/capacitor-cordova-android-plugins.lockfile",
    "android/gradle/verification-metadata.xml",
  ]) {
    assert.equal(existsSync(path.join(desktopRoot, relative)), true, relative);
  }
  const verificationMetadata = readFileSync(
    path.join(desktopRoot, "android/gradle/verification-metadata.xml"),
    "utf8",
  );
  assert.match(
    verificationMetadata,
    /guava-parent-33\.3\.1-jre\.pom[\s\S]*55441db27e8869dfefe053059bdf478bdc7e95585642bf391f0023345fd56287/,
  );
  assert.match(
    verificationMetadata,
    /junit-bom-5\.10\.2\.module[\s\S]*de23b114b3e4119a8fe6eb17bed5a3852816698bace67071579d6d927ebb080a/,
  );
  assert.match(
    verificationMetadata,
    /junit-bom-5\.9\.2\.module[\s\S]*ab137ba5a8e32c9b066bf9126a1c76dd5614b724ba5c0b02549772b5e9f4cf1f/,
  );
  assert.match(
    verificationMetadata,
    /aapt2-8\.13\.0-13719691-linux\.jar[\s\S]*c1aebd96a144313da65de675cc1f59041b41a52e844228d311bb580ed830b0d9/,
  );
  assert.equal(
    pkg.scripts["app:build:android:development"],
    "node scripts/build-android-debug.cjs",
  );
  assert.equal(
    pkg.scripts["app:dist:android"],
    "node scripts/build-android-release.cjs",
  );
  assert.equal(
    pkg.scripts["smoke:android:rotation"],
    "node scripts/android-signing-rotation-smoke.cjs",
  );
  assert.match(pkg.scripts.postinstall, /prepare-android-gradle-locks\.cjs/);
  assert.match(pkg.scripts["android:sync"], /prepare-android-gradle-locks\.cjs$/);
  assert.match(
    prepareLocksScript,
    /capacitor-cordova-android-plugins\.lockfile/,
  );
  for (const script of [debugScript, releaseScript]) {
    const syncIndex = script.indexOf('"cap", "sync", "android"');
    const lockIndex = script.indexOf("prepare-android-gradle-locks.cjs");
    assert.ok(syncIndex >= 0, "Android build must run cap sync");
    assert.ok(
      lockIndex > syncIndex,
      "Android build must install canonical locks after cap sync",
    );
  }
  assert.match(debugScript, /android-development\.apk/);
  assert.match(debugScript, /DEVELOPMENT ONLY/);
  assert.doesNotMatch(debugScript, /android-tv\.apk`/);
  assert.match(releaseScript, /releaseSigningContract\(env\)/);
  assert.match(releaseScript, /verifyReleaseApk/);
  assert.match(releaseScript, /app-release-unsigned\.apk/);
  assert.match(releaseScript, /"rotate"/);
  assert.match(releaseScript, /"--old-signer"/);
  assert.match(releaseScript, /"--next-signer"/);
  assert.match(releaseScript, /"--rotation-min-sdk-version"/);
  assert.match(releaseScript, /"--v4-signing-enabled",\s+"false"/);
  assert.match(releaseScript, /Verified using v3\.1 scheme/);
  assert.doesNotMatch(releaseScript, /pass:[^`"\s]+/);
  assert.match(rotationSmoke, /INSTALL_FAILED_UPDATE_INCOMPATIBLE/);
  assert.match(rotationSmoke, /uidPreserved/);
  assert.match(rotationSmoke, /firstInstallTimePreserved/);
  assert.match(rotationSmoke, /ALLOW_PHYSICAL/);
  assert.equal((rotationSmoke.match(/v4-signing-enabled/g) || []).length, 2);
  assert.match(
    rotationSmoke,
    /rmSync\(root, \{ recursive: true, force: true \}\)/,
  );
});

test("Android lint version advice suppression is scoped to the generated Cordova bridge", () => {
  const rootGradle = readFileSync(
    path.join(desktopRoot, "android/build.gradle"),
    "utf8",
  );
  const scopedContract =
    /subprojects \{ subproject ->[\s\S]*?if \(subproject\.name == 'capacitor-cordova-android-plugins'\) \{[\s\S]*?subproject\.plugins\.withId\('com\.android\.library'\) \{[\s\S]*?subproject\.android\.lint\.disable\.add\('NewerVersionAvailable'\)[\s\S]*?\}\s*\}/;

  assert.match(rootGradle, scopedContract);
  assert.equal((rootGradle.match(/\.android\.lint\.disable/g) || []).length, 1);
  assert.equal((rootGradle.match(/NewerVersionAvailable/g) || []).length, 1);
  assert.match(rootGradle, /Cordova 15 is\s*\n\s*\/\/ a template-governed major upgrade/);
  assert.doesNotMatch(rootGradle, /abortOnError|warningsAsErrors|checkAllWarnings/);

  const broadenedToEverySubproject = rootGradle.replace(
    "subproject.name == 'capacitor-cordova-android-plugins'",
    "true",
  );
  const changedToSecuritySuppression = rootGradle.replace(
    "'NewerVersionAvailable'",
    "'UnsafeIntentLaunch'",
  );
  assert.doesNotMatch(broadenedToEverySubproject, scopedContract);
  assert.doesNotMatch(changedToSecuritySuppression, scopedContract);
});

test("Android canonical locks install before Gradle and survive generated project recreation", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-android-locks-"));
  const locksRoot = path.join(root, "android", "gradle", "locks");
  const capacitorTarget = path.join(
    root,
    "node_modules",
    "@capacitor",
    "android",
    "capacitor",
  );
  const cordovaTarget = path.join(
    root,
    "android",
    "capacitor-cordova-android-plugins",
  );
  try {
    mkdirSync(locksRoot, { recursive: true });
    mkdirSync(capacitorTarget, { recursive: true });
    writeFileSync(
      path.join(root, "package-lock.json"),
      JSON.stringify({
        packages: { "node_modules/@capacitor/android": { version: "8.4.0" } },
      }),
    );
    writeFileSync(
      path.join(locksRoot, "capacitor-android-8.4.0.lockfile"),
      "capacitor-lock\n",
    );
    writeFileSync(
      path.join(locksRoot, "capacitor-cordova-android-plugins.lockfile"),
      "cordova-lock\n",
    );

    const deferred = installAndroidGradleLocks(root);
    assert.equal(deferred[0].installed, true);
    assert.equal(deferred[1].installed, false);
    assert.equal(
      readFileSync(path.join(capacitorTarget, "gradle.lockfile"), "utf8"),
      "capacitor-lock\n",
    );

    mkdirSync(cordovaTarget, { recursive: true });
    const installed = installAndroidGradleLocks(root);
    assert.equal(installed[1].installed, true);
    assert.equal(
      readFileSync(path.join(cordovaTarget, "gradle.lockfile"), "utf8"),
      "cordova-lock\n",
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("required CI and live workflows encode honest release and network gates", () => {
  const ci = readFileSync(
    path.join(repoRoot, ".github/workflows/ci.yml"),
    "utf8",
  );
  const release = readFileSync(
    path.join(repoRoot, ".github/workflows/build-android-tv-release.yml"),
    "utf8",
  );
  const live = readFileSync(
    path.join(repoRoot, ".github/workflows/live-contract.yml"),
    "utf8",
  );
  const windows = readFileSync(
    path.join(repoRoot, ".github/workflows/build-windows-installer.yml"),
    "utf8",
  );
  const migration = readFileSync(
    path.join(
      repoRoot,
      ".github/workflows/migrate-android-release-secrets.yml",
    ),
    "utf8",
  );
  const formalDesktop = readFileSync(
    path.join(
      repoRoot,
      ".github/workflows/build-desktop-release-candidates.yml",
    ),
    "utf8",
  );
  const windowsSmoke = readFileSync(
    path.join(desktopRoot, "scripts/windows-installed-smoke.ps1"),
    "utf8",
  );
  const normalizedWindowsSmoke = windowsSmoke.replace(/\r\n?/g, "\n");
  const pkg = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  );
  const playwright = readFileSync(
    path.join(desktopRoot, "playwright.config.ts"),
    "utf8",
  );
  const macCi = ci
    .split("  desktop-packaged-smoke:", 2)[1]
    .split("  desktop-linux-packaged-smoke:", 1)[0];
  const linuxCi = ci
    .split("  desktop-linux-packaged-smoke:", 2)[1]
    .split("  desktop-linux-provenance:", 1)[0];
  const androidCi = ci
    .split("  android-tv:", 2)[1]
    .split("  desktop-packaged-smoke:", 1)[0];

  assert.match(ci, /android-tv:\r?\n\s+name: Android \+ TV/);
  assert.match(ci, /permissions:\r?\n\s+contents: read/);
  assert.match(ci, /persist-credentials: false/);
  assert.doesNotMatch(ci, /--ignore-vuln/);
  assert.match(ci, /check-pip-audit-evidence\.py exception/);
  assert.match(ci, /check-pip-audit-evidence\.py clean/);
  assert.match(ci, /label: linux-x64-py311/);
  assert.match(ci, /label: macos-arm64-py311/);
  assert.match(ci, /label: windows-x64-py311/);
  assert.match(ci, /name: echodesk-python-audit-raw-\$\{\{ matrix\.label \}\}/);
  for (const lock of [
    "backend/requirements.lock",
    "backend/requirements-dev.lock",
    "backend/requirements-lint.lock",
    "backend/requirements-typecheck.lock",
    "backend/requirements-audit.lock",
    "backend/packaging/requirements-build.lock",
  ]) {
    assert.match(ci, new RegExp(lock.replaceAll(".", "\\.")));
  }
  assert.match(ci, /export ECHO_USER_DIR="\$RUNNER_TEMP\/echodesk-deterministic"/);
  assert.match(ci, /npm run app:build:android:development/);
  assert.match(androidCi, /:app:connectedDebugAndroidTest/);
  assert.match(androidCi, /if \[\[ ! -c \/dev\/kvm \]\]; then/);
  assert.match(androidCi, /sudo chmod a\+rw \/dev\/kvm/);
  assert.match(androidCi, /test -c \/dev\/kvm/);
  assert.match(androidCi, /test -r \/dev\/kvm/);
  assert.match(androidCi, /test -w \/dev\/kvm/);
  assert.doesNotMatch(androidCi, /udevadm|\/etc\/udev\/rules/);
  assert.ok(
    androidCi.indexOf("enable and verify KVM for Android instrumentation") <
      androidCi.indexOf("install pinned Android SDK components"),
  );
  assert.match(androidCi, /disable-linux-hw-accel: false/);
  assert.match(
    ci,
    /assembleRelease unexpectedly accepted missing stable signing inputs/,
  );
  assert.match(ci, /needs: \[[^\]]*android-tv/);
  assert.match(ci, /export ECHODESK_NODE_RUNTIME="\$\(command -v node\)"/);
  assert.match(ci, /ECHODESK_NODE_RUNTIME_IS_ELECTRON=true/);
  assert.match(ci, /name: echodesk-macos-arm64-adhoc-test/);
  assert.doesNotMatch(ci, /name: echodesk-macos-arm64-release/);
  assert.doesNotMatch(macCi, /id-token: write|attestations: write/);
  assert.doesNotMatch(linuxCi, /id-token: write|attestations: write/);
  assert.match(ci, /EchoDesk-\$\{version\}-linux-x86_64\.AppImage/);
  assert.match(ci, /EchoDesk-\$\{version\}-linux-amd64\.deb/);
  assert.match(ci, /extracted AppImage \+ bundled backend/);
  assert.match(ci, /installed deb \+ bundled backend/);
  assert.match(ci, /ECHODESK_SMOKE_PORT=18770/);
  assert.match(ci, /ECHODESK_SMOKE_PORT=18771/);
  assert.match(ci, /verify-release-update-metadata\.cjs linux/);
  assert.doesNotMatch(ci, /EchoDesk-\$\{version\}-linux-x64\.(?:AppImage|deb)/);
  assert.match(ci, /desktop-linux-provenance:/);
  assert.match(
    ci,
    /if: github\.event_name == 'push' && github\.ref == 'refs\/heads\/main'/,
  );
  assert.match(ci, /actions\/download-artifact@[0-9a-f]{40}/);
  assert.match(release, /missing required release secret/);
  assert.match(release, /release_sha:/);
  assert.match(release, /test "\$\{RELEASE_SHA\}" = "\$\{GITHUB_SHA\}"/);
  assert.match(release, /test "\$\{GITHUB_REF\}" = "refs\/heads\/main"/);
  assert.match(release, /concurrency:\r?\n\s+group: formal-android-tv-release-\$\{\{ inputs\.release_sha \}\}/);
  assert.match(release, /actions: read/);
  assert.doesNotMatch(release, /checks: read/);
  assert.match(release, /actions\/workflows\/ci\.yml\/runs\?branch=main&event=push&status=completed&head_sha=/);
  assert.match(release, /\.head_sha == \$sha/);
  assert.match(release, /\.head_branch == "main"/);
  assert.match(release, /\.event == "push"/);
  assert.match(release, /\.status == "completed"/);
  assert.match(release, /\.conclusion == "success"/);
  assert.match(release, /\.path == "\.github\/workflows\/ci\.yml"/);
  assert.match(release, /actions\/runs\/\$\{run_id\}\/jobs\?filter=latest/);
  assert.match(release, /\.name == "check"/);
  assert.doesNotMatch(release, /check-runs\?check_name=check/);
  assert.match(release, /needs: authorize-main/);
  assert.match(release, /environment: android-release/);
  assert.match(release, /ECHODESK_ANDROID_LEGACY_KEYSTORE_BASE64/);
  assert.match(release, /ECHODESK_ANDROID_CURRENT_KEYSTORE_BASE64/);
  assert.match(release, /ECHODESK_ANDROID_LEGACY_CERT_SHA256/);
  assert.match(release, /ECHODESK_ANDROID_CURRENT_CERT_SHA256/);
  assert.match(release, /ECHODESK_ANDROID_ROTATION_MIN_SDK_VERSION=33/);
  assert.match(release, /android-signing-lineage\.bin/);
  assert.match(
    release,
    /build and verify signed candidates in an isolated signing step/,
  );
  assert.match(release, /trap cleanup EXIT/);
  assert.match(release, /umask 077/);
  assert.match(release, /ECHODESK_VERSION=%s/);
  assert.doesNotMatch(
    release,
    /(?:KEYSTORE|KEY_PASSWORD|CERT_SHA256|KEY_ALIAS)[^\n]*GITHUB_ENV/,
  );
  assert.doesNotMatch(release, /path:[^\n]*(?:\.jks|\.keystore)/);
  assert.match(release, /npm run app:dist:android/);
  assert.match(release, /test -c \/dev\/kvm/);
  assert.match(release, /chmod a\+rw \/dev\/kvm/);
  assert.match(release, /test -r \/dev\/kvm/);
  assert.match(release, /test -w \/dev\/kvm/);
  assert.match(release, /disable-linux-hw-accel: false/);
  assert.match(release, /sha256sum --check --strict MANIFEST\.sha256/);
  assert.match(release, /generate-android-sbom\.py/);
  assert.match(release, /SHA256SUMS-Android\.txt/);
  assert.match(release, /diff -u/);
  assert.doesNotMatch(release, /^\s+desktop\/release\/.*\*/m);
  assert.match(live, /runs-on: ubuntu-latest/);
  assert.match(
    live,
    /runs-on: \[self-hosted, linux, x64, echodesk-private-models, actions-runner-2-327-1\]/,
  );
  assert.match(live, /run_public_model:/);
  assert.match(live, /permissions:\r?\n\s+contents: read/);
  assert.match(live, /environment: live-contract/);
  assert.match(live, /persist-credentials: false/);
  assert.match(live, /github\.repository == 'yoligehude14753\/echo-demo'/);
  assert.match(live, /github\.ref == 'refs\/heads\/main'/);
  assert.match(live, /inputs\.run_public_model == true/);
  assert.doesNotMatch(live, /\bschedule:/);
  assert.match(live, /tests\/integration\/test_product_model_live\.py/);
  assert.match(live, /private route unavailable: CONNECTIVITY_CHECK_FAILED/);
  assert.match(live, /route invalid: MISSING_HOST/);
  assert.doesNotMatch(live, /\{host\}:\{port\}|\{raw\}|\{address\}|\{exc\}/);
  assert.doesNotMatch(live, /pytest tests -m live/);
  assert.doesNotMatch(windows, /name: echodesk-windows-unsigned-test/);
  assert.doesNotMatch(
    windows,
    /path:\s*\|[\s\S]*desktop\/release\/EchoDesk\.Setup\.\$\{\{ env\.ECHODESK_VERSION \}\}\.exe/,
  );
  assert.doesNotMatch(windows, /name: echodesk-windows-release/);
  assert.match(windows, /Refuse unsigned public publishing/);
  assert.match(windows, /Portable ZIP startup smoke/);
  assert.match(windows, /-ApplicationDirectory \$app\.DirectoryName/);
  assert.match(windows, /diff -u/);
  assert.doesNotMatch(windows, /^\s+desktop\/release\/.*\*/m);
  assert.doesNotMatch(windows, /gh release upload/);
  assert.doesNotMatch(
    windows,
    /id-token: write|attestations: write|attest-build-provenance/,
  );
  assert.match(pkg.scripts["app:dist:linux"], /--publish never$/);
  assert.match(formalDesktop, /workflow_dispatch:/);
  assert.doesNotMatch(formalDesktop, /\bpush:/);
  assert.match(formalDesktop, /release_sha:/);
  assert.match(formalDesktop, /test "\$\{GITHUB_REF\}" = "refs\/heads\/main"/);
  assert.match(formalDesktop, /test "\$\{actual\}" = "\$\{GITHUB_SHA\}"/);
  assert.match(formalDesktop, /actions: read/);
  assert.doesNotMatch(formalDesktop, /checks: read/);
  assert.match(formalDesktop, /actions\/workflows\/ci\.yml\/runs\?branch=main&event=push&status=completed&head_sha=/);
  assert.match(formalDesktop, /\.head_sha == \$sha/);
  assert.match(formalDesktop, /\.head_branch == "main"/);
  assert.match(formalDesktop, /\.event == "push"/);
  assert.match(formalDesktop, /\.status == "completed"/);
  assert.match(formalDesktop, /\.conclusion == "success"/);
  assert.match(formalDesktop, /\.path == "\.github\/workflows\/ci\.yml"/);
  assert.match(formalDesktop, /actions\/runs\/\$\{run_id\}\/jobs\?filter=latest/);
  assert.match(formalDesktop, /\.name == "check"/);
  assert.doesNotMatch(formalDesktop, /check-runs\?check_name=check/);
  assert.equal(
    (formalDesktop.match(/needs: authorize-main/g) || []).length,
    2,
    "both formal desktop signing jobs must depend on the exact-SHA CI authorization",
  );
  assert.match(formalDesktop, /environment: desktop-release-macos/);
  assert.match(formalDesktop, /environment: desktop-release-windows/);
  assert.match(formalDesktop, /Blocked: macOS release credentials missing/);
  assert.match(formalDesktop, /Blocked: Windows release credentials missing/);
  assert.match(formalDesktop, /npm run app:dist:mac/);
  assert.match(formalDesktop, /npm run app:dist:win/);
  assert.match(formalDesktop, /npm run smoke:mac:dmg/);
  assert.match(formalDesktop, /Verify and smoke the final signed macOS updater ZIP/);
  assert.match(formalDesktop, /ditto -x -k "\$archive" "\$extract_root"/);
  assert.match(formalDesktop, /codesign --verify --deep --strict/);
  assert.match(formalDesktop, /xcrun stapler validate "\$app"/);
  assert.match(formalDesktop, /spctl --assess --type execute/);
  assert.match(formalDesktop, /packaged-local-smoke\.spec\.ts/);
  assert.match(formalDesktop, /windows-installed-smoke\.ps1/);
  assert.match(formalDesktop, /-ExpectedAuthenticodeThumbprint \$thumbprint/);
  assert.match(
    formalDesktop,
    /-ExpectedAuthenticodePublisher \$env:WINDOWS_EXPECTED_PUBLISHER/,
  );
  assert.match(formalDesktop, /WINDOWS_EXPECTED_PUBLISHER/);
  assert.match(formalDesktop, /verify-windows-authenticode\.ps1/);
  assert.match(formalDesktop, /foreach \(\$artifact in @\(\$app, \$backend\)\)/);
  assert.match(formalDesktop, /latest-mac\.yml/);
  assert.match(formalDesktop, /\.dmg\.blockmap/);
  assert.match(formalDesktop, /\.zip\.blockmap/);
  assert.match(formalDesktop, /generate-release-sbom\.py/);
  assert.match(formalDesktop, /SHA256SUMS-macOS\.txt/);
  assert.match(formalDesktop, /SHA256SUMS-Windows\.txt/);
  assert.match(formalDesktop, /diff -u/);
  assert.doesNotMatch(formalDesktop, /^\s+desktop\/release\/.*\*/m);
  assert.match(formalDesktop, /verify-release-update-metadata\.cjs mac/);
  assert.match(formalDesktop, /verify-release-update-metadata\.cjs windows/);
  assert.match(formalDesktop, /actions\/attest-build-provenance@/);
  assert.match(formalDesktop, /Upload signed macOS candidate without publishing/);
  assert.match(formalDesktop, /Upload signed Windows candidate without publishing/);
  assert.doesNotMatch(formalDesktop, /gh release upload/);
  assert.doesNotMatch(formalDesktop, /contents:\s*write/);
  assert.match(windowsSmoke, /ExpectedAuthenticodeThumbprint/);
  assert.match(windowsSmoke, /ExpectedAuthenticodePublisher/);
  assert.match(
    windowsSmoke,
    /\$authenticodeThumbprintConfigured -ne \$authenticodePublisherConfigured/,
  );
  assert.match(windowsSmoke, /scripts\/verify-windows-authenticode\.ps1/);
  assert.match(
    windowsSmoke,
    /foreach \(\$artifact in @\(\$installedApp, \$installedBackend\)\)/,
  );
  const silentInstallIndex = normalizedWindowsSmoke.indexOf(
    "Invoke-CheckedNativeProcess @installParameters",
  );
  const installedVerificationIndex = normalizedWindowsSmoke.lastIndexOf(
    "if ($authenticodeVerificationEnabled)",
  );
  const packagedCodeExecutionIndex = normalizedWindowsSmoke.indexOf(
    "\n  Set-SmokeEnvironment\n",
  );
  assert.ok(
    [
      silentInstallIndex,
      installedVerificationIndex,
      packagedCodeExecutionIndex,
    ].every((index) => index >= 0),
    "installed signing-order markers must all exist",
  );
  assert.ok(
    silentInstallIndex < installedVerificationIndex,
    "installed Authenticode verification must happen after silent installation",
  );
  assert.ok(
    installedVerificationIndex < packagedCodeExecutionIndex,
    "installed Authenticode verification must happen before packaged code executes",
  );
  assert.match(migration, /workflow_dispatch:/);
  assert.match(migration, /phase:/);
  assert.match(migration, /- copy/);
  assert.match(migration, /- cleanup/);
  assert.match(migration, /release_sha:/);
  assert.match(migration, /copy_run_id:/);
  assert.match(migration, /validated_run_id:/);
  assert.match(migration, /test "\$\{RELEASE_SHA\}" = "\$\{GITHUB_SHA\}"/);
  assert.doesNotMatch(migration, /\bpush:/);
  assert.match(migration, /test "\$\{GITHUB_REF\}" = "refs\/heads\/main"/);
  assert.equal(
    (migration.match(/environment: android-release-migration/g) || []).length,
    2,
  );
  assert.match(
    migration,
    /gh secret set "\$\{name\}"\s*\\?\s+--env android-release/,
  );
  assert.match(
    migration,
    /gh secret delete "\$\{name\}"\s*\\?\s+--repo/,
  );
  const copyPhase = migration
    .split("  copy-secrets:", 2)[1]
    .split("  cleanup-repository-secrets:", 1)[0];
  const cleanupPhase = migration.split("  cleanup-repository-secrets:", 2)[1];
  assert.doesNotMatch(copyPhase, /gh secret delete/);
  assert.match(copyPhase, /Repository sources remain intact/);
  assert.match(cleanupPhase, /COPY_RUN_ID/);
  assert.match(cleanupPhase, /VALIDATED_RUN_ID/);
  assert.match(cleanupPhase, /copy signing secrets to protected target/);
  assert.match(
    cleanupPhase,
    /\.path == "\.github\/workflows\/build-android-tv-release\.yml"/,
  );
  assert.match(cleanupPhase, /fromdateiso8601/);
  assert.match(cleanupPhase, /echodesk-android-tv-signed-release-candidate/);
  assert.match(cleanupPhase, /gh secret delete/);
  assert.match(migration, /revoke it at the issuer/);
  assert.doesNotMatch(migration, /set -x|echo "\$\{![^}]+\}"/);
  assert.match(playwright, /retries: 0/);
  assert.doesNotMatch(playwright, /retries: process\.env\.CI/);
});

test("CI platform builds stay independent and preserve failure diagnostics", () => {
  const ci = yaml.load(
    readFileSync(path.join(repoRoot, ".github/workflows/ci.yml"), "utf8"),
  );
  const windows = yaml.load(
    readFileSync(
      path.join(repoRoot, ".github/workflows/build-windows-installer.yml"),
      "utf8",
    ),
  );
  const platformInput = ci.on?.workflow_dispatch?.inputs?.platform;
  assert.equal(platformInput?.type, "choice");
  assert.equal(platformInput?.default, "all");
  assert.deepEqual(platformInput?.options, [
    "all",
    "backend",
    "macos",
    "linux",
    "windows",
    "android",
  ]);
  assert.equal(ci.concurrency?.["cancel-in-progress"], false);
  assert.equal(ci.concurrency?.queue, "max");

  const jobs = ci.jobs || {};
  const selectors = new Map([
    ["android-tv", "android"],
    ["desktop-packaged-smoke", "macos"],
    ["desktop-linux-packaged-smoke", "linux"],
    ["desktop-windows-packaged-smoke", "windows"],
  ]);
  for (const [jobName, selector] of selectors) {
    const job = jobs[jobName];
    assert.ok(job, `${jobName} must exist`);
    assert.equal(job.needs, undefined, `${jobName} must stay independent`);
    assert.match(job.if, /github\.event_name != 'workflow_dispatch'/);
    assert.match(job.if, /inputs\.platform == 'all'/);
    assert.match(job.if, new RegExp(`inputs\\.platform == '${selector}'`));
    assert.doesNotMatch(JSON.stringify(job), /continue-on-error/);
  }

  const requireFailureDiagnostics = (jobName, label) => {
    const failureOrCancellation = "${{ failure() || cancelled() }}";
    const steps = jobs[jobName]?.steps || [];
    const collect = steps.find(
      (step) => step.name === `collect ${label} smoke failure diagnostics`,
    );
    const upload = steps.find(
      (step) => step.name === `upload ${label} smoke failure diagnostics`,
    );
    assert.equal(
      collect?.if,
      failureOrCancellation,
      `${jobName} must collect on failure or cancellation`,
    );
    assert.match(collect?.run || "", /job-context\.txt/);
    assert.equal(
      upload?.if,
      failureOrCancellation,
      `${jobName} must upload on failure or cancellation`,
    );
    assert.match(upload?.uses || "", /^actions\/upload-artifact@[0-9a-f]{40}$/);
    assert.match(upload?.with?.path || "", /runner\.temp/);
    assert.equal(upload?.with?.["if-no-files-found"], "error");
  };
  requireFailureDiagnostics("android-tv", "Android");
  requireFailureDiagnostics("desktop-packaged-smoke", "macOS");
  requireFailureDiagnostics("desktop-linux-packaged-smoke", "Linux");

  const backendFailureUpload = jobs["backend-test"].steps.find(
    (step) => step.name === "upload backend test failure diagnostics",
  );
  const desktopFailureUpload = jobs["desktop-e2e"].steps.find(
    (step) => step.name === "upload desktop E2E failure diagnostics",
  );
  for (const upload of [backendFailureUpload, desktopFailureUpload]) {
    assert.equal(upload?.if, "${{ failure() || cancelled() }}");
    assert.equal(upload?.with?.["if-no-files-found"], "error");
  }

  const windowsSteps = windows.jobs?.["build-windows"]?.steps || [];
  const windowsCollect = windowsSteps.find(
    (step) => step.name === "Collect Windows smoke failure diagnostics",
  );
  const windowsUpload = windowsSteps.find(
    (step) => step.name === "Upload Windows installed smoke evidence",
  );
  assert.equal(windowsCollect?.if, "${{ failure() || cancelled() }}");
  assert.match(windowsCollect?.run || "", /failure\.log/);
  assert.equal(windowsUpload?.if, "always()");
  assert.equal(windowsUpload?.with?.["if-no-files-found"], "error");

  const requiredJobs = [
    "backend-lint",
    "backend-typecheck",
    "backend-dependency-audit",
    "backend-test",
    "desktop-e2e",
    "android-tv",
    "desktop-packaged-smoke",
    "desktop-windows-packaged-smoke",
  ];
  assert.match(
    jobs.check?.name || "",
    /github\.event_name == 'workflow_dispatch'.*inputs\.platform != 'all'.*'manual platform check'.*\|\| 'check'/,
  );
  assert.equal(jobs.check?.if, "always()");
  assert.deepEqual(jobs.check?.needs, requiredJobs);
  const gate = jobs.check?.steps?.find((step) => step.name === "gate")?.run || "";
  assert.match(gate, /event_name.*workflow_dispatch/);
  assert.match(gate, /platform.*all/);
  for (const jobName of requiredJobs) {
    assert.match(gate, new RegExp(`needs\\.${jobName}\\.result`));
  }

  assert.equal(ci.permissions?.contents, "read");
  assert.equal(jobs["desktop-windows-packaged-smoke"].with.publish_release, false);
  const windowsPublishGuard = windowsSteps.find(
    (step) => step.name === "Refuse unsigned public publishing",
  );
  assert.match(windowsPublishGuard?.if || "", /inputs\.publish_release == true/);
  assert.match(windowsPublishGuard?.run || "", /exit 1/);
  assert.match(
    jobs["desktop-packaged-smoke"].steps
      .map((step) => step.run || "")
      .join("\n"),
    /app:dist:mac:adhoc-test/,
  );
  assert.match(
    jobs["android-tv"].steps.map((step) => step.run || "").join("\n"),
    /assembleRelease unexpectedly accepted missing stable signing inputs/,
  );
  const candidateJobs = JSON.stringify(
    [...selectors.keys()].map((jobName) => jobs[jobName]),
  );
  assert.doesNotMatch(
    candidateJobs,
    /contents["']?:["']?write|gh release (?:create|upload)|--publish (?!never)|publish_release["']?:true/i,
  );
});

test("Linux packaged CI fails fast on non-CPU Python dependencies before packaging", () => {
  const document = yaml.load(
    readFileSync(path.join(repoRoot, ".github/workflows/ci.yml"), "utf8"),
  );
  const job = document.jobs?.["desktop-linux-packaged-smoke"];
  assert.ok(job, "Linux packaged smoke job must exist");

  const assertPreflightContract = (steps) => {
    const stepByName = (name) => {
      const matches = steps.filter((step) => step.name === name);
      assert.equal(matches.length, 1, `expected exactly one ${name} step`);
      return matches[0];
    };
    const stepIndex = (name) => steps.indexOf(stepByName(name));

    const lockName =
      "validate CPU-only Python locks before dependency installation";
    const installName = "install clean Linux CPU build dependencies";
    const runtimeName =
      "verify installed Linux CPU-only runtime before packaging";
    const frontendName = "install clean Linux frontend and smoke dependencies";
    const electronName = "run Electron main-process contracts";
    const buildName = "build x64 backend, AppImage and deb";
    const unpackedSmokeName =
      "unpacked Electron + bundled backend + artifact + persistence smoke";
    const appImageSmokeName =
      "extracted AppImage + bundled backend + artifact + persistence smoke";
    const debSmokeName =
      "installed deb + bundled backend + artifact + persistence smoke";

    const lockIndex = stepIndex(lockName);
    const installIndex = stepIndex(installName);
    const runtimeIndex = stepIndex(runtimeName);
    const frontendIndex = stepIndex(frontendName);
    const electronIndex = stepIndex(electronName);
    const buildIndex = stepIndex(buildName);
    const unpackedSmokeIndex = stepIndex(unpackedSmokeName);
    const appImageSmokeIndex = stepIndex(appImageSmokeName);
    const debSmokeIndex = stepIndex(debSmokeName);
    assert.ok(
      lockIndex < installIndex,
      "lock validation must run before Python dependency installation",
    );
    assert.ok(
      installIndex < runtimeIndex,
      "installed runtime verification must follow the hashed build-lock install",
    );
    assert.ok(
      runtimeIndex < frontendIndex &&
        runtimeIndex < electronIndex &&
        runtimeIndex < buildIndex,
      "CPU runtime verification must run before frontend setup, Electron contracts, and packaging",
    );
    assert.ok(
      buildIndex < unpackedSmokeIndex &&
        unpackedSmokeIndex < appImageSmokeIndex &&
        appImageSmokeIndex < debSmokeIndex,
      "Linux packaging must retain unpacked, extracted AppImage, and installed deb smoke states",
    );

    assert.equal(
      stepByName(lockName).run.trim(),
      "python scripts/check-python-locks.py",
    );
    assert.match(
      stepByName(installName).run,
      /python -m pip install --require-hashes -r backend\/packaging\/requirements-build\.lock/,
      "build dependencies must come only from the hashed build lock",
    );
    const runtime = stepByName(runtimeName).run;
    for (const assertion of [
      'metadata.version("torch") == expected',
      'metadata.version("torchaudio") == expected',
      "torch.__version__ == expected",
      "torchaudio.__version__ == expected",
      "torch.version.cuda is None",
      "torch.cuda.is_available() is False",
      'name == "triton"',
      'name.startswith(("cuda-", "nvidia-"))',
      "assert not forbidden",
    ]) {
      assert.ok(
        runtime.includes(assertion),
        `CPU runtime preflight must enforce ${assertion}`,
      );
    }
    assert.match(
      runtime,
      /expected = "2\.11\.0\+cpu"/,
      "CPU runtime preflight must require the exact Linux wheel build",
    );
    assert.doesNotMatch(
      stepByName(frontendName).run,
      /pip install|requirements-build\.lock/,
      "slow frontend setup must not defer Python dependency installation",
    );
  };

  assertPreflightContract(job.steps);

  const reordered = structuredClone(job.steps);
  const lockIndex = reordered.findIndex((step) =>
    step.name?.startsWith("validate CPU-only Python locks"),
  );
  const [lockStep] = reordered.splice(lockIndex, 1);
  const buildIndex = reordered.findIndex(
    (step) => step.name === "build x64 backend, AppImage and deb",
  );
  reordered.splice(buildIndex + 1, 0, lockStep);
  assert.throws(
    () => assertPreflightContract(reordered),
    /lock validation must run before Python dependency installation/,
  );

  const weakened = structuredClone(job.steps);
  const runtimeStep = weakened.find(
    (step) =>
      step.name === "verify installed Linux CPU-only runtime before packaging",
  );
  runtimeStep.run = runtimeStep.run.replace(
    'expected = "2.11.0+cpu"',
    'expected = "2.11.0"',
  );
  assert.throws(
    () => assertPreflightContract(weakened),
    /exact Linux wheel build/,
  );
});

test("public build content scans fail closed on missing roots and find errors", () => {
  const workflowStepRun = (relativePath, stepName) => {
    const document = yaml.load(
      readFileSync(path.join(repoRoot, relativePath), "utf8"),
    );
    const matches = Object.values(document.jobs || {}).flatMap((job) =>
      (job.steps || []).filter((step) => step.name === stepName),
    );
    assert.equal(
      matches.length,
      1,
      `${relativePath} must contain exactly one ${stepName} step`,
    );
    assert.equal(typeof matches[0].run, "string");
    return matches[0].run;
  };
  const assertStaticScanContract = (run, label, requiredNames) => {
    assert.match(run, /^set -euo pipefail$/m, label);
    assert.match(
      run,
      /scan_roots=\(desktop\/dist backend\/dist desktop\/release\)/,
      label,
    );
    assert.match(
      run,
      /for scan_root in "\$\{scan_roots\[@\]\}"; do[\s\S]*test -d "\$\{scan_root\}" \|\| \{[\s\S]*exit 1[\s\S]*done/,
      `${label} must reject a missing scan root`,
    );
    assert.match(run, /! -path '\*\/certifi\/cacert\.pem'/, label);
    assert.doesNotMatch(
      run,
      /2>\/?dev\/null|\|\| true/,
      `${label} must not silence content-scan errors`,
    );
    assert.match(
      run,
      /found="\$\(find "\$\{scan_roots\[@\]\}"[\s\S]*-print\)"$/m,
      `${label} must capture a direct, fail-closed find command`,
    );
    for (const name of requiredNames) {
      assert.ok(
        run.includes(`-name '${name}'`),
        `${label} must scan ${name}`,
      );
    }
  };
  const guards = [
    {
      label: "linux CI",
      run: workflowStepRun(
        ".github/workflows/ci.yml",
        "guard Linux public build contents",
      ),
      forbiddenExtension: ".key",
      requiredNames: ["*.map", ".env", "*.pem", "*.key"],
    },
    {
      label: "unsigned Windows",
      run: workflowStepRun(
        ".github/workflows/build-windows-installer.yml",
        "Guard public build contents",
      ),
      forbiddenExtension: ".key",
      requiredNames: ["*.map", ".env", "*.pem", "*.key"],
    },
    {
      label: "formal macOS",
      run: workflowStepRun(
        ".github/workflows/build-desktop-release-candidates.yml",
        "Require complete macOS updater assets",
      ),
      forbiddenExtension: ".p12",
      requiredNames: ["*.map", ".env", "*.p12", "*.pem", "*.key"],
    },
    {
      label: "formal Windows",
      run: workflowStepRun(
        ".github/workflows/build-desktop-release-candidates.yml",
        "Require complete Windows updater assets",
      ),
      forbiddenExtension: ".pfx",
      requiredNames: ["*.map", ".env", "*.pfx", "*.pem", "*.key"],
    },
  ];
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-build-scan-gates-"));
  const bash = "bash";

  try {
    for (const [index, contract] of guards.entries()) {
      assertStaticScanContract(
        contract.run,
        contract.label,
        contract.requiredNames,
      );

      if (process.platform === "win32") {
        continue;
      }

      const marker = contract.run.indexOf("scan_roots=(");
      assert.ok(marker >= 0, `${contract.label} must declare scan roots`);
      const guardScript = `set -euo pipefail\n${contract.run.slice(marker)}`;
      const caseRoot = path.join(root, String(index));
      for (const relative of ["desktop/dist", "backend/dist", "desktop/release"]) {
        mkdirSync(path.join(caseRoot, relative), { recursive: true });
      }
      const certifi = path.join(caseRoot, "backend/dist/vendor/certifi");
      mkdirSync(certifi, { recursive: true });
      writeFileSync(path.join(certifi, "cacert.pem"), "public CA bundle\n");

      const accepted = spawnSync(bash, ["-c", guardScript], {
        cwd: caseRoot,
        encoding: "utf8",
      });
      assert.equal(
        accepted.status,
        0,
        `${contract.label} rejected certifi exception:\n${accepted.stderr}`,
      );

      const forbidden = path.join(
        caseRoot,
        `desktop/release/private${contract.forbiddenExtension}`,
      );
      writeFileSync(forbidden, "secret\n");
      const forbiddenResult = spawnSync(bash, ["-c", guardScript], {
        cwd: caseRoot,
        encoding: "utf8",
      });
      assert.notEqual(forbiddenResult.status, 0, contract.label);
      assert.match(forbiddenResult.stderr, /forbidden files?/i, contract.label);
      rmSync(forbidden);

      rmSync(path.join(caseRoot, "backend/dist"), { recursive: true, force: true });
      const missingRoot = spawnSync(bash, ["-c", guardScript], {
        cwd: caseRoot,
        encoding: "utf8",
      });
      assert.notEqual(missingRoot.status, 0, contract.label);
      assert.match(missingRoot.stderr, /scan root is missing/, contract.label);
      mkdirSync(path.join(caseRoot, "backend/dist"), { recursive: true });

      const fakeBin = path.join(caseRoot, "fake-bin");
      mkdirSync(fakeBin, { recursive: true });
      const fakeFind = path.join(fakeBin, process.platform === "win32" ? "find.exe" : "find");
      writeFileSync(
        fakeFind,
        "#!/usr/bin/env bash\necho forced find failure >&2\nexit 17\n",
      );
      chmodSync(fakeFind, 0o755);
      const findFailure = spawnSync(bash, ["-c", guardScript], {
        cwd: caseRoot,
        encoding: "utf8",
        env: {
          ...process.env,
          PATH: `${fakeBin}${path.delimiter}${process.env.PATH || ""}`,
        },
      });
      assert.notEqual(findFailure.status, 0, contract.label);
      assert.match(findFailure.stderr, /forced find failure/, contract.label);
    }

    const failOpenCounterexample = guards[0].run.replace(
      '-print)"',
      '-print || true)"',
    );
    assert.notEqual(failOpenCounterexample, guards[0].run);
    assert.throws(
      () =>
        assertStaticScanContract(
          failOpenCounterexample,
          "fail-open counterexample",
          guards[0].requiredNames,
        ),
      /must not silence content-scan errors/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("action pin gate scans yaml and rejects quoted or flow-style mutable uses", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-action-pins-"));
  const checker = path.join(repoRoot, "scripts/check-ci-action-pins.py");
  const python = process.platform === "win32" ? "python.exe" : "python3";
  const probe = [
    "import importlib.util, json, sys",
    "from pathlib import Path",
    "spec = importlib.util.spec_from_file_location('pins', sys.argv[1])",
    "module = importlib.util.module_from_spec(spec)",
    "spec.loader.exec_module(module)",
    "print(json.dumps(module.validate_workflows(Path(sys.argv[2]))))",
  ].join("; ");
  try {
    writeFileSync(
      path.join(root, "quoted.yaml"),
      'jobs:\n  audit:\n    steps:\n      - "uses": "owner/action@main"\n',
    );
    writeFileSync(
      path.join(root, "flow.yml"),
      "jobs:\n  audit:\n    steps:\n      - { uses: another/action@v1 }\n",
    );
    const result = spawnSync(python, ["-c", probe, checker, root], {
      encoding: "utf8",
    });
    assert.equal(result.status, 0, result.stderr);
    const violations = JSON.parse(result.stdout);
    assert.equal(violations.length, 2);
    assert.ok(violations.some((item) => item.includes("owner/action@main")));
    assert.ok(violations.some((item) => item.includes("another/action@v1")));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("raw pip-audit evidence permits only the explicit dependency exceptions", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-pip-audit-"));
  const report = path.join(root, "report.json");
  const exitCode = path.join(root, "report.exit-code");
  const lock = path.join(root, "requirements.lock");
  const exception = path.join(root, "exception.md");
  const parser = path.join(repoRoot, "scripts/check-pip-audit-evidence.py");
  const python = process.platform === "win32" ? "python.exe" : "python3";
  const torchFinding = {
    dependencies: [
      {
        name: "torch",
        version: "2.11.0",
        vulns: [
          {
            id: "CVE-2025-3000",
            aliases: ["GHSA-rrmf-rvhw-rf47"],
            fix_versions: [],
          },
        ],
      },
    ],
    fixes: [],
  };
  const setuptoolsFinding = {
    dependencies: [
      {
        name: "setuptools",
        version: "81.0.0",
        vulns: [
          {
            id: "CVE-2026-59890",
            aliases: ["PYSEC-2026-3447", "GHSA-h35f-9h28-mq5c"],
            fix_versions: ["83.0.0"],
          },
        ],
      },
    ],
    fixes: [],
  };
  const dualExceptionArgs = [
    "--package",
    "torch",
    "--vulnerability",
    "CVE-2025-3000",
    "--package",
    "setuptools",
    "--vulnerability",
    "CVE-2026-59890",
  ];
  const writeReport = (dependencies) =>
    writeFileSync(report, JSON.stringify({ dependencies, fixes: [] }));
  const run = (mode, extra = []) =>
    spawnSync(
      python,
      [
        parser,
        mode,
        "--report",
        report,
        "--exit-code",
        exitCode,
        ...extra,
      ],
      { encoding: "utf8" },
    );

  try {
    writeFileSync(lock, "torch==2.11.0\nsetuptools==81.0.0\n");
    writeFileSync(
      exception,
      "## CVE-2025-3000 / GHSA-rrmf-rvhw-rf47 — torch 2.11.0\n\n" +
        "- Exception expires: 2999-12-31\n\n" +
        "## CVE-2026-59890 / GHSA-h35f-9h28-mq5c — setuptools 81.0.0\n\n" +
        "- Exception expires: 2999-12-31\n" +
        "- Compatible fixed release: setuptools>=83.0.0\n",
    );
    writeReport([torchFinding.dependencies[0], setuptoolsFinding.dependencies[0]]);
    writeFileSync(exitCode, "1\n");
    const accepted = run("exception", [
      "--lock",
      lock,
      "--exception",
      exception,
      ...dualExceptionArgs,
    ]);
    assert.equal(accepted.status, 0, `${accepted.stdout}\n${accepted.stderr}`);

    writeFileSync(
      lock,
      "torch==2.11.0 ; sys_platform == 'darwin'\\\n" +
        "torch==2.11.0+cpu ; sys_platform != 'darwin'\\\n" +
        "setuptools==81.0.0\n",
    );
    const cpuFinding = structuredClone(torchFinding);
    cpuFinding.dependencies[0].version = "2.11.0+cpu";
    writeReport([cpuFinding.dependencies[0], setuptoolsFinding.dependencies[0]]);
    const acceptedCpuBuild = run("exception", [
      "--lock",
      lock,
      "--exception",
      exception,
      ...dualExceptionArgs,
    ]);
    assert.equal(
      acceptedCpuBuild.status,
      0,
      `${acceptedCpuBuild.stdout}\n${acceptedCpuBuild.stderr}`,
    );
    const normalizedLock = path.join(root, "requirements-audit.lock");
    const normalized = spawnSync(
      python,
      [
        parser,
        "normalize-lock",
        "--lock",
        lock,
        "--output",
        normalizedLock,
      ],
      { encoding: "utf8" },
    );
    assert.equal(normalized.status, 0, normalized.stderr);
    assert.doesNotMatch(readFileSync(normalizedLock, "utf8"), /\+cpu/);
    assert.match(readFileSync(lock, "utf8"), /torch==2\.11\.0\+cpu/);

    const unpinnedLocalBuild = structuredClone(torchFinding);
    unpinnedLocalBuild.dependencies[0].version = "2.11.0+cuda";
    writeReport([
      unpinnedLocalBuild.dependencies[0],
      setuptoolsFinding.dependencies[0],
    ]);
    const rejectedLocalBuild = run("exception", [
      "--lock",
      lock,
      "--exception",
      exception,
      ...dualExceptionArgs,
    ]);
    assert.notEqual(rejectedLocalBuild.status, 0);
    assert.match(rejectedLocalBuild.stderr, /unexpected exception finding/);

    writeFileSync(
      lock,
      "torch==2.11.0+cuda ; sys_platform != 'darwin'\\\n" +
        "setuptools==81.0.0\n",
    );
    const rejectedAuditInput = spawnSync(
      python,
      [
        parser,
        "normalize-lock",
        "--lock",
        lock,
        "--output",
        normalizedLock,
      ],
      { encoding: "utf8" },
    );
    assert.notEqual(rejectedAuditInput.status, 0);
    assert.match(rejectedAuditInput.stderr, /unreviewed local dependency build/);

    const extraFinding = structuredClone(torchFinding);
    extraFinding.dependencies.push(setuptoolsFinding.dependencies[0]);
    extraFinding.dependencies.push({
      name: "requests",
      version: "0.0.1",
      vulns: [{ id: "CVE-2999-0001", aliases: [], fix_versions: ["1.0.0"] }],
    });
    writeReport(extraFinding.dependencies);
    const rejected = run("exception", [
      "--lock",
      lock,
      "--exception",
      exception,
      ...dualExceptionArgs,
    ]);
    assert.notEqual(rejected.status, 0);
    assert.match(rejected.stderr, /exactly 2 findings/);

    writeFileSync(report, JSON.stringify({ dependencies: [], fixes: [] }));
    writeFileSync(exitCode, "0\n");
    const clean = run("clean");
    assert.equal(clean.status, 0, `${clean.stdout}\n${clean.stderr}`);
    writeFileSync(exitCode, "1\n");
    const dishonestClean = run("clean");
    assert.notEqual(dishonestClean.status, 0);
    assert.match(dishonestClean.stderr, /clean audit must exit 0/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("release SBOMs are deterministic and bind their serials to locked inputs", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-sbom-"));
  const python = process.platform === "win32" ? "python.exe" : "python3";
  const documents = [];
  try {
    for (const [label, scriptName] of [
      ["desktop", "generate-release-sbom.py"],
      ["android", "generate-android-sbom.py"],
    ]) {
      const first = path.join(root, `${label}-first.json`);
      const second = path.join(root, `${label}-second.json`);
      for (const output of [first, second]) {
        const generated = spawnSync(
          python,
          [path.join(repoRoot, "scripts", scriptName), output],
          { encoding: "utf8" },
        );
        assert.equal(
          generated.status,
          0,
          `${generated.stdout}\n${generated.stderr}`,
        );
      }
      assert.deepEqual(readFileSync(first), readFileSync(second));
      const document = JSON.parse(readFileSync(first, "utf8"));
      assert.equal(document.bomFormat, "CycloneDX");
      assert.equal(document.specVersion, "1.5");
      assert.match(document.serialNumber, /^urn:uuid:[0-9a-f-]{36}$/);
      assert.equal(document.metadata.component.version, "0.3.3");
      assert.ok(document.components.length > 0);
      const references = document.components.map((item) => item["bom-ref"]);
      assert.deepEqual(references, [...references].sort());
      assert.equal(new Set(references).size, references.length);
      for (const property of document.metadata.properties) {
        assert.match(property.name, /^echodesk:/);
        assert.match(property.value, /^[0-9a-f]{64}$/);
      }
      documents.push(document);
    }
    assert.notEqual(documents[0].serialNumber, documents[1].serialNumber);
    const desktopByRef = new Map(
      documents[0].components.map((component) => [component["bom-ref"], component]),
    );
    for (const reference of [
      "pkg:npm/docxtemplater@3.68.7",
      "pkg:npm/pizzip@3.2.0",
      "pkg:npm/pptxgenjs@3.12.0",
    ]) {
      assert.equal(desktopByRef.get(reference)?.scope, "required", reference);
    }
    const desktopProperties = new Set(
      documents[0].metadata.properties.map((property) => property.name),
    );
    assert.ok(desktopProperties.has("echodesk:desktop-npm-lock-sha256"));
    assert.ok(desktopProperties.has("echodesk:ppt-runtime-npm-lock-sha256"));

    const gradle = documents[1].components.filter((component) =>
      component["bom-ref"].startsWith("pkg:maven/"),
    );
    const requiredGradle = gradle.filter(
      (component) => component.scope === "required",
    );
    const optionalGradle = gradle.filter(
      (component) => component.scope === "optional",
    );
    assert.ok(requiredGradle.length > 0);
    assert.ok(optionalGradle.length > 0);
    assert.ok(
      requiredGradle.every(
        (component) =>
          component.properties?.[0]?.value === "releaseRuntimeClasspath",
      ),
    );
    assert.ok(
      optionalGradle.every((component) =>
        component.properties?.[0]?.value.includes("not proven release runtime"),
      ),
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("desktop SBOM fails when the frozen PPT runtime lock or a direct component is missing", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-sbom-ppt-negative-"));
  const generator = path.join(repoRoot, "scripts/generate-release-sbom.py");
  const sourceLock = path.join(
    repoRoot,
    "backend/app/adapters/skill/assets/ppt_ib_deck/package-lock.json",
  );
  const python = process.platform === "win32" ? "python.exe" : "python3";
  const probe = [
    "import importlib.util, sys",
    "from pathlib import Path",
    "spec = importlib.util.spec_from_file_location('sbom', sys.argv[1])",
    "module = importlib.util.module_from_spec(spec)",
    "spec.loader.exec_module(module)",
    "module.PPT_RUNTIME_NPM_LOCK = Path(sys.argv[2])",
    "module.npm_components()",
  ].join("; ");
  try {
    const missing = spawnSync(
      python,
      ["-c", probe, generator, path.join(root, "missing-package-lock.json")],
      { encoding: "utf8" },
    );
    assert.notEqual(missing.status, 0);
    assert.match(missing.stderr, /No such file|cannot find/i);

    const tamperedPath = path.join(root, "package-lock.json");
    const tampered = JSON.parse(readFileSync(sourceLock, "utf8"));
    delete tampered.packages["node_modules/pptxgenjs"];
    writeFileSync(tamperedPath, JSON.stringify(tampered));
    const critical = spawnSync(python, ["-c", probe, generator, tamperedPath], {
      encoding: "utf8",
    });
    assert.notEqual(critical.status, 0);
    assert.match(critical.stderr, /missing from lock: pptxgenjs/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("Electron identity IPC binds every issued session to the credential backend origin", () => {
  const main = readFileSync(
    path.join(desktopRoot, "electron", "main.cjs"),
    "utf8",
  );
  assert.match(
    main,
    /backend_origin:\s*credentialVault\(\)\.backendOrigin/g,
  );
  assert.match(
    main,
    /backendBoundJsonFetch\(\{[\s\S]*backendOrigin: vault\.backendOrigin,[\s\S]*pathname,/,
  );
  assert.doesNotMatch(main, /fetch\(new URL\(pathname/);
});

test("generated TV installer always preserves current app state and only removes an explicit legacy package", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-tv-contract-"));
  const releaseDir = path.join(root, "release");
  const fakeBin = path.join(root, "bin");
  const adbLog = path.join(root, "adb.log");
  const version = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  ).version;
  const packageScript = readFileSync(
    path.join(desktopRoot, "scripts/package-tv-installer.cjs"),
    "utf8",
  );
  assert.match(packageScript, /process\.platform !== "win32"/);
  assert.match(packageScript, /Compress-Archive/);
  assert.match(packageScript, /run\("powershell\.exe"/);
  mkdirSync(releaseDir, { recursive: true });
  mkdirSync(fakeBin, { recursive: true });
  writeFileSync(
    path.join(releaseDir, `EchoDesk-${version}-android-tv.apk`),
    "apk",
  );
  const fakeAdb = path.join(fakeBin, "adb");
  writeFileSync(
    fakeAdb,
    `#!/bin/sh\nprintf '%s\\n' "$*" >> "$ADB_LOG"\nif [ "$1" = devices ]; then printf 'List of devices attached\\n192.168.1.2:5555\\tdevice\\n'; fi\nexit 0\n`,
  );
  chmodSync(fakeAdb, 0o755);
  try {
    const packaged = spawnSync("node", ["scripts/package-tv-installer.cjs"], {
      cwd: desktopRoot,
      env: { ...process.env, ECHODESK_RELEASE_DIR: releaseDir },
      encoding: "utf8",
    });
    assert.equal(packaged.status, 0, `${packaged.stdout}\n${packaged.stderr}`);
    const bundleZip = path.join(
      releaseDir,
      `EchoDesk-${version}-smart-tv-oneclick.zip`,
    );
    assert.equal(existsSync(bundleZip), true);
    const extracted = path.join(root, "extracted-bundle");
    mkdirSync(extracted, { recursive: true });
    const extraction =
      process.platform === "win32"
        ? spawnSync(
            "powershell.exe",
            [
              "-NoLogo",
              "-NoProfile",
              "-NonInteractive",
              "-Command",
              `$ErrorActionPreference = 'Stop'; Expand-Archive -LiteralPath '${bundleZip.replaceAll("'", "''")}' -DestinationPath '${extracted.replaceAll("'", "''")}' -Force`,
            ],
            { encoding: "utf8" },
          )
        : spawnSync("unzip", ["-q", bundleZip, "-d", extracted], {
            encoding: "utf8",
          });
    assert.equal(
      extraction.status,
      0,
      `${extraction.stdout}\n${extraction.stderr}`,
    );
    for (const required of [
      `EchoDesk-${version}-smart-tv.apk`,
      "install-tv-macos.sh",
      "install-tv-windows.ps1",
      "README-TV-INSTALL.txt",
      "MANIFEST.sha256",
    ]) {
      assert.equal(existsSync(path.join(extracted, required)), true, required);
    }
    const bundle = path.join(
      releaseDir,
      `EchoDesk-${version}-smart-tv-oneclick`,
    );
    const installer = path.join(bundle, "install-tv-macos.sh");
    const windows = readFileSync(
      path.join(bundle, "install-tv-windows.ps1"),
      "utf8",
    );
    const readme = readFileSync(
      path.join(bundle, "README-TV-INSTALL.txt"),
      "utf8",
    );
    const manifest = readFileSync(
      path.join(bundle, "MANIFEST.sha256"),
      "utf8",
    );
    const manifestNames = manifest
      .trim()
      .split("\n")
      .map((line) => line.trim().split(/\s+/).at(-1));
    assert.deepEqual(manifestNames, [
      `EchoDesk-${version}-smart-tv.apk`,
      "install-tv-macos.sh",
      "install-tv-windows.ps1",
      "README-TV-INSTALL.txt",
    ]);
    const baseEnv = {
      ...process.env,
      PATH: `${fakeBin}${path.delimiter}${process.env.PATH}`,
      ADB_LOG: adbLog,
    };

    const runInstaller = (extraEnv = {}) => {
      writeFileSync(adbLog, "");
      const result = spawnSync("bash", [installer, "192.168.1.2"], {
        env: { ...baseEnv, ...extraEnv },
        encoding: "utf8",
      });
      assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
      return readFileSync(adbLog, "utf8");
    };

    const defaultLog = runInstaller();
    assert.doesNotMatch(defaultLog, /pm clear|pm uninstall/);
    const ignoredResetLog = runInstaller({
      ECHODESK_TV_RESET_DATA: "1",
      ECHODESK_TV_KEEP_DATA: "0",
    });
    assert.doesNotMatch(ignoredResetLog, /pm clear com\.echodesk\.tv/);
    const removeLog = runInstaller({ ECHODESK_TV_REMOVE_LEGACY: "1" });
    assert.match(removeLog, /shell pm clear com\.echodesk\.app/);
    assert.match(removeLog, /shell pm uninstall com\.echodesk\.app/);
    assert.doesNotMatch(
      windows,
      /ECHODESK_TV_RESET_DATA|ECHODESK_TV_KEEP_DATA/,
    );
    assert.doesNotMatch(windows, /pm clear \$Pkg/);
    assert.match(windows, /ECHODESK_TV_REMOVE_LEGACY/);
    assert.match(readme, /默认保留当前 WebView、app data 和设备身份/);
    assert.match(readme, /不提供清空当前应用数据的能力/);
    assert.doesNotMatch(readme, /默认清理/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
