#!/usr/bin/env node

const { copyFileSync, existsSync, mkdirSync } = require("node:fs");
const { homedir } = require("node:os");
const { join } = require("node:path");
const { spawnSync } = require("node:child_process");

const ROOT = join(__dirname, "..");
const ANDROID_DIR = join(ROOT, "android");
const APK_PATH = join(
  ANDROID_DIR,
  "app",
  "build",
  "outputs",
  "apk",
  "release",
  "app-release-unsigned.apk",
);
const { version } = require(join(ROOT, "package.json"));
const RELEASE_DIR = join(ROOT, "release");
const TV_APK_PATH = join(RELEASE_DIR, `EchoDesk-${version}-android-tv.apk`);
const ANDROID_APK_PATH = join(RELEASE_DIR, `EchoDesk-${version}-android.apk`);
const ANDROID_APP_ID = "com.echodesk.app";
const TV_APP_ID = "com.echodesk.tv";

function firstExisting(paths) {
  return paths.find((p) => p && existsSync(p)) || null;
}

function resolveJavaHome() {
  return firstExisting([
    process.env.JAVA_HOME,
    "/Applications/Android Studio.app/Contents/jbr/Contents/Home",
    "/Library/Java/JavaVirtualMachines/temurin-21.jdk/Contents/Home",
    "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home",
    "/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home",
  ]);
}

function resolveAndroidHome() {
  return firstExisting([
    process.env.ANDROID_HOME,
    process.env.ANDROID_SDK_ROOT,
    join(homedir(), "Library", "Android", "sdk"),
  ]);
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || ROOT,
    env: options.env || process.env,
    stdio: "inherit",
    shell: false,
  });
  if (result.status !== 0) {
    process.exit(result.status || 1);
  }
}

function resolveBuildTool(name) {
  const candidates = [
    join(androidHome, "build-tools", "36.1.0", name),
    join(androidHome, "build-tools", "36.0.0", name),
    join(androidHome, "build-tools", "35.0.0", name),
    join(androidHome, "build-tools", "34.0.0", name),
  ];
  const tool = firstExisting(candidates);
  if (!tool) {
    console.error(`Android build failed: ${name} not found in Android SDK build-tools.`);
    process.exit(1);
  }
  return tool;
}

function ensureDemoKeystore(env) {
  const keytool = join(env.JAVA_HOME, "bin", "keytool");
  const keystore = process.env.ECHODESK_ANDROID_KEYSTORE || join(homedir(), ".android", "debug.keystore");
  const alias = process.env.ECHODESK_ANDROID_KEY_ALIAS || "androiddebugkey";
  const storePass = process.env.ECHODESK_ANDROID_KEYSTORE_PASSWORD || "android";
  const keyPass = process.env.ECHODESK_ANDROID_KEY_PASSWORD || storePass;
  mkdirSync(join(homedir(), ".android"), { recursive: true });
  if (!existsSync(keystore)) {
    run(keytool, [
      "-genkeypair",
      "-v",
      "-keystore",
      keystore,
      "-storepass",
      storePass,
      "-keypass",
      keyPass,
      "-alias",
      alias,
      "-keyalg",
      "RSA",
      "-keysize",
      "2048",
      "-validity",
      "10000",
      "-dname",
      "CN=EchoDesk Demo,O=EchoDesk,C=CN",
    ], { env });
  }
  return { keystore, alias, storePass, keyPass };
}

function signReleaseApk(appId, outputPath, env) {
  const zipalign = resolveBuildTool("zipalign");
  const apksigner = resolveBuildTool("apksigner");
  const aligned = join(RELEASE_DIR, `.tmp-${appId}-aligned.apk`);
  const signed = join(RELEASE_DIR, `.tmp-${appId}-signed.apk`);
  const signing = ensureDemoKeystore(env);
  run(zipalign, ["-p", "-f", "4", APK_PATH, aligned], { env });
  run(apksigner, [
    "sign",
    "--ks",
    signing.keystore,
    "--ks-key-alias",
    signing.alias,
    "--ks-pass",
    `pass:${signing.storePass}`,
    "--key-pass",
    `pass:${signing.keyPass}`,
    "--out",
    signed,
    aligned,
  ], { env });
  run(apksigner, ["verify", "--verbose", signed], { env });
  copyFileSync(signed, outputPath);
}

const javaHome = resolveJavaHome();
const androidHome = resolveAndroidHome();

if (!javaHome) {
  console.error(
    "Android build failed: JAVA_HOME not found. Install Android Studio or export JAVA_HOME.",
  );
  process.exit(1);
}

if (!androidHome) {
  console.error(
    "Android build failed: Android SDK not found. Install Android SDK or export ANDROID_HOME.",
  );
  process.exit(1);
}

const env = {
  ...process.env,
  JAVA_HOME: javaHome,
  ANDROID_HOME: androidHome,
  ANDROID_SDK_ROOT: androidHome,
  PATH: `${join(javaHome, "bin")}:${join(androidHome, "platform-tools")}:${join(androidHome, "emulator")}:${join(androidHome, "build-tools", "36.1.0")}:${process.env.PATH || ""}`,
};

console.log(`[android] JAVA_HOME=${javaHome}`);
console.log(`[android] ANDROID_HOME=${androidHome}`);
console.log(
  `[android] API base=${process.env.VITE_ECHODESK_API_BASE || "runtime default"}`,
);

run("npm", ["run", "build"], { env });
run("npx", ["cap", "sync", "android"], { env });
mkdirSync(RELEASE_DIR, { recursive: true });

run("./gradlew", ["clean", "assembleRelease", `-PechoApplicationId=${ANDROID_APP_ID}`], {
  cwd: ANDROID_DIR,
  env,
});
console.log(`[android] Android APK ready: ${APK_PATH}`);
signReleaseApk(ANDROID_APP_ID, ANDROID_APK_PATH, env);
console.log(`[android] Android APK copied: ${ANDROID_APK_PATH}`);

run("./gradlew", ["clean", "assembleRelease", `-PechoApplicationId=${TV_APP_ID}`], {
  cwd: ANDROID_DIR,
  env,
});
console.log(`[android] TV APK ready: ${APK_PATH}`);
signReleaseApk(TV_APP_ID, TV_APK_PATH, env);
console.log(`[android] TV-compatible APK copied: ${TV_APK_PATH}`);
