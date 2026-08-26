/* eslint-disable @typescript-eslint/no-var-requires */

const { existsSync, statSync } = require("node:fs");
const { join, resolve } = require("node:path");

const { peMachine } = require("./build-backend-win.cjs");

function backendArtifactName(platform) {
  if (platform === "win32") return "echodesk-backend.exe";
  if (platform === "darwin" || platform === "linux") return "echodesk-backend";
  throw new Error(`[release-contract] unsupported Electron platform: ${platform}`);
}

function verifyBundledBackend({ platform, repoRoot = resolve(__dirname, "../..") }) {
  const specPath = join(repoRoot, "backend", "packaging", "echodesk-backend.spec");
  if (!existsSync(specPath) || !statSync(specPath).isFile()) {
    throw new Error(
      `[release-contract] missing ${specPath}; refusing to package a local-first app`,
    );
  }

  const artifactPath = join(
    repoRoot,
    "backend",
    "dist",
    backendArtifactName(platform),
  );
  if (
    !existsSync(artifactPath) ||
    !statSync(artifactPath).isFile() ||
    statSync(artifactPath).size < 1
  ) {
    throw new Error(
      `[release-contract] missing backend artifact ${artifactPath}; refusing to package`,
    );
  }

  if (platform === "win32") {
    const machine = peMachine(artifactPath);
    if (machine !== 0x8664) {
      throw new Error(
        `[release-contract] Windows backend is not x64 PE: machine=0x${machine.toString(16)}`,
      );
    }
  }

  return artifactPath;
}

async function beforePack(context) {
  const platform =
    context?.electronPlatformName || context?.packager?.platform?.nodeName;
  verifyBundledBackend({ platform });
}

module.exports = beforePack;
module.exports.beforePack = beforePack;
module.exports.verifyBundledBackend = verifyBundledBackend;
