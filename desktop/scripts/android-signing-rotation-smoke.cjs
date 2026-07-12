#!/usr/bin/env node

const { randomBytes } = require("node:crypto");
const { existsSync, mkdtempSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { basename, join } = require("node:path");
const { spawnSync } = require("node:child_process");

const {
  androidEnvironment,
  apksignerSignerArgs,
  normalizeFingerprint,
  readKeystoreFingerprint,
  releaseSigningContract,
  resolveBuildTool,
  run,
  verifySigningIdentities,
} = require("./android-build-common.cjs");
const {
  createSigningLineage,
  verifyReleaseApk,
} = require("./build-android-release.cjs");

function runAllowFailure(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd,
    env: options.env || process.env,
    encoding: "utf8",
    shell: false,
  });
  return {
    status: result.status ?? 1,
    output: `${result.stdout || ""}${result.stderr || ""}`,
    error: result.error,
  };
}

function requireInput(env, name) {
  const value = String(env[name] || "").trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function adbCommand(adb, serial, args, env, allowFailure = false) {
  if (allowFailure)
    return runAllowFailure(adb, ["-s", serial, ...args], { env });
  return run(adb, ["-s", serial, ...args], { env, capture: true });
}

function resolveSmokeDevice(adb, env) {
  const configured = String(
    env.ECHODESK_ANDROID_ROTATION_SMOKE_SERIAL || "",
  ).trim();
  const devices = run(adb, ["devices"], { env, capture: true })
    .split(/\r?\n/)
    .slice(1)
    .map((line) => line.trim().split(/\s+/))
    .filter(([serial, state]) => serial && state === "device")
    .map(([serial]) => serial);
  const serial = configured || (devices.length === 1 ? devices[0] : "");
  if (!serial || !devices.includes(serial)) {
    throw new Error(
      `rotation smoke needs one connected device; available=${devices.join(",") || "none"}`,
    );
  }
  if (
    !serial.startsWith("emulator-") &&
    env.ECHODESK_ANDROID_ROTATION_SMOKE_ALLOW_PHYSICAL !== "1"
  ) {
    throw new Error(
      "rotation smoke refuses a physical device unless ECHODESK_ANDROID_ROTATION_SMOKE_ALLOW_PHYSICAL=1",
    );
  }
  return serial;
}

function packageState(adb, serial, applicationId, env) {
  const packages = adbCommand(
    adb,
    serial,
    ["shell", "cmd", "package", "list", "packages", "-U", applicationId],
    env,
  );
  const uidMatch = packages.match(
    new RegExp(`package:${applicationId}\\s+uid:(\\d+)`),
  );
  const dump = adbCommand(
    adb,
    serial,
    ["shell", "dumpsys", "package", applicationId],
    env,
  );
  const firstInstallMatch = dump.match(/firstInstallTime=([^\r\n]+)/);
  if (!uidMatch || !firstInstallMatch) {
    throw new Error(`unable to read installed state for ${applicationId}`);
  }
  return {
    uid: Number.parseInt(uidMatch[1], 10),
    firstInstallTime: firstInstallMatch[1].trim(),
    microphoneGranted: /android\.permission\.RECORD_AUDIO:\s+granted=true/.test(
      dump,
    ),
    hasPastSignatures: hasNonEmptyPastSignatures(dump),
  };
}

function hasNonEmptyPastSignatures(packageDump) {
  const match = String(packageDump).match(/past signatures:\s*\[([\s\S]*?)\]/i);
  return Boolean(match?.[1]?.trim());
}

function main() {
  const baseEnv = androidEnvironment();
  const historicalApk = requireInput(
    baseEnv,
    "ECHODESK_ANDROID_HISTORICAL_APK",
  );
  if (!existsSync(historicalApk)) {
    throw new Error(`historical APK does not exist: ${historicalApk}`);
  }
  for (const name of [
    "ECHODESK_ANDROID_LEGACY_KEYSTORE",
    "ECHODESK_ANDROID_LEGACY_KEY_ALIAS",
    "ECHODESK_ANDROID_LEGACY_KEYSTORE_PASSWORD",
    "ECHODESK_ANDROID_LEGACY_KEY_PASSWORD",
    "ECHODESK_ANDROID_EXPECTED_LEGACY_CERT_SHA256",
  ]) {
    requireInput(baseEnv, name);
  }

  const apksigner = resolveBuildTool(baseEnv.ANDROID_HOME, "apksigner");
  const aapt = resolveBuildTool(baseEnv.ANDROID_HOME, "aapt");
  const adb = join(
    baseEnv.ANDROID_HOME,
    "platform-tools",
    process.platform === "win32" ? "adb.exe" : "adb",
  );
  if (!existsSync(adb)) throw new Error(`adb does not exist: ${adb}`);
  const serial = resolveSmokeDevice(adb, baseEnv);
  const badging = run(aapt, ["dump", "badging", historicalApk], {
    env: baseEnv,
    capture: true,
  });
  const packageMatch = badging.match(/package: name='([^']+)'/);
  const applicationId = packageMatch?.[1] || "";
  if (!["com.echodesk.app", "com.echodesk.tv"].includes(applicationId)) {
    throw new Error(
      `unexpected historical applicationId: ${applicationId || "missing"}`,
    );
  }

  const legacyExpected = normalizeFingerprint(
    baseEnv.ECHODESK_ANDROID_EXPECTED_LEGACY_CERT_SHA256,
  );
  const historicalVerification = run(
    apksigner,
    ["verify", "--print-certs", historicalApk],
    { env: baseEnv, capture: true },
  );
  const historicalMatch = historicalVerification.match(
    /certificate SHA-256 digest:\s*([0-9a-f:]+)/i,
  );
  if (
    !historicalMatch ||
    normalizeFingerprint(historicalMatch[1]) !== legacyExpected
  ) {
    throw new Error(
      "historical APK certificate does not match the pinned legacy signer",
    );
  }

  const root = mkdtempSync(join(tmpdir(), "echodesk-android-rotation-smoke-"));
  const generatedPassword = randomBytes(24).toString("base64url");
  const currentKeystore = join(root, "temporary-current-release.jks");
  const lineage = join(root, "lineage.bin");
  const newOnlyApk = join(root, "new-key-without-lineage.apk");
  const rotatedApk = join(root, "rotated-v3.1.apk");
  const keytool = join(
    baseEnv.JAVA_HOME,
    "bin",
    process.platform === "win32" ? "keytool.exe" : "keytool",
  );
  const env = {
    ...baseEnv,
    ECHODESK_ANDROID_CURRENT_KEYSTORE: currentKeystore,
    ECHODESK_ANDROID_CURRENT_KEY_ALIAS: "echodesk-rotation-smoke",
    ECHODESK_ANDROID_CURRENT_KEYSTORE_PASSWORD: generatedPassword,
    ECHODESK_ANDROID_CURRENT_KEY_PASSWORD: generatedPassword,
    ECHODESK_ANDROID_ROTATION_SMOKE_CURRENT_PASSWORD: generatedPassword,
    ECHODESK_ANDROID_ROTATION_MIN_SDK_VERSION: "33",
  };
  let installedBySmoke = false;
  try {
    run(
      keytool,
      [
        "-genkeypair",
        "-noprompt",
        "-keystore",
        currentKeystore,
        "-storepass:env",
        "ECHODESK_ANDROID_ROTATION_SMOKE_CURRENT_PASSWORD",
        "-keypass:env",
        "ECHODESK_ANDROID_ROTATION_SMOKE_CURRENT_PASSWORD",
        "-alias",
        env.ECHODESK_ANDROID_CURRENT_KEY_ALIAS,
        "-keyalg",
        "RSA",
        "-keysize",
        "3072",
        "-validity",
        "10000",
        "-dname",
        "CN=EchoDesk Rotation Smoke,O=EchoDesk,C=CN",
      ],
      { env },
    );
    const temporaryCurrent = {
      keystore: currentKeystore,
      alias: env.ECHODESK_ANDROID_CURRENT_KEY_ALIAS,
      keystorePasswordEnv: "ECHODESK_ANDROID_CURRENT_KEYSTORE_PASSWORD",
      keyPasswordEnv: "ECHODESK_ANDROID_CURRENT_KEY_PASSWORD",
    };
    env.ECHODESK_ANDROID_EXPECTED_CURRENT_CERT_SHA256 = readKeystoreFingerprint(
      temporaryCurrent,
      env,
    );
    const signing = releaseSigningContract(env);
    verifySigningIdentities(signing, env);
    createSigningLineage(signing, lineage, env);

    run(
      apksigner,
      [
        "sign",
        "--v4-signing-enabled",
        "false",
        "--out",
        newOnlyApk,
        ...apksignerSignerArgs(signing.current),
        historicalApk,
      ],
      { env },
    );
    run(
      apksigner,
      [
        "sign",
        "--v4-signing-enabled",
        "false",
        "--out",
        rotatedApk,
        "--lineage",
        lineage,
        "--rotation-min-sdk-version",
        "33",
        ...apksignerSignerArgs(signing.legacy),
        "--next-signer",
        ...apksignerSignerArgs(signing.current),
        historicalApk,
      ],
      { env },
    );
    verifyReleaseApk(rotatedApk, applicationId, signing, env);

    const existing = adbCommand(
      adb,
      serial,
      ["shell", "pm", "path", applicationId],
      env,
      true,
    );
    if (
      existing.status === 0 &&
      existing.output.includes("package:") &&
      env.ECHODESK_ANDROID_ROTATION_SMOKE_ALLOW_REPLACE !== "1"
    ) {
      throw new Error(
        `${applicationId} is already installed; set ECHODESK_ANDROID_ROTATION_SMOKE_ALLOW_REPLACE=1 only on a disposable device`,
      );
    }
    adbCommand(adb, serial, ["uninstall", applicationId], env, true);
    adbCommand(adb, serial, ["install", historicalApk], env);
    installedBySmoke = true;
    adbCommand(
      adb,
      serial,
      [
        "shell",
        "pm",
        "grant",
        applicationId,
        "android.permission.RECORD_AUDIO",
      ],
      env,
    );
    const before = packageState(adb, serial, applicationId, env);

    const incompatible = adbCommand(
      adb,
      serial,
      ["install", "-r", newOnlyApk],
      env,
      true,
    );
    if (
      incompatible.status === 0 ||
      !incompatible.output.includes("INSTALL_FAILED_UPDATE_INCOMPATIBLE")
    ) {
      throw new Error(
        "new signer without lineage did not fail with signature incompatibility",
      );
    }

    adbCommand(adb, serial, ["install", "-r", rotatedApk], env);
    const after = packageState(adb, serial, applicationId, env);
    if (
      after.uid !== before.uid ||
      after.firstInstallTime !== before.firstInstallTime ||
      !after.microphoneGranted ||
      !after.hasPastSignatures
    ) {
      throw new Error(
        `rotation did not preserve installed state: before=${JSON.stringify(before)} after=${JSON.stringify(after)}`,
      );
    }
    console.log(
      JSON.stringify(
        {
          status: "passed",
          device: serial,
          applicationId,
          historicalApk: basename(historicalApk),
          incompatibleWithoutLineage: true,
          rotationMinSdkVersion: 33,
          uidPreserved: true,
          firstInstallTimePreserved: true,
          microphoneGrantPreserved: true,
          pastSignaturesPresent: true,
        },
        null,
        2,
      ),
    );
  } finally {
    if (
      installedBySmoke &&
      env.ECHODESK_ANDROID_ROTATION_SMOKE_KEEP_APP !== "1"
    ) {
      adbCommand(adb, serial, ["uninstall", applicationId], env, true);
    }
    rmSync(root, { recursive: true, force: true });
  }
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`[android-rotation-smoke] ${error?.message || error}`);
    process.exit(1);
  }
}

module.exports = {
  hasNonEmptyPastSignatures,
  main,
  packageState,
  resolveSmokeDevice,
};
