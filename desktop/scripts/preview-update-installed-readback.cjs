"use strict";

const { execFileSync } = require("node:child_process");
const { existsSync, readFileSync, writeFileSync } = require("node:fs");
const path = require("node:path");

const {
  TARGET_VERSION,
  TARGET_VERSION_CODE,
} = require("./preview-update-contract.cjs");

function parseArgs(argv) {
  const options = { platform: null, target: null, serial: null, output: null };
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    const value = argv[index + 1];
    if (["--platform", "--target", "--serial", "--output"].includes(name) && value) {
      options[name.slice(2)] = value;
      index += 1;
    } else {
      throw new Error(`unknown or incomplete argument: ${name}`);
    }
  }
  if (!["darwin", "win32", "android"].includes(options.platform)) {
    throw new Error("--platform must be darwin, win32, or android");
  }
  if (options.platform !== "android" && !path.isAbsolute(options.target || "")) {
    throw new Error("desktop --target must be an absolute installed App or directory path");
  }
  return options;
}

function installedAsarPath(platform, target) {
  if (target.endsWith(".asar")) return target;
  if (platform === "darwin") {
    return path.join(target, "Contents", "Resources", "app.asar");
  }
  const root = target.toLowerCase().endsWith(".exe") ? path.win32.dirname(target) : target;
  return path.win32.join(root, "resources", "app.asar");
}

function readDesktopVersion(platform, target) {
  const asarPath = installedAsarPath(platform, target);
  if (!existsSync(asarPath)) {
    throw new Error(`installed app.asar not found: ${asarPath}`);
  }
  let extractFile;
  try {
    ({ extractFile } = require("@electron/asar"));
  } catch {
    throw new Error("@electron/asar is required; run this from a locked desktop npm install");
  }
  const pkg = JSON.parse(extractFile(asarPath, "package.json").toString("utf8"));
  return { version: String(pkg.version || ""), evidencePath: asarPath };
}

function readAndroidVersion(serial, exec = execFileSync) {
  const args = [];
  if (serial) args.push("-s", serial);
  args.push("shell", "dumpsys", "package", "com.echodesk.app");
  const output = exec("adb", args, { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 });
  const versionName = output.match(/^\s*versionName=([^\s]+)\s*$/m)?.[1] || "";
  const versionCode = Number(output.match(/^\s*versionCode=(\d+)\b/m)?.[1] || 0);
  return { version: versionName, versionCode, evidencePath: "adb:dumpsys package com.echodesk.app" };
}

function validateInstalled(options, exec = execFileSync) {
  const observed = options.platform === "android"
    ? readAndroidVersion(options.serial, exec)
    : readDesktopVersion(options.platform, options.target);
  if (observed.version !== TARGET_VERSION) {
    throw new Error(`installed version ${observed.version || "missing"} != ${TARGET_VERSION}`);
  }
  if (options.platform === "android" && observed.versionCode !== TARGET_VERSION_CODE) {
    throw new Error(
      `installed Android versionCode ${observed.versionCode} != ${TARGET_VERSION_CODE}`,
    );
  }
  return {
    schema: 1,
    platform: options.platform,
    targetVersion: TARGET_VERSION,
    targetVersionCode: options.platform === "android" ? TARGET_VERSION_CODE : null,
    observed,
    readbackAt: new Date().toISOString(),
  };
}

function main(argv = process.argv.slice(2), exec = execFileSync) {
  const options = parseArgs(argv);
  const evidence = validateInstalled(options, exec);
  const serialized = `${JSON.stringify(evidence, null, 2)}\n`;
  if (options.output) {
    writeFileSync(options.output, serialized, { mode: 0o600, flag: "wx" });
  }
  process.stdout.write(serialized);
  return evidence;
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    process.stderr.write(`[preview-update-installed-readback] ${error?.message || error}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  installedAsarPath,
  main,
  parseArgs,
  readAndroidVersion,
  validateInstalled,
};
