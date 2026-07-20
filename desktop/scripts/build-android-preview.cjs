#!/usr/bin/env node

const {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
} = require("node:fs");
const { createHash } = require("node:crypto");
const { isAbsolute, join, relative, resolve } = require("node:path");

const {
  ANDROID_DIR,
  RELEASE_DIR,
  ROOT,
  androidEnvironment,
  run,
} = require("./android-build-common.cjs");

const PREVIEW_VERSION = "0.3.4";
const PREVIEW_VERSION_CODE = "30401";
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
  `EchoDesk-${PREVIEW_VERSION}-android-universal.apk`,
);
function sha256(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function previewSigningContract(environment = process.env) {
  const required = [
    "ECHODESK_ANDROID_PREVIEW_KEYSTORE",
    "ECHODESK_ANDROID_PREVIEW_KEY_ALIAS",
    "ECHODESK_ANDROID_PREVIEW_KEYSTORE_PASSWORD",
    "ECHODESK_ANDROID_PREVIEW_KEY_PASSWORD",
    "ECHODESK_ANDROID_PREVIEW_EXPECTED_CERT_SHA256",
  ];
  const missing = required.filter((name) => !String(environment[name] || "").trim());
  if (missing.length) {
    throw new Error(
      "stable Preview signing requires CI/env inputs; because preview.2 used a random " +
      `certificate, users must uninstall it once before installing the first stable-signed build: ${missing.join(", ")}`,
    );
  }
  const keystore = resolve(String(environment.ECHODESK_ANDROID_PREVIEW_KEYSTORE));
  if (!isAbsolute(keystore) || !existsSync(keystore)) {
    throw new Error("stable Preview keystore does not exist");
  }
  const relativeToRepo = relative(ROOT, keystore);
  if (relativeToRepo === "" || (!relativeToRepo.startsWith("..") && !isAbsolute(relativeToRepo))) {
    throw new Error("stable Preview keystore must remain outside the repository");
  }
  const expectedFingerprint = String(
    environment.ECHODESK_ANDROID_PREVIEW_EXPECTED_CERT_SHA256,
  ).replace(/[^0-9a-f]/gi, "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(expectedFingerprint)) {
    throw new Error("stable Preview certificate fingerprint must contain 64 hex digits");
  }
  return { keystore, expectedFingerprint };
}

function main() {
  const baseEnv = androidEnvironment();
  const previewSigning = previewSigningContract(process.env);
  const env = {
    ...baseEnv,
    VITE_ECHODESK_RUNTIME_MODE: "release",
    VITE_ECHODESK_PRINCIPAL_MODE: "public",
    VITE_ECHODESK_UPDATE_VERSION: PREVIEW_VERSION,
    ECHODESK_ANDROID_PREVIEW_KEYSTORE: previewSigning.keystore,
  };

  mkdirSync(RELEASE_DIR, { recursive: true });
  rmSync(OUTPUT_APK, { force: true });

  console.log(
    "[android-preview] STABLE PREVIEW SIGNER: preview.2 random-signed installs require one uninstall; subsequent builds update in place",
  );
  console.log(
    "[android-preview] PREVIEW SIDELOAD ONLY: non-debuggable remote-mobile release/public runtime; never publish to Play Store",
  );
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
    const signingReport = run(
      apksigner,
      ["verify", "--verbose", "--print-certs", OUTPUT_APK],
      { env, capture: true },
    );
    const fingerprintMatch = signingReport.match(
      /Signer #1 certificate SHA-256 digest:\s*([0-9a-f]+)/i,
    );
    if (
      !fingerprintMatch ||
      fingerprintMatch[1].toLowerCase() !==
        previewSigning.expectedFingerprint
    ) {
      throw new Error("stable Preview certificate fingerprint mismatch");
    }
    console.log(`[android-preview] APK: ${OUTPUT_APK}`);
    console.log(`[android-preview] SHA-256: ${sha256(OUTPUT_APK)}`);
    console.log(
      `[android-preview] runtime: VITE_ECHODESK_RUNTIME_MODE=${env.VITE_ECHODESK_RUNTIME_MODE} VITE_ECHODESK_PRINCIPAL_MODE=${env.VITE_ECHODESK_PRINCIPAL_MODE}`,
    );
    console.log(`[android-preview] aapt:\n${badging.trim()}`);
    console.log(`[android-preview] apksigner:\n${signingReport.trim()}`);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`[android-preview] ${error?.message || error}`);
    process.exit(1);
  }
}

module.exports = { main, previewSigningContract };
