#!/usr/bin/env node
"use strict";

// The acceptance manifest deliberately contains no install location, account
// data, endpoint, or user content.  It binds the release identity to the
// executable, renderer archive, and frozen backend bytes.
const { createHash } = require("node:crypto");
const { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } = require("node:fs");
const { dirname, join, resolve } = require("node:path");

const DESKTOP_ROOT = resolve(__dirname, "..");
const DEFAULT_MANIFEST_PATH = join(DESKTOP_ROOT, "release", "final", "installed-release-manifest.json");

function fail(code) { throw new Error(`[installed-release-manifest] ${code}`); }
function executableFor(appPath) { return join(appPath, "Contents", "MacOS", "EchoDesk"); }
function rendererFor(appPath) { return join(appPath, "Contents", "Resources", "app.asar"); }
function backendFor(appPath) { return join(appPath, "Contents", "Resources", "backend", "echodesk-backend"); }
function sha256File(filePath) { return createHash("sha256").update(readFileSync(filePath)).digest("hex"); }
function resourceHashes(appPath) {
  const resources = {
    executable_sha256: executableFor(appPath),
    renderer_sha256: rendererFor(appPath),
    backend_sha256: backendFor(appPath),
  };
  const hashes = {};
  for (const [field, resourcePath] of Object.entries(resources)) {
    if (!existsSync(resourcePath)) fail("resource_missing");
    hashes[field] = sha256File(resourcePath);
  }
  return hashes;
}

function packageReleaseIdentity(desktopRoot = DESKTOP_ROOT) {
  const pkg = JSON.parse(readFileSync(join(desktopRoot, "package.json"), "utf8"));
  const version = String(pkg.version || "").trim();
  if (!version || /[\0\r\n]/.test(version)) fail("invalid_version");
  // EchoDesk intentionally uses the package version as the app build value.
  return { version, build: version };
}

function writeInstalledReleaseManifest({
  appPath,
  desktopRoot = DESKTOP_ROOT,
  manifestPath = DEFAULT_MANIFEST_PATH,
} = {}) {
  const manifest = {
    ...packageReleaseIdentity(desktopRoot),
    ...resourceHashes(String(appPath || "")),
  };
  const target = resolve(manifestPath);
  mkdirSync(dirname(target), { recursive: true });
  const temporary = `${target}.${process.pid}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(manifest)}\n`, "utf8");
  renameSync(temporary, target);
  return manifest;
}

function verifyInstalledReleaseManifest({
  appPath,
  desktopRoot = DESKTOP_ROOT,
  manifestPath = DEFAULT_MANIFEST_PATH,
} = {}) {
  let manifest;
  try { manifest = JSON.parse(readFileSync(manifestPath, "utf8")); } catch { fail("manifest_missing"); }
  const identity = packageReleaseIdentity(desktopRoot);
  if (manifest?.version !== identity.version || manifest?.build !== identity.build) {
    fail("release_identity_mismatch");
  }
  const expected = resourceHashes(String(appPath || ""));
  for (const field of Object.keys(expected)) {
    if (String(manifest?.[field] || "").toLowerCase() !== expected[field]) fail("resource_hash_mismatch");
  }
  return { resource_count: Object.keys(expected).length, hash_match: true };
}

module.exports = {
  DEFAULT_MANIFEST_PATH,
  backendFor,
  executableFor,
  packageReleaseIdentity,
  rendererFor,
  resourceHashes,
  sha256File,
  verifyInstalledReleaseManifest,
  writeInstalledReleaseManifest,
};
