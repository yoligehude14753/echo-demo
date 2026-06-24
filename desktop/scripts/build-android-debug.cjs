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
  "debug",
  "app-debug.apk",
);
const { version } = require(join(ROOT, "package.json"));
const RELEASE_DIR = join(ROOT, "release");
const TV_APK_PATH = join(RELEASE_DIR, `EchoDesk-${version}-android-tv-debug.apk`);
const ANDROID_APK_PATH = join(RELEASE_DIR, `EchoDesk-${version}-android.apk`);

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
run("./gradlew", ["assembleDebug"], { cwd: ANDROID_DIR, env });

console.log(`[android] APK ready: ${APK_PATH}`);
mkdirSync(RELEASE_DIR, { recursive: true });
copyFileSync(APK_PATH, TV_APK_PATH);
copyFileSync(APK_PATH, ANDROID_APK_PATH);
console.log(`[android] TV-compatible APK copied: ${TV_APK_PATH}`);
console.log(`[android] Android APK copied: ${ANDROID_APK_PATH}`);
