#!/usr/bin/env node

const {
  chmodSync,
  copyFileSync,
  mkdirSync,
  readFileSync,
  rmSync,
} = require("node:fs");
const { createHash, randomBytes } = require("node:crypto");
const { join } = require("node:path");

const {
  ANDROID_DIR,
  RELEASE_DIR,
  ROOT,
  androidEnvironment,
  run,
} = require("./android-build-common.cjs");

const PREVIEW_VERSION = "0.3.3-preview.2";
const PREVIEW_VERSION_CODE = "30302";
const PREVIEW_ALIAS = "echodesk-preview";
const RELEASE_APK = join(
  ANDROID_DIR,
  "app",
  "build",
  "outputs",
  "apk",
  "release",
  "app-release.apk",
);
const OUTPUT_APK = join(
  RELEASE_DIR,
  `EchoDesk-${PREVIEW_VERSION}-android-universal-PREVIEW.apk`,
);
const SIGNING_DIR = join(ANDROID_DIR, ".preview-signing");
const PREVIEW_KEYSTORE = join(SIGNING_DIR, "echodesk-preview.p12");

function sha256(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function main() {
  const baseEnv = androidEnvironment();
  const keytool = join(baseEnv.JAVA_HOME, "bin", "keytool");
  const password = randomBytes(24).toString("base64url");
  const env = {
    ...baseEnv,
    VITE_ECHODESK_RUNTIME_MODE: "release",
    VITE_ECHODESK_PRINCIPAL_MODE: "public",
    ECHODESK_ANDROID_PREVIEW_KEYSTORE: PREVIEW_KEYSTORE,
    ECHODESK_ANDROID_PREVIEW_KEY_ALIAS: PREVIEW_ALIAS,
    ECHODESK_ANDROID_PREVIEW_KEYSTORE_PASSWORD: password,
    ECHODESK_ANDROID_PREVIEW_KEY_PASSWORD: password,
  };

  mkdirSync(RELEASE_DIR, { recursive: true });
  rmSync(OUTPUT_APK, { force: true });
  rmSync(SIGNING_DIR, { recursive: true, force: true });
  mkdirSync(SIGNING_DIR, { recursive: true, mode: 0o700 });

  console.log(
    "[android-preview] PREVIEW SIDELOAD ONLY: non-debuggable remote-mobile release/public runtime; never publish to Play Store",
  );
  try {
    run(
      keytool,
      [
        "-genkeypair",
        "-keystore",
        PREVIEW_KEYSTORE,
        "-storetype",
        "PKCS12",
        "-storepass",
        password,
        "-keypass",
        password,
        "-alias",
        PREVIEW_ALIAS,
        "-keyalg",
        "RSA",
        "-keysize",
        "3072",
        "-validity",
        "3650",
        "-dname",
        "CN=EchoDesk Android Preview,OU=Preview Sideload,O=EchoDesk,C=GB",
      ],
      { env },
    );
    chmodSync(PREVIEW_KEYSTORE, 0o600);

    run("npm", ["run", "build"], { env });
    run("npx", ["cap", "sync", "android"], { env });
    run(
      process.execPath,
      [join(ROOT, "scripts", "prepare-android-gradle-locks.cjs")],
      { env },
    );
    run(
      "./gradlew",
      [
        "clean",
        "assembleRelease",
        "-PechoApplicationId=com.echodesk.app",
        "-PechoPreviewSigning=true",
        `-PechoPreviewVersionName=${PREVIEW_VERSION}`,
        `-PechoPreviewVersionCode=${PREVIEW_VERSION_CODE}`,
      ],
      { cwd: ANDROID_DIR, env },
    );
    copyFileSync(RELEASE_APK, OUTPUT_APK);

    const aapt = join(env.ANDROID_HOME, "build-tools", "36.1.0", "aapt");
    const apksigner = join(
      env.ANDROID_HOME,
      "build-tools",
      "36.1.0",
      "apksigner",
    );
    const badging = run(aapt, ["dump", "badging", OUTPUT_APK], {
      env,
      capture: true,
    });
    if (/^application-debuggable$/m.test(badging)) {
      throw new Error("Preview APK must not be debuggable");
    }
    const signing = run(
      apksigner,
      ["verify", "--verbose", "--print-certs", OUTPUT_APK],
      { env, capture: true },
    );
    console.log(`[android-preview] APK: ${OUTPUT_APK}`);
    console.log(`[android-preview] SHA-256: ${sha256(OUTPUT_APK)}`);
    console.log(
      `[android-preview] runtime: VITE_ECHODESK_RUNTIME_MODE=${env.VITE_ECHODESK_RUNTIME_MODE} VITE_ECHODESK_PRINCIPAL_MODE=${env.VITE_ECHODESK_PRINCIPAL_MODE}`,
    );
    console.log(`[android-preview] aapt:\n${badging.trim()}`);
    console.log(`[android-preview] apksigner:\n${signing.trim()}`);
  } finally {
    rmSync(SIGNING_DIR, { recursive: true, force: true });
  }
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`[android-preview] ${error?.message || error}`);
    process.exit(1);
  }
}

module.exports = { main };
