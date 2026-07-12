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

const {
  normalizeFingerprint,
  releaseSigningContract,
} = require("../../scripts/android-build-common.cjs");

const desktopRoot = path.resolve(__dirname, "../..");
const repoRoot = path.resolve(desktopRoot, "..");

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
  assert.doesNotMatch(gradle, /signingConfigs\s*\{/);
  assert.match(rootGradle, /kotlin-stdlib/);
  assert.match(rootGradle, /details\.useVersion '1\.8\.22'/);
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
  const playwright = readFileSync(
    path.join(desktopRoot, "playwright.config.ts"),
    "utf8",
  );

  assert.match(ci, /android-tv:\n\s+name: Android \+ TV/);
  assert.match(ci, /npm run app:build:android:development/);
  assert.match(ci, /:app:connectedDebugAndroidTest/);
  assert.match(
    ci,
    /assembleRelease unexpectedly accepted missing stable signing inputs/,
  );
  assert.match(ci, /needs: \[[^\]]*android-tv/);
  assert.match(ci, /export ECHODESK_NODE_RUNTIME="\$\(command -v node\)"/);
  assert.match(ci, /ECHODESK_NODE_RUNTIME_IS_ELECTRON=true/);
  assert.match(ci, /name: echodesk-macos-arm64-adhoc-test/);
  assert.doesNotMatch(ci, /name: echodesk-macos-arm64-release/);
  assert.match(release, /missing required release secret/);
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
  assert.doesNotMatch(release, /GITHUB_ENV/);
  assert.doesNotMatch(release, /path:[^\n]*(?:\.jks|\.keystore)/);
  assert.match(release, /npm run app:dist:android/);
  assert.match(live, /runs-on: ubuntu-latest/);
  assert.match(
    live,
    /runs-on: \[self-hosted, linux, x64, echodesk-private-models\]/,
  );
  assert.match(live, /run_public_model:/);
  assert.match(live, /if: \$\{\{ inputs\.run_public_model == true \}\}/);
  assert.doesNotMatch(live, /\bschedule:/);
  assert.match(live, /tests\/integration\/test_product_model_live\.py/);
  assert.match(live, /private route unavailable/);
  assert.doesNotMatch(live, /pytest tests -m live/);
  assert.match(windows, /name: echodesk-windows-unsigned-test/);
  assert.doesNotMatch(windows, /name: echodesk-windows-release/);
  assert.match(windows, /Refuse unsigned public publishing/);
  assert.doesNotMatch(windows, /gh release upload/);
  assert.match(playwright, /retries: 0/);
  assert.doesNotMatch(playwright, /retries: process\.env\.CI/);
});

test("generated TV installer always preserves current app state and only removes an explicit legacy package", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-tv-contract-"));
  const releaseDir = path.join(root, "release");
  const fakeBin = path.join(root, "bin");
  const adbLog = path.join(root, "adb.log");
  const version = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  ).version;
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
    const baseEnv = {
      ...process.env,
      PATH: `${fakeBin}:${process.env.PATH}`,
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
