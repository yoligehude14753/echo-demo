/* eslint-disable @typescript-eslint/no-var-requires, no-undef */
const { existsSync, readdirSync, statSync } = require("node:fs");
const { join } = require("node:path");
const { execFileSync } = require("node:child_process");

const USAGE_DESCRIPTIONS = {
  NSMicrophoneUsageDescription:
    "EchoDesk needs microphone access to transcribe meetings.",
  NSCameraUsageDescription:
    "EchoDesk may use camera access when system media APIs enumerate devices.",
};

function setPlistValue(plistPath, key, value) {
  try {
    execFileSync(
      "/usr/libexec/PlistBuddy",
      ["-c", `Set :${key} ${value}`, plistPath],
      {
        stdio: "ignore",
      },
    );
  } catch {
    execFileSync(
      "/usr/libexec/PlistBuddy",
      ["-c", `Add :${key} string ${value}`, plistPath],
      {
        stdio: "ignore",
      },
    );
  }
}

function patchHelperUsageDescriptions(appPath) {
  const frameworksDir = join(appPath, "Contents", "Frameworks");
  if (!existsSync(frameworksDir)) return;

  for (const entry of readdirSync(frameworksDir)) {
    if (!entry.endsWith(".app")) continue;
    const helperApp = join(frameworksDir, entry);
    try {
      if (!statSync(helperApp).isDirectory()) continue;
    } catch {
      continue;
    }
    const plistPath = join(helperApp, "Contents", "Info.plist");
    if (!existsSync(plistPath)) continue;
    for (const [key, value] of Object.entries(USAGE_DESCRIPTIONS)) {
      setPlistValue(plistPath, key, value);
    }
    console.log(`[mac-sign] patched helper usage descriptions ${plistPath}`);
  }
}

module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") {
    return;
  }

  const productName = context.packager.appInfo.productFilename;
  const appPath = join(context.appOutDir, `${productName}.app`);
  if (!existsSync(appPath)) {
    throw new Error(`[mac-sign] Missing packaged app: ${appPath}`);
  }

  patchHelperUsageDescriptions(appPath);

  console.log(
    "[mac-sign] helper plist patched; signing is deferred until the final bundle stage",
  );
};
