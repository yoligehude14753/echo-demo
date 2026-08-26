/* eslint-disable @typescript-eslint/no-var-requires */
"use strict";

const { execFileSync } = require("node:child_process");
const {
  existsSync,
  openSync,
  closeSync,
  readSync,
  readdirSync,
  statSync,
} = require("node:fs");
const { resolve, join, extname } = require("node:path");

const DESKTOP_ROOT = resolve(__dirname, "..");
const DEFAULT_MAIN_ENTITLEMENTS = join(
  DESKTOP_ROOT,
  "build",
  "entitlements.mac.plist",
);
const DEFAULT_INHERIT_ENTITLEMENTS = join(
  DESKTOP_ROOT,
  "build",
  "entitlements.mac.inherit.plist",
);
const REQUIRED_ELECTRON_ENTITLEMENTS = Object.freeze([
  "com.apple.security.device.audio-input",
  "com.apple.security.cs.allow-jit",
  "com.apple.security.cs.allow-unsigned-executable-memory",
]);
const MACH_O_MAGICS = new Set([
  0xfeedface,
  0xcefaedfe,
  0xfeedfacf,
  0xcffaedfe,
  0xcafebabe,
  0xbebafeca,
  0xcafebabf,
  0xbfbafeca,
]);

function requireRegularFile(filePath, label) {
  if (!existsSync(filePath) || !statSync(filePath).isFile()) {
    throw new Error(`[mac-bundle-sign] missing ${label}: ${filePath}`);
  }
}

function requireDirectory(directoryPath, label) {
  if (!existsSync(directoryPath) || !statSync(directoryPath).isDirectory()) {
    throw new Error(`[mac-bundle-sign] missing ${label}: ${directoryPath}`);
  }
}

function bundlePaths(appPath) {
  const contents = join(appPath, "Contents");
  const resources = join(contents, "Resources");
  return {
    contents,
    appAsar: join(resources, "app.asar"),
    backend: join(resources, "backend", "echodesk-backend"),
  };
}

function defaultRunner(command, args, options = {}) {
  return execFileSync(command, args, {
    encoding: options.capture ? "utf8" : undefined,
    stdio: options.capture ? ["ignore", "pipe", "pipe"] : "inherit",
  });
}

function walk(rootPath, result = []) {
  for (const entry of readdirSync(rootPath, { withFileTypes: true })) {
    const candidate = join(rootPath, entry.name);
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) {
      result.push({ path: candidate, kind: "directory" });
      walk(candidate, result);
    } else if (entry.isFile()) {
      result.push({ path: candidate, kind: "file" });
    }
  }
  return result;
}

function isMachO(filePath) {
  let descriptor;
  try {
    descriptor = openSync(filePath, "r");
    const header = Buffer.allocUnsafe(4);
    if (readSync(descriptor, header, 0, 4, 0) !== 4) return false;
    return MACH_O_MAGICS.has(header.readUInt32BE(0));
  } catch {
    return false;
  } finally {
    if (descriptor !== undefined) closeSync(descriptor);
  }
}

function depth(filePath) {
  return filePath.split("/").length;
}

function nestedSigningTargets(appPath) {
  const { contents } = bundlePaths(appPath);
  requireDirectory(contents, "app Contents");
  const entries = walk(contents);
  const machOFiles = entries
    .filter((entry) => entry.kind === "file" && isMachO(entry.path))
    .map((entry) => entry.path)
    .sort((left, right) => depth(right) - depth(left));
  const codeBundles = entries
    .filter(
      (entry) =>
        entry.kind === "directory" &&
        [".framework", ".xpc"].includes(extname(entry.path)),
    )
    .map((entry) => entry.path)
    .sort((left, right) => depth(right) - depth(left));
  const helperApps = entries
    .filter(
      (entry) => entry.kind === "directory" && extname(entry.path) === ".app",
    )
    .map((entry) => entry.path)
    .sort((left, right) => depth(right) - depth(left));
  return { machOFiles, codeBundles, helperApps };
}

function signingArgs({ identity, target, entitlements, timestamp }) {
  const args = ["--force", "--sign", identity, "--options", "runtime"];
  args.push(timestamp ? "--timestamp" : "--timestamp=none");
  if (entitlements) args.push("--entitlements", entitlements);
  args.push(target);
  return args;
}

function signOne({ identity, target, entitlements = null, timestamp, runner }) {
  runner(
    "/usr/bin/codesign",
    signingArgs({ identity, target, entitlements, timestamp }),
  );
}

function displayedEntitlements(target, runner = defaultRunner) {
  try {
    const output = runner(
      "/usr/bin/codesign",
      ["--display", "--entitlements", ":-", target],
      { capture: true },
    );
    if (output && typeof output === "object") {
      return `${String(output.stdout || "")}\n${String(output.stderr || "")}`;
    }
    return String(output || "");
  } catch (error) {
    const stdout = error && typeof error === "object" ? error.stdout : "";
    const stderr = error && typeof error === "object" ? error.stderr : "";
    return `${String(stdout || "")}\n${String(stderr || "")}`;
  }
}

