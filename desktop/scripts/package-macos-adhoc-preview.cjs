/* eslint-disable @typescript-eslint/no-var-requires */
"use strict";

const { execFileSync } = require("node:child_process");
const {
  existsSync,
  mkdirSync,
  mkdtempSync,
  renameSync,
  rmSync,
} = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { signAdhocMacBundle } = require("./mac-bundle-sign.cjs");

function run(command, args, options = {}) {
  return execFileSync(command, args, {
    stdio: "inherit",
    ...options,
  });
}

function readPackage(desktopRoot) {
  // eslint-disable-next-line global-require, import/no-dynamic-require
  return require(path.join(desktopRoot, "package.json"));
}

function archiveSignedApp({ appPath, archivePath, runCommand = run }) {
  const archiveDir = path.dirname(archivePath);
  const temporaryDir = mkdtempSync(
    path.join(os.tmpdir(), "echodesk-adhoc-preview-archive-"),
  );
  const temporaryArchive = path.join(temporaryDir, path.basename(archivePath));

  try {
    // ditto preserves the macOS bundle metadata that a generic zip command can lose.
    runCommand("/usr/bin/ditto", [
      "-c",
      "-k",
      "--sequesterRsrc",
      "--keepParent",
      appPath,
      temporaryArchive,
    ]);

    const listing = execFileSync("/usr/bin/unzip", ["-l", temporaryArchive], {
      encoding: "utf8",
    });
    const appName = path.basename(appPath);
    if (!listing.includes(`${appName}/Contents/`)) {
      throw new Error(
        `[mac-adhoc-preview] archive does not contain ${appName}/Contents/: ${temporaryArchive}`,
      );
    }

    mkdirSync(archiveDir, { recursive: true });
    rmSync(archivePath, { force: true });
    renameSync(temporaryArchive, archivePath);
    console.log(`[mac-adhoc-preview] archived signed app ${archivePath}`);
    return archivePath;
  } finally {
    rmSync(temporaryDir, { recursive: true, force: true });
  }
}

function packageMacosAdhocPreview({
  desktopRoot = path.resolve(__dirname, ".."),
  runCommand = run,
  signBundle = signAdhocMacBundle,
} = {}) {
  if (process.platform !== "darwin") {
    throw new Error("[mac-adhoc-preview] macOS is required to build a macOS preview");
  }

  const packageJson = readPackage(desktopRoot);
  const productName = packageJson.build?.productName || "EchoDesk";
  const version = packageJson.version;
  const releaseRoot = path.join(desktopRoot, "release");
  const appPath = path.join(releaseRoot, "mac-arm64", `${productName}.app`);
  const archivePath = path.join(
    releaseRoot,
    "adhoc",
    `${productName}-${version}-arm64-adhoc-preview.zip`,
  );
  const buildEnvironment = {
    ...process.env,
    CSC_IDENTITY_AUTO_DISCOVERY: "false",
    ECHODESK_ADHOC_SIGN: "1",
  };

  runCommand("npm", ["run", "backend:build:mac"], { cwd: desktopRoot });
  runCommand("npm", ["run", "build"], { cwd: desktopRoot });
  runCommand(
    "npx",
    [
      "--no-install",
      "electron-builder",
      "--mac",
      "--arm64",
      "--dir",
      "--publish",
      "never",
    ],
    { cwd: desktopRoot, env: buildEnvironment },
  );

  if (!existsSync(appPath)) {
    throw new Error(`[mac-adhoc-preview] missing packaged app: ${appPath}`);
  }

  // signAdhocMacBundle performs strict verification before any archive is created.
  signBundle(appPath);
  return archiveSignedApp({ appPath, archivePath, runCommand });
}

module.exports = {
  archiveSignedApp,
  packageMacosAdhocPreview,
};

if (require.main === module) {
  packageMacosAdhocPreview();
}
