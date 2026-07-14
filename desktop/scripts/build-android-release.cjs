#!/usr/bin/env node

const { createHash } = require("node:crypto");
const {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} = require("node:fs");
const { tmpdir } = require("node:os");
const { basename, join } = require("node:path");

const {
  ANDROID_DIR,
  RELEASE_DIR,
  ROOT,
  androidEnvironment,
  apksignerSignerArgs,
  normalizeFingerprint,
  patchTvRuntimeMarker,
  releaseSigningContract,
  resolveBuildTool,
  run,
  verifySigningIdentities,
} = require("./android-build-common.cjs");

const { version } = require(join(ROOT, "package.json"));
const UNSIGNED_APK = join(
  ANDROID_DIR,
  "app",
  "build",
  "outputs",
  "apk",
  "release",
  "app-release-unsigned.apk",
);
const ANDROID_OUTPUT = join(RELEASE_DIR, `EchoDesk-${version}-android.apk`);
const TV_OUTPUT = join(RELEASE_DIR, `EchoDesk-${version}-android-tv.apk`);
const MANIFEST_OUTPUT = join(
  RELEASE_DIR,
  `EchoDesk-${version}-android-signing.json`,
);
const LINEAGE_OUTPUT = join(
  RELEASE_DIR,
  `EchoDesk-${version}-android-signing-lineage.bin`,
);

function sha256File(filePath) {
  return createHash("sha256").update(readFileSync(filePath)).digest("hex");
}

function certificateDigests(output) {
  return [
    ...new Set(
      [...output.matchAll(/certificate SHA-256 digest:\s*([0-9a-f:]+)/gi)].map(
        (match) => normalizeFingerprint(match[1]),
      ),
    ),
  ];
}

function requireRangeFingerprint(output, minSdk, maxSdk, expected, role) {
  const match = output.match(
    new RegExp(
      `Signer \\(minSdkVersion=${minSdk}, maxSdkVersion=${maxSdk}\\) certificate SHA-256 digest:\\s*([0-9a-f:]+)`,
      "i",
    ),
  );
  if (!match) {
    throw new Error(`apksigner did not report the ${role} SDK range`);
  }
  const actual = normalizeFingerprint(match[1]);
  if (actual !== expected) {
    throw new Error(
      `${role} SDK range certificate mismatch: expected ${expected}, got ${actual}`,
    );
  }
}

function verifyReleaseApk(apkPath, applicationId, signing, env) {
  const apksigner = resolveBuildTool(env.ANDROID_HOME, "apksigner");
  const aapt = resolveBuildTool(env.ANDROID_HOME, "aapt");
  const zipalign = resolveBuildTool(env.ANDROID_HOME, "zipalign");
  const verification = run(
    apksigner,
    ["verify", "--verbose", "--print-certs", "--Werr", apkPath],
    { env, capture: true },
  );
  for (const scheme of ["v2", "v3", "v3.1"]) {
    if (
      !verification.includes(
        `Verified using ${scheme} scheme (APK Signature Scheme ${scheme}): true`,
      )
    ) {
      throw new Error(
        `${apkPath} is not verified with APK Signature Scheme ${scheme}`,
      );
    }
  }
  requireRangeFingerprint(
    verification,
    signing.rotationMinSdkVersion,
    2147483647,
    signing.current.expectedFingerprint,
    "current",
  );

  const badging = run(aapt, ["dump", "badging", apkPath], {
    env,
    capture: true,
  });
  const packageMatch = badging.match(/package: name='([^']+)'/);
  if (packageMatch?.[1] !== applicationId) {
    throw new Error(
      `release applicationId mismatch: expected ${applicationId}, got ${packageMatch?.[1] || "missing"}`,
    );
  }
  if (/^application-debuggable(?:\s|$)/m.test(badging)) {
    throw new Error(`public release APK is debuggable: ${apkPath}`);
  }
  const minSdkMatch = badging.match(/sdkVersion:'(\d+)'/);
  const minSdkVersion = Number.parseInt(minSdkMatch?.[1] || "", 10);
  if (
    !Number.isInteger(minSdkVersion) ||
    minSdkVersion >= signing.rotationMinSdkVersion
  ) {
    throw new Error(
      `unexpected minSdkVersion ${minSdkMatch?.[1] || "missing"}; legacy signer range would be untested`,
    );
  }

  const legacyVerification = run(
    apksigner,
    [
      "verify",
      "--verbose",
      "--print-certs",
      "--min-sdk-version",
      String(minSdkVersion),
      "--max-sdk-version",
      String(signing.rotationMinSdkVersion - 1),
      apkPath,
    ],
    { env, capture: true },
  );
  const legacyDigests = certificateDigests(legacyVerification);
  if (
    legacyDigests.length !== 1 ||
    legacyDigests[0] !== signing.legacy.expectedFingerprint
  ) {
    throw new Error(
      `legacy SDK range must verify only with ${signing.legacy.expectedFingerprint}; got ${legacyDigests.join(",")}`,
    );
  }

  const currentVerification = run(
    apksigner,
    [
      "verify",
      "--verbose",
      "--print-certs",
      "--min-sdk-version",
      String(signing.rotationMinSdkVersion),
      "--max-sdk-version",
      "36",
      apkPath,
    ],
    { env, capture: true },
  );
  if (
    !currentVerification.includes(
      "Verified using v3.1 scheme (APK Signature Scheme v3.1): true",
    )
  ) {
    throw new Error(
      `API ${signing.rotationMinSdkVersion}+ does not verify through v3.1`,
    );
  }
  requireRangeFingerprint(
    currentVerification,
    signing.rotationMinSdkVersion,
    2147483647,
    signing.current.expectedFingerprint,
    "current",
  );
  requireRangeFingerprint(
    currentVerification,
    minSdkVersion,
    signing.rotationMinSdkVersion - 1,
    signing.legacy.expectedFingerprint,
    "legacy",
  );
  run(zipalign, ["-c", "-P", "16", "4", apkPath], { env });
  return {
    minSdkVersion,
    legacyCertificateSha256: signing.legacy.expectedFingerprint,
    currentCertificateSha256: signing.current.expectedFingerprint,
  };
}