function entitlementIsTrue(plist, key) {
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(
    `<key>\\s*${escaped}\\s*</key>\\s*<true\\s*/>`,
    "m",
  ).test(plist);
}

function assertRequiredEntitlements(target, label, runner = defaultRunner) {
  const plist = displayedEntitlements(target, runner);
  for (const key of REQUIRED_ELECTRON_ENTITLEMENTS) {
    if (!entitlementIsTrue(plist, key)) {
      throw new Error(`[mac-bundle-sign] ${label} is missing required entitlement ${key}`);
    }
  }
}

function verifyMacBundle(appPath, { runner = defaultRunner } = {}) {
  const resolvedAppPath = resolve(appPath);
  const paths = bundlePaths(resolvedAppPath);
  requireRegularFile(paths.appAsar, "app.asar");
  requireRegularFile(paths.backend, "bundled backend");
  runner(
    "/usr/bin/codesign",
    ["--verify", "--deep", "--strict", "--verbose=4", resolvedAppPath],
  );
  assertRequiredEntitlements(resolvedAppPath, "main app", runner);
  for (const helperApp of nestedSigningTargets(resolvedAppPath).helperApps) {
    assertRequiredEntitlements(helperApp, "Electron helper", runner);
  }
  runner("/usr/bin/codesign", ["--display", "--verbose=4", resolvedAppPath]);
  console.log(`[mac-bundle-sign] strict entitlement verification passed ${resolvedAppPath}`);
  return paths;
}

function signMacBundle(
  appPath,
  {
    identity,
    timestamp,
    mainEntitlements = DEFAULT_MAIN_ENTITLEMENTS,
    inheritEntitlements = DEFAULT_INHERIT_ENTITLEMENTS,
    runner = defaultRunner,
  },
) {
  const resolvedAppPath = resolve(appPath);
  requireDirectory(resolvedAppPath, "app bundle");
  requireRegularFile(mainEntitlements, "main entitlements");
  requireRegularFile(inheritEntitlements, "inherit entitlements");
  const normalizedIdentity = String(identity || "").trim();
  if (!normalizedIdentity || /[\0\r\n]/.test(normalizedIdentity)) {
    throw new Error("[mac-bundle-sign] signing identity must be a non-empty single line");
  }

  const targets = nestedSigningTargets(resolvedAppPath);
  for (const target of targets.machOFiles) {
    signOne({ identity: normalizedIdentity, target, timestamp, runner });
  }
  for (const target of targets.codeBundles) {
    signOne({ identity: normalizedIdentity, target, timestamp, runner });
  }
  for (const target of targets.helperApps) {
    signOne({
      identity: normalizedIdentity,
      target,
      entitlements: inheritEntitlements,
      timestamp,
      runner,
    });
  }
  signOne({
    identity: normalizedIdentity,
    target: resolvedAppPath,
    entitlements: mainEntitlements,
    timestamp,
    runner,
  });
  return verifyMacBundle(resolvedAppPath, { runner });
}

function signAdhocMacBundle(appPath, options = {}) {
  console.log(`[mac-bundle-sign] final development ad-hoc signing ${resolve(appPath)}`);
  return signMacBundle(appPath, {
    ...options,
    identity: "-",
    timestamp: false,
  });
}

function assertDeveloperIdSigningHash(value) {
  const normalized = String(value || "").trim().toUpperCase();
  if (!/^[0-9A-F]{40}$/.test(normalized)) {
    throw new Error("[mac-bundle-sign] Developer ID signing identity must be a 40-character SHA-1 hash");
  }
  return normalized;
}

function signDeveloperIdMacBundle(appPath, signingHash, options = {}) {
  return signMacBundle(appPath, {
    ...options,
    identity: assertDeveloperIdSigningHash(signingHash),
    timestamp: true,
  });
}

module.exports = {
  DEFAULT_INHERIT_ENTITLEMENTS,
  DEFAULT_MAIN_ENTITLEMENTS,
  REQUIRED_ELECTRON_ENTITLEMENTS,
  assertDeveloperIdSigningHash,
  assertRequiredEntitlements,
  bundlePaths,
  nestedSigningTargets,
  signAdhocMacBundle,
  signDeveloperIdMacBundle,
  signingArgs,
  verifyMacBundle,
};

if (require.main === module) {
  const appPath = process.argv[2];
  if (!appPath) {
    throw new Error("usage: node scripts/mac-bundle-sign.cjs <EchoDesk.app>");
  }
  const identity = String(process.env.CSC_NAME || "").trim();
  if (identity) signDeveloperIdMacBundle(appPath, identity);
  else signAdhocMacBundle(appPath);
}
