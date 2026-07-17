"use strict";

const { execFileSync } = require("node:child_process");
const { readFileSync } = require("node:fs");
const path = require("node:path");

const {
  compareSemver,
  selectRelease,
  updateAssetName,
} = require("../electron/app-update-protocol.cjs");

const DESKTOP_ROOT = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(DESKTOP_ROOT, "..");
const PREVIOUS_VERSION = "0.3.3-preview.4";
const TARGET_VERSION = "0.3.4";
const TARGET_VERSION_CODE = 30400;
const OWNER = "yoligehude14753";
const REPO = "echo-demo";

function readJson(filePath) {
  return JSON.parse(readFileSync(filePath, "utf8"));
}

function canonicalAssets(version = TARGET_VERSION) {
  return {
    darwin: updateAssetName("darwin", version),
    win32: updateAssetName("win32", version),
    android: updateAssetName("android", version),
  };
}

function releaseFixture(version, platform) {
  const name = updateAssetName(platform, version);
  return {
    tag_name: `v${version}`,
    name: `EchoDesk ${version}`,
    draft: false,
    prerelease: false,
    html_url: `https://github.com/${OWNER}/${REPO}/releases/tag/v${version}`,
    assets: [
      {
        name,
        size: 1,
        digest: `sha256:${"a".repeat(64)}`,
        browser_download_url:
          `https://github.com/${OWNER}/${REPO}/releases/download/v${version}/${name}`,
      },
    ],
  };
}

function assertVersionContract(root = REPO_ROOT) {
  const desktopRoot = path.join(root, "desktop");
  const pkg = readJson(path.join(desktopRoot, "package.json"));
  const lock = readJson(path.join(desktopRoot, "package-lock.json"));
  const ledger = readJson(path.join(desktopRoot, "android", "version-codes.json"));
  const backend = readFileSync(path.join(root, "backend", "app", "__init__.py"), "utf8");
  const env = readFileSync(path.join(root, ".env.example"), "utf8");
  const releaseBuilder = readFileSync(
    path.join(desktopRoot, "scripts", "build-android-release.cjs"),
    "utf8",
  );

  if (pkg.version !== TARGET_VERSION) {
    throw new Error(`desktop version ${pkg.version} != ${TARGET_VERSION}`);
  }
  if (
    lock.version !== TARGET_VERSION ||
    lock.packages?.[""]?.version !== TARGET_VERSION
  ) {
    throw new Error("desktop package-lock root version is not the target");
  }
  if (!backend.includes(`__version__ = "${TARGET_VERSION}"`)) {
    throw new Error("backend version is not the target");
  }
  if (!new RegExp(`^APP_VERSION=${TARGET_VERSION.replaceAll(".", "\\.")}$`, "m").test(env)) {
    throw new Error(".env.example APP_VERSION is not the target");
  }
  const releases = ledger.releases;
  const previous = releases?.at(-2);
  const target = releases?.at(-1);
  if (
    previous?.version !== PREVIOUS_VERSION ||
    previous?.status !== "historical-preview" ||
    target?.version !== TARGET_VERSION ||
    target?.versionCode !== TARGET_VERSION_CODE ||
    target?.status !== "current-release"
  ) {
    throw new Error("Android version ledger is not a strict Preview4 -> 0.3.4 release step");
  }
  if (
    !releaseBuilder.includes('const { version } = require(join(ROOT, "package.json"))') ||
    !releaseBuilder.includes('`EchoDesk-${version}-android.apk`')
  ) {
    throw new Error("Android formal release builder is not bound to the package version");
  }
  if (compareSemver(TARGET_VERSION, PREVIOUS_VERSION) <= 0) {
    throw new Error("target version must be newer than the installed Preview4 version");
  }

  const assets = canonicalAssets();
  for (const platform of Object.keys(assets)) {
    const selected = selectRelease([releaseFixture(TARGET_VERSION, platform)], {
      currentVersion: PREVIOUS_VERSION,
      channel: "preview",
      platform,
    });
    if (!selected || selected.version !== TARGET_VERSION || selected.asset.name !== assets[platform]) {
      throw new Error(`Preview4 updater cannot select the canonical ${platform} 0.3.4 asset`);
    }
  }
  return {
    schema: 1,
    repository: `${OWNER}/${REPO}`,
    previousVersion: PREVIOUS_VERSION,
    targetVersion: TARGET_VERSION,
    targetTag: `v${TARGET_VERSION}`,
    androidVersionCode: TARGET_VERSION_CODE,
    assets,
    releaseChannel: "stable",
    releaseNotes: `EchoDesk ${TARGET_VERSION}`,
    preview4ToStable: "in-app",
  };
}

function currentSourceSha(root = REPO_ROOT) {
  return execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: root,
    encoding: "utf8",
  }).trim();
}

if (require.main === module) {
  try {
    const result = assertVersionContract();
    process.stdout.write(
      `${JSON.stringify({ ...result, sourceSha: currentSourceSha() }, null, 2)}\n`,
    );
  } catch (error) {
    process.stderr.write(`[release-update-contract] ${error?.message || error}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  OWNER,
  PREVIOUS_VERSION,
  REPO,
  TARGET_VERSION,
  TARGET_VERSION_CODE,
  assertVersionContract,
  canonicalAssets,
  currentSourceSha,
};
