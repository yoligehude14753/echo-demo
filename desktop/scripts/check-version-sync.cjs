/* eslint-disable no-console */
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..", "..");
const desktopRoot = path.resolve(__dirname, "..");

function read(relPath) {
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

const backendInit = read("backend/app/__init__.py");
const backendMatch = backendInit.match(/__version__\s*=\s*["']([^"']+)["']/);
if (!backendMatch) {
  fail("backend/app/__init__.py missing __version__");
} else if (backendMatch[1] !== version) {
  fail(`backend version ${backendMatch[1]} != desktop version ${version}`);
}

const backendConfig = read("backend/app/config.py");
if (!/from app import __version__/.test(backendConfig)) {
  fail("backend/app/config.py must import __version__ from app");
}
if (!/app_version:\s*str\s*=\s*__version__/.test(backendConfig)) {
  fail("Settings.app_version must default to __version__");
}

const [, , minorRaw, patchRaw] = semverMatch;
const minor = Number.parseInt(minorRaw, 10);
const patch = Number.parseInt(patchRaw, 10);
const expectedAndroidCode = minor * 100 + patch;
const androidGradle = read("desktop/android/app/build.gradle");
const versionCodeMatch = androidGradle.match(/versionCode\s+(\d+)/);
const versionNameMatch = androidGradle.match(/versionName\s+"([^"]+)"/);
if (!versionCodeMatch || Number.parseInt(versionCodeMatch[1], 10) !== expectedAndroidCode) {
  fail(`Android versionCode must be ${expectedAndroidCode}`);
}
if (!versionNameMatch || versionNameMatch[1] !== version) {
  fail(`Android versionName ${versionNameMatch?.[1] || "missing"} != ${version}`);
}

const changelog = read("CHANGELOG.md");
if (!changelog.includes(`## [${version}]`)) {
  fail(`CHANGELOG.md missing ## [${version}] entry`);
}

for (const relPath of [
  "README.md",
  "docs/INSTALL.md",
  "docs/TV_INSTALL.md",
  "docs/tv-install.html",
]) {
  const body = read(relPath);
  if (!body.includes(`v${version}`) && !body.includes(version)) {
    fail(`${relPath} does not mention current version ${version}`);
  }
}

if (process.exitCode) {
  process.exit(process.exitCode);
}

console.log(`[version:check] OK v${version}`);
