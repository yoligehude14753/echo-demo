#!/usr/bin/env node
/* eslint-disable @typescript-eslint/no-var-requires */

const {
  copyFileSync,
  existsSync,
  readFileSync,
} = require("node:fs");
const { createHash } = require("node:crypto");
const path = require("node:path");

const desktopRoot = path.resolve(__dirname, "..");

function sha256(filePath) {
  return createHash("sha256").update(readFileSync(filePath)).digest("hex");
}

function installCanonicalLock({ label, source, target, deferUntilTargetExists }) {
  if (!existsSync(source)) {
    throw new Error(`[android-lock] canonical lock is missing: ${source}`);
  }
  const targetDirectory = path.dirname(target);
  if (!existsSync(targetDirectory)) {
    if (deferUntilTargetExists) {
      console.log(
        `[android-lock] ${label} project not generated yet; lock installation deferred`,
      );
      return { installed: false, label, source, target };
    }
    throw new Error(
      `[android-lock] installed ${label} project is missing: ${targetDirectory}`,
    );
  }
  copyFileSync(source, target);
  const sourceDigest = sha256(source);
  const targetDigest = sha256(target);
  if (sourceDigest !== targetDigest) {
    throw new Error(
      `[android-lock] ${label} lock copy mismatch: ${sourceDigest} != ${targetDigest}`,
    );
  }
  console.log(`[android-lock] installed ${label} lock sha256=${targetDigest}`);
  return { installed: true, label, source, target, digest: targetDigest };
}

function installAndroidGradleLocks(root = desktopRoot) {
  const lock = JSON.parse(
    readFileSync(path.join(root, "package-lock.json"), "utf8"),
  );
  const packageRecord = lock.packages?.["node_modules/@capacitor/android"];
  const version = String(packageRecord?.version || "");
  if (version !== "8.4.0") {
    throw new Error(
      `[android-lock] expected @capacitor/android 8.4.0, received ${version || "missing"}`,
    );
  }

  const locksRoot = path.join(root, "android", "gradle", "locks");
  return [
    installCanonicalLock({
      label: `@capacitor/android ${version}`,
      source: path.join(locksRoot, `capacitor-android-${version}.lockfile`),
      target: path.join(
        root,
        "node_modules",
        "@capacitor",
        "android",
        "capacitor",
        "gradle.lockfile",
      ),
      deferUntilTargetExists: false,
    }),
    installCanonicalLock({
      label: "capacitor-cordova-android-plugins",
      source: path.join(
        locksRoot,
        "capacitor-cordova-android-plugins.lockfile",
      ),
      target: path.join(
        root,
        "android",
        "capacitor-cordova-android-plugins",
        "gradle.lockfile",
      ),
      deferUntilTargetExists: true,
    }),
  ];
}

if (require.main === module) {
  installAndroidGradleLocks();
}

module.exports = { installAndroidGradleLocks };
