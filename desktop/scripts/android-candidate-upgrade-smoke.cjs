#!/usr/bin/env node

const { createHash } = require("node:crypto");
const { existsSync, readFileSync } = require("node:fs");
const { basename, join } = require("node:path");

const {
  androidEnvironment,
  normalizeFingerprint,
  resolveBuildTool,
  run,
} = require("./android-build-common.cjs");
const { verifyReleaseApk } = require("./build-android-release.cjs");
const {
  packageState,
  resolveSmokeDevice,
} = require("./android-signing-rotation-smoke.cjs");

function requireInput(env, name) {
  const value = String(env[name] || "").trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function sha256File(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function metadata(aapt, apkPath, env) {
  const output = run(aapt, ["dump", "badging", apkPath], {
    env,
    capture: true,
  });
  const packageMatch = output.match(
    /package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'/,
  );
  const activityMatch = output.match(/launchable-activity: name='([^']+)'/);
  if (!packageMatch || !activityMatch) {
    throw new Error(`unable to read package metadata from ${apkPath}`);
  }
  return {
    applicationId: packageMatch[1],
    versionCode: Number.parseInt(packageMatch[2], 10),
    versionName: packageMatch[3],
    launchableActivity: activityMatch[1],
  };
}

function assertMetadata(label, actual, expected) {
  for (const field of ["applicationId", "versionCode", "versionName"]) {
    if (actual[field] !== expected[field]) {
      throw new Error(
        `${label} ${field}=${actual[field]} expected=${expected[field]}`,
      );
    }
  }
}

function adb(adbPath, serial, args, env, allowFailure = false) {
  if (!allowFailure) {
    return run(adbPath, ["-s", serial, ...args], { env, capture: true });
  }
  const { spawnSync } = require("node:child_process");
  const result = spawnSync(adbPath, ["-s", serial, ...args], {
    env,
    encoding: "utf8",
    shell: false,
  });
  return {
    status: result.status ?? 1,
    output: `${result.stdout || ""}${result.stderr || ""}`,
  };
}

function main() {
  const env = androidEnvironment();
  const historicalApk = requireInput(env, "ECHODESK_ANDROID_HISTORICAL_APK");
  const candidateApk = requireInput(env, "ECHODESK_ANDROID_CANDIDATE_APK");
  const expectedApplicationId = requireInput(
    env,
    "ECHODESK_ANDROID_EXPECTED_APPLICATION_ID",
  );
  const expectedHistoricalSha256 = requireInput(
    env,
    "ECHODESK_ANDROID_EXPECTED_HISTORICAL_SHA256",
  ).toLowerCase();
  const expectedHistoricalVersion = requireInput(
    env,
    "ECHODESK_ANDROID_EXPECTED_HISTORICAL_VERSION",
  );
  const expectedHistoricalCode = Number.parseInt(
    requireInput(env, "ECHODESK_ANDROID_EXPECTED_HISTORICAL_VERSION_CODE"),
    10,
  );
  const expectedCandidateVersion = requireInput(
    env,
    "ECHODESK_ANDROID_EXPECTED_CANDIDATE_VERSION",
  );
  const expectedCandidateCode = Number.parseInt(
    requireInput(env, "ECHODESK_ANDROID_EXPECTED_CANDIDATE_VERSION_CODE"),
    10,
  );
  if (!existsSync(historicalApk) || !existsSync(candidateApk)) {
    throw new Error("historical and candidate APKs must both exist");
  }
  if (!/^[0-9a-f]{64}$/.test(expectedHistoricalSha256)) {
    throw new Error(
      "expected historical SHA-256 must contain 64 lowercase hex digits",
    );
  }
  if (
    !Number.isSafeInteger(expectedHistoricalCode) ||
    !Number.isSafeInteger(expectedCandidateCode) ||
    expectedCandidateCode <= expectedHistoricalCode
  ) {
    throw new Error(
      "candidate versionCode must be an integer above the historical release",
    );
  }

  const historicalSha256 = sha256File(historicalApk);
  if (historicalSha256 !== expectedHistoricalSha256) {
    throw new Error(
      `historical APK digest ${historicalSha256} != pinned ${expectedHistoricalSha256}`,
    );
  }
  const candidateSha256 = sha256File(candidateApk);
  const aapt = resolveBuildTool(env.ANDROID_HOME, "aapt");
  const apksigner = resolveBuildTool(env.ANDROID_HOME, "apksigner");
  const historicalMetadata = metadata(aapt, historicalApk, env);
  const candidateMetadata = metadata(aapt, candidateApk, env);
  assertMetadata("historical APK", historicalMetadata, {
    applicationId: expectedApplicationId,
    versionCode: expectedHistoricalCode,
    versionName: expectedHistoricalVersion,
  });
  assertMetadata("candidate APK", candidateMetadata, {
    applicationId: expectedApplicationId,
    versionCode: expectedCandidateCode,
    versionName: expectedCandidateVersion,
  });

  const rotationMinSdkVersion = Number.parseInt(
    requireInput(env, "ECHODESK_ANDROID_ROTATION_MIN_SDK_VERSION"),
    10,
  );
  if (rotationMinSdkVersion !== 33) {
    throw new Error(
      "candidate verification requires the published API 33 rotation boundary",
    );
  }
  const signing = {
    rotationMinSdkVersion,
    legacy: {
      expectedFingerprint: normalizeFingerprint(
        requireInput(env, "ECHODESK_ANDROID_EXPECTED_LEGACY_CERT_SHA256"),
      ),
    },
    current: {
      expectedFingerprint: normalizeFingerprint(
        requireInput(env, "ECHODESK_ANDROID_EXPECTED_CURRENT_CERT_SHA256"),
      ),
    },
  };
  const historicalVerification = run(
    apksigner,
    ["verify", "--print-certs", historicalApk],
    { env, capture: true },
  );
  const historicalCertificate = historicalVerification.match(
    /certificate SHA-256 digest:\s*([0-9a-f:]+)/i,
  );
  if (
    !historicalCertificate ||
    normalizeFingerprint(historicalCertificate[1]) !==
      signing.legacy.expectedFingerprint
  ) {
    throw new Error(
      "historical APK does not match the pinned legacy certificate",
    );
  }
  verifyReleaseApk(candidateApk, expectedApplicationId, signing, env);

  const adbPath = join(
    env.ANDROID_HOME,
    "platform-tools",
    process.platform === "win32" ? "adb.exe" : "adb",
  );
  if (!existsSync(adbPath)) throw new Error(`adb does not exist: ${adbPath}`);
  const serial = resolveSmokeDevice(adbPath, env);
  const installed = adb(
    adbPath,
    serial,
    ["shell", "pm", "path", expectedApplicationId],
    env,
    true,
  );
  if (
    installed.status === 0 &&
    installed.output.includes("package:") &&
    env.ECHODESK_ANDROID_ROTATION_SMOKE_ALLOW_REPLACE !== "1"
  ) {
    throw new Error(
      `${expectedApplicationId} is already installed on ${serial}; use a disposable emulator`,
    );
  }

  let installedBySmoke = false;
  try {
    adb(adbPath, serial, ["uninstall", expectedApplicationId], env, true);
    adb(adbPath, serial, ["install", historicalApk], env);
    installedBySmoke = true;
    adb(
      adbPath,
      serial,
      [
        "shell",
        "pm",
        "grant",
        expectedApplicationId,
        "android.permission.RECORD_AUDIO",
      ],
      env,
    );
    const before = packageState(adbPath, serial, expectedApplicationId, env);

    adb(adbPath, serial, ["install", "-r", candidateApk], env);
    const after = packageState(adbPath, serial, expectedApplicationId, env);
    if (
      after.uid !== before.uid ||
      after.firstInstallTime !== before.firstInstallTime ||
      !after.microphoneGranted ||
      !after.hasPastSignatures
    ) {
      throw new Error(
        `candidate upgrade did not preserve identity/data/permission state: before=${JSON.stringify(before)} after=${JSON.stringify(after)}`,
      );
    }
    const launch = adb(
      adbPath,
      serial,
      [
        "shell",
        "am",
        "start",
        "-W",
        "-n",
        `${expectedApplicationId}/${candidateMetadata.launchableActivity}`,
      ],
      env,
    );
    if (!/(?:Status:\s*ok|Complete)/i.test(launch)) {
      throw new Error(`candidate did not launch successfully: ${launch}`);
    }
    if (sha256File(candidateApk) !== candidateSha256) {
      throw new Error("candidate APK changed during upgrade verification");
    }
    console.log(
      JSON.stringify(
        {
          status: "passed",
          device: serial,
          applicationId: expectedApplicationId,
          historical: {
            file: basename(historicalApk),
            sha256: historicalSha256,
            version: expectedHistoricalVersion,
            versionCode: expectedHistoricalCode,
          },
          candidate: {
            file: basename(candidateApk),
            sha256: candidateSha256,
            version: expectedCandidateVersion,
            versionCode: expectedCandidateCode,
          },
          uidPreserved: true,
          firstInstallTimePreserved: true,
          microphoneGrantPreserved: true,
          signingLineageObserved: true,
          launchVerified: true,
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
      adb(adbPath, serial, ["uninstall", expectedApplicationId], env, true);
    }
  }
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`[android-candidate-upgrade] ${error?.message || error}`);
    process.exit(1);
  }
}

module.exports = { assertMetadata, main, metadata, sha256File };
