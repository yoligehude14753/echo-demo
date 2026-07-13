/* eslint-disable no-console */
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..", "..");
const desktopRoot = path.resolve(__dirname, "..");

function readDesktop(relPath) {
  return fs.readFileSync(path.join(desktopRoot, relPath), "utf8");
}

function readRepo(relPath) {
  return fs.readFileSync(path.join(repoRoot, relPath), "utf8");
}

function fail(message) {
  console.error(`[version:check] ${message}`);
  process.exitCode = 1;
}

const pkg = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
const version = String(pkg.version || "").trim();
const semverMatch = version.match(/^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$/);
if (!semverMatch) {
  fail(`desktop/package.json version must be semver, got "${version}"`);
}

const lock = JSON.parse(readDesktop("package-lock.json"));
if (lock.version !== version || lock.packages?.[""]?.version !== version) {
  fail(
    `package-lock root versions (${lock.version || "missing"}, ${lock.packages?.[""]?.version || "missing"}) != ${version}`,
  );
}

const backendInit = readRepo("backend/app/__init__.py");
const backendMatch = backendInit.match(/__version__\s*=\s*["']([^"']+)["']/);
if (!backendMatch) {
  fail("backend/app/__init__.py missing __version__");
} else if (backendMatch[1] !== version) {
  fail(`backend version ${backendMatch[1]} != desktop version ${version}`);
}

const backendConfig = readRepo("backend/app/config.py");
if (!/from app import __version__/.test(backendConfig)) {
  fail("backend/app/config.py must import __version__ from app");
}
if (!/app_version:\s*str\s*=\s*__version__/.test(backendConfig)) {
  fail("Settings.app_version must default to __version__");
}

const envExample = readRepo(".env.example");
const envVersion = envExample.match(/^APP_VERSION=(.+)$/m)?.[1]?.trim();
if (envVersion !== version) {
  fail(`.env.example APP_VERSION ${envVersion || "missing"} != ${version}`);
}

const androidVersions = JSON.parse(readDesktop("android/version-codes.json"));
const releases = Array.isArray(androidVersions.releases)
  ? androidVersions.releases
  : [];
if (androidVersions.schemaVersion !== 1 || releases.length === 0) {
  fail("Android version-code ledger must contain schemaVersion=1 and releases");
}
let previousAndroidCode = 0;
const seenAndroidVersions = new Set();
for (const release of releases) {
  const releaseVersion = String(release?.version || "");
  const releaseCode = Number(release?.versionCode);
  if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(releaseVersion)) {
    fail(`Android version-code ledger has invalid version "${releaseVersion}"`);
  }
  if (
    !Number.isSafeInteger(releaseCode) ||
    releaseCode <= previousAndroidCode ||
    releaseCode > 2_100_000_000
  ) {
    fail(
      `Android versionCode ${releaseCode} must be a positive, strictly increasing safe integer`,
    );
  }
  if (seenAndroidVersions.has(releaseVersion)) {
    fail(`Android version-code ledger repeats ${releaseVersion}`);
  }
  seenAndroidVersions.add(releaseVersion);
  previousAndroidCode = releaseCode;
}
const currentAndroidRelease = releases.at(-1);
if (currentAndroidRelease?.version !== version) {
  fail(
    `Android version ledger current version ${currentAndroidRelease?.version || "missing"} != ${version}`,
  );
}
const androidGradle = readDesktop("android/app/build.gradle");
if (!/versionCode\s+currentAndroidRelease\.versionCode\s+as\s+Integer/.test(androidGradle)) {
  fail("Android Gradle versionCode must come from the append-only version ledger");
}
if (!/versionName\s+currentAndroidRelease\.version\.toString\(\)/.test(androidGradle)) {
  fail("Android Gradle versionName must come from the append-only version ledger");
}

const installedSmoke = readDesktop("tests/e2e-real/installed-local-workflow.spec.ts");
if (!installedSmoke.includes(`expect(appVersion).toBe("${version}")`)) {
  fail(`installed-local-workflow App version assertion must be ${version}`);
}
if (!installedSmoke.includes(`expect((health.backend as JsonMap).version).toBe("${version}")`)) {
  fail(`installed-local-workflow backend version assertion must be ${version}`);
}

if (process.exitCode) {
  process.exit(process.exitCode);
}

console.log(`[version:check] OK v${version}`);