function createSigningLineage(signing, lineagePath, env) {
  const apksigner = resolveBuildTool(env.ANDROID_HOME, "apksigner");
  rmSync(lineagePath, { force: true });
  run(
    apksigner,
    [
      "rotate",
      "--out",
      lineagePath,
      "--old-signer",
      ...apksignerSignerArgs(signing.legacy),
      "--set-installed-data",
      "true",
      "--set-shared-uid",
      "false",
      "--set-permission",
      "true",
      "--set-rollback",
      "false",
      "--set-auth",
      "true",
      "--new-signer",
      ...apksignerSignerArgs(signing.current),
    ],
    { env },
  );
  if (!existsSync(lineagePath) || statSync(lineagePath).size < 1) {
    throw new Error("apksigner did not create a signing certificate lineage");
  }
}

function buildReleaseVariant(
  applicationId,
  outputPath,
  signing,
  lineagePath,
  env,
) {
  const apksigner = resolveBuildTool(env.ANDROID_HOME, "apksigner");
  run(
    "./gradlew",
    [
      "--no-daemon",
      "clean",
      "assembleRelease",
      "-PechoExternalSigning=true",
      `-PechoApplicationId=${applicationId}`,
    ],
    { cwd: ANDROID_DIR, env },
  );
  if (!existsSync(UNSIGNED_APK) || statSync(UNSIGNED_APK).size < 1) {
    throw new Error(
      `Gradle did not create unsigned release APK: ${UNSIGNED_APK}`,
    );
  }
  rmSync(outputPath, { force: true });
  run(
    apksigner,
    [
      "sign",
      "--v4-signing-enabled",
      "false",
      "--out",
      outputPath,
      "--lineage",
      lineagePath,
      "--rotation-min-sdk-version",
      String(signing.rotationMinSdkVersion),
      ...apksignerSignerArgs(signing.legacy),
      "--next-signer",
      ...apksignerSignerArgs(signing.current),
      UNSIGNED_APK,
    ],
    { env },
  );
  const verification = verifyReleaseApk(
    outputPath,
    applicationId,
    signing,
    env,
  );
  return {
    applicationId,
    file: basename(outputPath),
    sha256: sha256File(outputPath),
    ...verification,
    legacyMaxSdkVersion: signing.rotationMinSdkVersion - 1,
    currentMinSdkVersion: signing.rotationMinSdkVersion,
  };
}

function main() {
  const baseEnv = androidEnvironment();
  const env = {
    ...baseEnv,
    VITE_ECHODESK_RUNTIME_MODE: "release",
    VITE_ECHODESK_PRINCIPAL_MODE: "public",
  };
  // Validate both private keys and pinned public fingerprints before npm/Gradle work.
  // The historical signer is intentionally retained only for API <= 32 updates.
  const signing = releaseSigningContract(env);
  const identities = verifySigningIdentities(signing, env);
  const tempRoot = mkdtempSync(join(tmpdir(), "echodesk-android-release-"));
  const tempLineage = join(tempRoot, "echodesk-signing-lineage.bin");
  mkdirSync(RELEASE_DIR, { recursive: true });
  for (const target of [
    ANDROID_OUTPUT,
    TV_OUTPUT,
    MANIFEST_OUTPUT,
    LINEAGE_OUTPUT,
  ]) {
    rmSync(target, { force: true });
  }
  try {
    createSigningLineage(signing, tempLineage, env);
    copyFileSync(tempLineage, LINEAGE_OUTPUT);
    run("npm", ["run", "build"], { env });
    run("npx", ["cap", "sync", "android"], { env });
    run(
      process.execPath,
      [join(ROOT, "scripts", "prepare-android-gradle-locks.cjs")],
      { env },
    );
    const android = buildReleaseVariant(
      "com.echodesk.app",
      ANDROID_OUTPUT,
      signing,
      tempLineage,
      env,
    );

    const restoreTvRuntimeMarker = patchTvRuntimeMarker();
    let tv;
    try {
      tv = buildReleaseVariant(
        "com.echodesk.tv",
        TV_OUTPUT,
        signing,
        tempLineage,
        env,
      );
    } finally {
      restoreTvRuntimeMarker();
    }
    const lineageSha256 = sha256File(LINEAGE_OUTPUT);
    writeFileSync(
      MANIFEST_OUTPUT,
      `${JSON.stringify(
        {
          version,
          signing: "rotated-v3.1",
          rotationMinSdkVersion: signing.rotationMinSdkVersion,
          lineage: {
            file: basename(LINEAGE_OUTPUT),
            sha256: lineageSha256,
            legacyCertificateSha256: identities.legacyFingerprint,
            currentCertificateSha256: identities.currentFingerprint,
          },
          artifacts: [android, tv],
        },
        null,
        2,
      )}\n`,
      "utf8",
    );
    console.log(`[android] signed release APK: ${ANDROID_OUTPUT}`);
    console.log(`[android] signed release TV APK: ${TV_OUTPUT}`);
    console.log(`[android] public signing lineage: ${LINEAGE_OUTPUT}`);
    console.log(`[android] signing contract: ${MANIFEST_OUTPUT}`);
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`[android-release] ${error?.message || error}`);
    process.exit(1);
  }
}

module.exports = { createSigningLineage, main, verifyReleaseApk };
