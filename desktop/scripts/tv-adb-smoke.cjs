#!/usr/bin/env node
const { existsSync, mkdirSync, writeFileSync } = require("node:fs");
const { join, resolve } = require("node:path");
const { execFileSync, spawnSync } = require("node:child_process");

const ROOT = resolve(__dirname, "..");
const { version } = require(join(ROOT, "package.json"));

function usage() {
  console.error(
    [
      "Usage: node scripts/tv-adb-smoke.cjs <tv-ip> [adb-port]",
      "",
      "Env:",
      "  ECHODESK_TV_APK=/path/to/EchoDesk-<version>-smart-tv.apk",
      "  ECHODESK_TV_KEEP_DATA=1",
      "  ECHODESK_TV_AUTH_TIMEOUT_SECONDS=240",
      "  ECHODESK_TV_SMOKE_DIR=/tmp/echodesk-tv-smoke",
    ].join("\n"),
  );
}

function findAdb() {
  const candidates = [
    process.env.ADB,
    "adb",
    join(process.env.HOME || "", "Library/Android/sdk/platform-tools/adb"),
    process.env.ANDROID_HOME && join(process.env.ANDROID_HOME, "platform-tools/adb"),
    process.env.ANDROID_SDK_ROOT && join(process.env.ANDROID_SDK_ROOT, "platform-tools/adb"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    const result = spawnSync(candidate, ["version"], { stdio: "ignore" });
    if (result.status === 0) return candidate;
  }
  throw new Error("adb not found");
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    stdio: options.capture ? ["ignore", "pipe", "pipe"] : "inherit",
  });
  if (result.status !== 0 && !options.allowFail) {
    const detail = [result.stdout, result.stderr].filter(Boolean).join("\n");
    throw new Error(`${command} ${args.join(" ")} failed\n${detail}`);
  }
  return result.stdout || "";
}

function adb(args, options = {}) {
  return run(adbPath, ["-s", serial, ...args], options);
}

function getState() {
  const output = run(adbPath, ["devices"], { capture: true, allowFail: true });
  const line = output
    .split(/\r?\n/)
    .find((row) => row.trim().startsWith(`${serial}\t`) || row.trim().startsWith(`${serial} `));
  return line ? line.trim().split(/\s+/)[1] : "";
}

function waitForAuthorized(timeoutSeconds) {
  const deadline = Date.now() + timeoutSeconds * 1000;
  while (Date.now() < deadline) {
    run(adbPath, ["connect", serial], { capture: true, allowFail: true });
    const state = getState();
    console.log(`[tv-smoke] ${serial} state=${state || "none"}`);
    if (state === "device") return true;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 3000);
  }
  return false;
}

function shellText(command) {
  return adb(["shell", command], { capture: true, allowFail: true }).trim();
}

const ip = process.argv[2];
const port = process.argv[3] || process.env.ECHODESK_TV_ADB_PORT || "5555";
if (!ip) {
  usage();
  process.exit(2);
}

const adbPath = findAdb();
const serial = `${ip}:${port}`;
const pkg = "com.echodesk.tv";
const legacyPkg = "com.echodesk.app";
const apk = resolve(
  process.env.ECHODESK_TV_APK || join(ROOT, "release", `EchoDesk-${version}-smart-tv.apk`),
);
const outDir = resolve(
  process.env.ECHODESK_TV_SMOKE_DIR ||
    join(ROOT, "release", `tv-smoke-${serial.replace(/[:.]/g, "-")}-${Date.now()}`),
);

if (!existsSync(apk)) {
  throw new Error(`APK not found: ${apk}`);
}
mkdirSync(outDir, { recursive: true });

console.log(`[tv-smoke] adb=${adbPath}`);
console.log(`[tv-smoke] serial=${serial}`);
console.log(`[tv-smoke] apk=${apk}`);
console.log(`[tv-smoke] out=${outDir}`);

run(adbPath, ["connect", serial], { capture: true, allowFail: true });
if (!waitForAuthorized(Number(process.env.ECHODESK_TV_AUTH_TIMEOUT_SECONDS || 240))) {
  console.error("[tv-smoke] ADB is not authorized. Accept the RSA debugging prompt on the TV.");
  run(adbPath, ["devices", "-l"], { allowFail: true });
  process.exit(3);
}

if (process.env.ECHODESK_TV_KEEP_DATA !== "1") {
  console.log("[tv-smoke] clearing old app data");
  adb(["shell", "am", "force-stop", pkg], { allowFail: true });
  adb(["shell", "pm", "clear", pkg], { allowFail: true });
  if (process.env.ECHODESK_TV_KEEP_LEGACY !== "1") {
    adb(["shell", "am", "force-stop", legacyPkg], { allowFail: true });
    adb(["shell", "pm", "clear", legacyPkg], { allowFail: true });
    adb(["shell", "pm", "uninstall", legacyPkg], { allowFail: true });
  }
}

console.log("[tv-smoke] installing APK");
adb(["install", "-r", "-d", apk]);
adb(["shell", "pm", "grant", pkg, "android.permission.RECORD_AUDIO"], { allowFail: true });
adb(["shell", "appops", "set", pkg, "RECORD_AUDIO", "allow"], { allowFail: true });
adb(["logcat", "-c"], { allowFail: true });

console.log("[tv-smoke] launching EchoDesk TV");
adb(["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"]);
Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 12000);

const versionInfo = shellText(`dumpsys package ${pkg} | grep -E "versionName|versionCode" | head -5`);
const focus = shellText("dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp' | head -5");
const activity = shellText("dumpsys activity activities | grep -E 'mResumedActivity|ResumedActivity' | head -5");
const wmSize = shellText("wm size; wm density");
const packages = shellText(`pm list packages | grep echodesk || true`);

writeFileSync(join(outDir, "package.txt"), `${versionInfo}\n\n${packages}\n`, "utf8");
writeFileSync(join(outDir, "focus.txt"), `${focus}\n\n${activity}\n\n${wmSize}\n`, "utf8");

adb(["shell", "screencap", "-p", "/sdcard/echodesk-tv-smoke.png"], { allowFail: true });
adb(["pull", "/sdcard/echodesk-tv-smoke.png", join(outDir, "screen.png")], { allowFail: true });
adb(["shell", "uiautomator", "dump", "/sdcard/echodesk-tv-window.xml"], { allowFail: true });
adb(["pull", "/sdcard/echodesk-tv-window.xml", join(outDir, "window.xml")], { allowFail: true });
const logcat = adb(["logcat", "-d", "-t", "1200"], { capture: true, allowFail: true });
writeFileSync(join(outDir, "logcat.txt"), logcat, "utf8");

const summary = {
  serial,
  apk,
  outDir,
  versionInfo,
  focus,
  activity,
  wmSize,
  packages,
  screen: join(outDir, "screen.png"),
  ui: join(outDir, "window.xml"),
  logcat: join(outDir, "logcat.txt"),
};
writeFileSync(join(outDir, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
console.log(JSON.stringify(summary, null, 2));

if (!versionInfo.includes(`versionName=${version}`)) {
  throw new Error(`Installed package version mismatch. Expected ${version}.`);
}
if (!`${focus}\n${activity}`.includes(pkg)) {
  throw new Error(`EchoDesk TV is not the focused/resumed app. focus=${focus} activity=${activity}`);
}
