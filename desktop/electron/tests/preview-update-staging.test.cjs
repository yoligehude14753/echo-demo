"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const {
  TARGET_VERSION,
  TARGET_VERSION_CODE,
  assertVersionContract,
  canonicalAssets,
} = require("../../scripts/preview-update-contract.cjs");
const {
  resolveTagSha,
  validateRelease,
} = require("../../scripts/preview-update-release-readback.cjs");
const {
  installedAsarPath,
  readAndroidVersion,
} = require("../../scripts/preview-update-installed-readback.cjs");

const SOURCE_SHA = "b".repeat(40);
const DIGEST = `sha256:${"c".repeat(64)}`;

function releaseFixture(extraAssets = []) {
  return {
    id: 44,
    tag_name: `v${TARGET_VERSION}`,
    name: `EchoDesk ${TARGET_VERSION}`,
    body: `EchoDesk ${TARGET_VERSION} formal release notes`,
    draft: false,
    prerelease: false,
    html_url: `https://github.com/example/releases/tag/v${TARGET_VERSION}`,
    assets: [
      ...Object.values(canonicalAssets()).map((name, index) => ({
        id: index + 1,
        name,
        size: index + 100,
        digest: DIGEST,
        browser_download_url: `https://github.com/example/releases/download/v${TARGET_VERSION}/${name}`,
      })),
      ...extraAssets,
    ],
  };
}

test("0.3.4 promotion remains a strict updater-compatible metadata step", () => {
  const result = assertVersionContract();
  assert.equal(result.previousVersion, "0.3.3-preview.4");
  assert.equal(result.targetVersion, TARGET_VERSION);
  assert.equal(result.androidVersionCode, TARGET_VERSION_CODE);
  assert.deepEqual(result.assets, {
    darwin: "EchoDesk-0.3.4-arm64-mac.zip",
    win32: "EchoDesk.Setup.0.3.4.exe",
    android: "EchoDesk-0.3.4-android.apk",
  });
  assert.equal(result.releaseChannel, "stable");
  assert.equal(result.releaseNotes, "EchoDesk 0.3.4");
  assert.equal(result.preview4ToStable, "in-app");
});

test("release readback requires exact tag SHA and exactly three canonical update assets", () => {
  const evidence = validateRelease(releaseFixture(), SOURCE_SHA, SOURCE_SHA);
  assert.equal(evidence.sourceSha, SOURCE_SHA);
  assert.equal(evidence.prerelease, false);
  assert.match(evidence.releaseNotes, /0\.3\.4/);
  assert.deepEqual(Object.keys(evidence.assets).sort(), Object.values(canonicalAssets()).sort());
  assert.throws(
    () => validateRelease(releaseFixture(), SOURCE_SHA, "d".repeat(40)),
    /release tag points to/,
  );
  assert.throws(
    () => validateRelease(releaseFixture([{ name: "unexpected.txt" }]), SOURCE_SHA, SOURCE_SHA),
    /release assets/,
  );
});

test("annotated release tags are dereferenced to their commit", () => {
  const calls = [];
  const fakeGh = (_command, args) => {
    calls.push(args.at(-1));
    if (calls.length === 1) {
      return JSON.stringify({ object: { type: "tag", sha: "a".repeat(40) } });
    }
    return JSON.stringify({ object: { type: "commit", sha: SOURCE_SHA } });
  };
  assert.equal(resolveTagSha(`v${TARGET_VERSION}`, fakeGh), SOURCE_SHA);
  assert.equal(calls.length, 2);
});

test("installed readback resolves desktop app.asar and Android package version", () => {
  assert.equal(
    installedAsarPath("darwin", "/Applications/EchoDesk.app"),
    path.join("/Applications/EchoDesk.app", "Contents", "Resources", "app.asar"),
  );
  assert.equal(
    installedAsarPath("win32", "C:\\Program Files\\EchoDesk\\EchoDesk.exe"),
    path.win32.join("C:\\Program Files\\EchoDesk", "resources", "app.asar"),
  );
  const observed = readAndroidVersion("emulator-5554", (_command, args) => {
    assert.deepEqual(args.slice(0, 2), ["-s", "emulator-5554"]);
    return `Packages:\n  versionCode=${TARGET_VERSION_CODE} minSdk=24 targetSdk=36\n  versionName=${TARGET_VERSION}\n`;
  });
  assert.equal(observed.version, TARGET_VERSION);
  assert.equal(observed.versionCode, TARGET_VERSION_CODE);
});
