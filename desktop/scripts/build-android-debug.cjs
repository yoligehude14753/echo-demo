#!/usr/bin/env node

const { copyFileSync, mkdirSync, rmSync } = require("node:fs");
const { join } = require("node:path");

const {
  ANDROID_DIR,
  RELEASE_DIR,
  ROOT,
  androidEnvironment,
  patchTvRuntimeMarker,
  run,
} = require("./android-build-common.cjs");

const { version } = require(join(ROOT, "package.json"));
const DEBUG_APK = join(ANDROID_DIR, "app", "build", "outputs", "apk", "debug", "app-debug.apk");
const ANDROID_OUTPUT = join(RELEASE_DIR, `EchoDesk-${version}-android-development.apk`);
const TV_OUTPUT = join(RELEASE_DIR, `EchoDesk-${version}-android-tv-development.apk`);

function buildDebugVariant(applicationId, outputPath, env) {
  run("./gradlew", ["clean", "assembleDebug", `-PechoApplicationId=${applicationId}`], {
    cwd: ANDROID_DIR,
    env,
  });
  copyFileSync(DEBUG_APK, outputPath);
}

function main() {
  const baseEnv = androidEnvironment();
  const env = {
    ...baseEnv,
    VITE_ECHODESK_RUNTIME_MODE: "development",
    VITE_ECHODESK_PRINCIPAL_MODE:
      baseEnv.VITE_ECHODESK_PRINCIPAL_MODE || "local",
  };
  mkdirSync(RELEASE_DIR, { recursive: true });
  rmSync(ANDROID_OUTPUT, { force: true });
  rmSync(TV_OUTPUT, { force: true });
  console.log("[android] DEVELOPMENT ONLY: Gradle debug signing; never publish these APKs");
  run("npm", ["run", "build"], { env });
  run("npx", ["cap", "sync", "android"], { env });
  run(
    process.execPath,
    [join(ROOT, "scripts", "prepare-android-gradle-locks.cjs")],
    { env },
  );
  buildDebugVariant("com.echodesk.app", ANDROID_OUTPUT, env);

  const restoreTvRuntimeMarker = patchTvRuntimeMarker();
  try {
    buildDebugVariant("com.echodesk.tv", TV_OUTPUT, env);
  } finally {
    restoreTvRuntimeMarker();
  }
  console.log(`[android] development APK: ${ANDROID_OUTPUT}`);
  console.log(`[android] development TV APK: ${TV_OUTPUT}`);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`[android] ${error?.message || error}`);
    process.exit(1);
  }
}

module.exports = { main };
