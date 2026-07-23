/* eslint-disable @typescript-eslint/no-var-requires */
"use strict";

const { execFileSync } = require("node:child_process");
const { existsSync, statSync } = require("node:fs");
const { resolve, join } = require("node:path");

function requireRegularFile(path, label) {
  if (!existsSync(path) || !statSync(path).isFile()) {
    throw new Error(`[mac-bundle-sign] missing ${label}: ${path}`);
  }
}

function bundlePaths(appPath) {
  const resources = join(appPath, "Contents", "Resources");
  return {
    appAsar: join(resources, "app.asar"),
    backend: join(resources, "backend", "echodesk-backend"),
    worker: join(resources, "agent-runtime", "worker.mjs"),
  };
}

function verifyMacBundle(appPath) {
  const resolvedAppPath = resolve(appPath);
  const paths = bundlePaths(resolvedAppPath);
  requireRegularFile(paths.appAsar, "app.asar");
  requireRegularFile(paths.backend, "bundled backend");
  requireRegularFile(paths.worker, "agent worker");
  execFileSync(
    "codesign",
    ["--verify", "--deep", "--strict", "--verbose=4", resolvedAppPath],
    { stdio: "inherit" },
  );
  execFileSync("codesign", ["--display", "--verbose=4", resolvedAppPath], {
    stdio: "inherit",
  });
  console.log(`[mac-bundle-sign] strict bundle verification passed ${resolvedAppPath}`);
  return paths;
}

function signAdhocMacBundle(appPath) {
  const resolvedAppPath = resolve(appPath);
  console.log(`[mac-bundle-sign] final development ad-hoc signing ${resolvedAppPath}`);
  execFileSync(
    "codesign",
    ["--force", "--deep", "--sign", "-", resolvedAppPath],
    { stdio: "inherit" },
  );
  return verifyMacBundle(resolvedAppPath);
}

module.exports = {
  bundlePaths,
  signAdhocMacBundle,
  verifyMacBundle,
};

if (require.main === module) {
  const appPath = process.argv[2];
  if (!appPath) {
    throw new Error("usage: node scripts/mac-bundle-sign.cjs <EchoDesk.app>");
  }
  signAdhocMacBundle(appPath);
}
