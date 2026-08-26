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
const OWNER = "yoligehude14753";
const REPO = "echo-demo";

function readJson(filePath) {
  return JSON.parse(readFileSync(filePath, "utf8"));
}

function canonicalReleaseStep(root = REPO_ROOT) {
  const ledger = readJson(
    path.join(root, "desktop", "android", "version-codes.json"),
  );
  const releases = ledger.releases;
  const previous = releases?.at(-2);
  const target = releases?.at(-1);
  if (
    typeof previous?.version !== "string" ||
    previous.status !== "historical-release" ||
    typeof target?.version !== "string" ||
    !Number.isInteger(target?.versionCode) ||
    target.status !== "current-release"
  ) {
    throw new Error(
      "Android version ledger must end with historical-release -> current-release",
    );
  }
  if (compareSemver(target.version, previous.version) <= 0) {
    throw new Error(`target version must be newer than ${previous.version}`);
  }
  return {
    previousVersion: previous.version,
    targetVersion: target.version,
    targetVersionCode: target.versionCode,
  };
}

const {
  previousVersion: PREVIOUS_VERSION,
  targetVersion: TARGET_VERSION,
  targetVersionCode: TARGET_VERSION_CODE,
} = canonicalReleaseStep();

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
  const releaseStep = canonicalReleaseStep(root);
  const previousVersion = releaseStep.previousVersion;
  const targetVersion = releaseStep.targetVersion;
  const targetVersionCode = releaseStep.targetVersionCode;
  const pkg = readJson(path.join(desktopRoot, "package.json"));
  const lock = readJson(path.join(desktopRoot, "package-lock.json"));
  const backend = readFileSync(path.join(root, "backend", "app", "__init__.py"), "utf8");
  const env = readFileSync(path.join(root, ".env.example"), "utf8");
  const releaseBuilder = readFileSync(
    path.join(desktopRoot, "scripts", "build-android-release.cjs"),
    "utf8",
  );

  if (pkg.version !== targetVersion) {
    throw new Error(`desktop version ${pkg.version} != ${targetVersion}`);
  }
  if (
    lock.version !== targetVersion ||
    lock.packages?.[""]?.version !== targetVersion
  ) {
    throw new Error("desktop package-lock root version is not the target");
  }
  if (!backend.includes(`__version__ = "${targetVersion}"`)) {
    throw new Error("backend version is not the target");
  }
  if (!new RegExp(`^APP_VERSION=${targetVersion.replaceAll(".", "\\.")}$`, "m").test(env)) {
    throw new Error(".env.example APP_VERSION is not the target");
  }
  if (
    !releaseBuilder.includes('const { version } = require(join(ROOT, "package.json"))') ||
    !releaseBuilder.includes('`EchoDesk-${version}-android.apk`')
  ) {
    throw new Error("Android formal release builder is not bound to the package version");
  }
  const assets = canonicalAssets(targetVersion);
  for (const platform of Object.keys(assets)) {
    const selected = selectRelease([releaseFixture(targetVersion, platform)], {
      currentVersion: previousVersion,
      channel: "preview",
      platform,
    });
    if (!selected || selected.version !== targetVersion || selected.asset.name !== assets[platform]) {
      throw new Error(
        `${previousVersion} updater cannot select the canonical ${platform} ${targetVersion} asset`,
      );
    }
  }
  return {
    schema: 1,
    repository: `${OWNER}/${REPO}`,
    previousVersion,
    targetVersion,
    targetTag: `v${targetVersion}`,
    androidVersionCode: targetVersionCode,
    assets,
    releaseChannel: "stable",
    releaseNotes: `EchoDesk ${targetVersion}`,
    stableToStable: "in-app",
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
  canonicalReleaseStep,
  currentSourceSha,
};
