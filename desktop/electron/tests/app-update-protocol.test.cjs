"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  compareSemver,
  isCompatibleUpgrade,
  normalizeDigest,
  releaseVersionForChannel,
  selectRelease,
  updateAssetName,
} = require("../app-update-protocol.cjs");

const DIGEST = `sha256:${"a".repeat(64)}`;

function release(version, {
  prerelease = true,
  assetName = updateAssetName("darwin", version),
  digest = DIGEST,
} = {}) {
  return {
    tag_name: `v${version}`,
    name: `EchoDesk ${version}`,
    html_url: `https://github.com/example/repo/releases/tag/v${version}`,
    prerelease,
    draft: false,
    assets: [
      {
        name: assetName,
        size: 123,
        digest,
        browser_download_url:
          `https://github.com/example/repo/releases/download/v${version}/${assetName}`,
      },
    ],
  };
}

test("preview semver orders prereleases without treating them as stable", () => {
  assert.equal(compareSemver("0.3.4-preview.10", "0.3.4-preview.2"), 1);
  assert.equal(compareSemver("0.3.4", "0.3.4-preview.10"), 1);
  assert.equal(compareSemver("0.3.4-preview.1", "0.3.3"), 1);
});

test("preview channel queries release arrays and selects the exact mac ZIP", () => {
  const selected = selectRelease(
    [
      release("0.3.4-preview.1"),
      release("0.3.4-preview.2"),
      release("0.3.5-preview.1", { assetName: "EchoDesk-unsafe.dmg" }),
    ],
    {
      currentVersion: "0.3.4-preview.0",
      channel: "preview",
      platform: "darwin",
    },
  );
  assert.equal(selected.version, "0.3.4-preview.2");
  assert.equal(
    selected.asset.name,
    "EchoDesk-0.3.4-preview.2-arm64-mac.zip",
  );
});

test("stable stays on formal releases while preview can promote to a newer formal release", () => {
  const releases = [
    release("0.3.4-preview.2"),
    release("0.3.4", { prerelease: false }),
  ];
  assert.equal(
    selectRelease(releases, {
      currentVersion: "0.3.3",
      channel: "stable",
      platform: "darwin",
    }).version,
    "0.3.4",
  );
  assert.equal(
    selectRelease(releases, {
      currentVersion: "0.3.4-preview.0",
      channel: "preview",
      platform: "darwin",
    }).version,
    "0.3.4",
  );
});

test("Preview4 discovers formal 0.3.4 on every supported updater path", () => {
  for (const platform of ["darwin", "win32", "android"]) {
    const selected = selectRelease(
      [
        release("0.3.4", {
          prerelease: false,
          assetName: updateAssetName(platform, "0.3.4"),
        }),
      ],
      {
        currentVersion: "0.3.3-preview.4",
        channel: "preview",
        platform,
      },
    );
    assert.equal(selected?.version, "0.3.4");
    assert.equal(selected?.asset.name, updateAssetName(platform, "0.3.4"));
  }
});

test("formal 0.3.4 discovers formal 0.3.5 on every supported updater path", () => {
  for (const platform of ["darwin", "win32", "android"]) {
    const selected = selectRelease(
      [
        release("0.3.5", {
          prerelease: false,
          assetName: updateAssetName(platform, "0.3.5"),
        }),
      ],
      {
        currentVersion: "0.3.4",
        channel: "stable",
        platform,
      },
    );
    assert.equal(selected?.version, "0.3.5");
    assert.equal(selected?.asset.name, updateAssetName(platform, "0.3.5"));
  }
});

test("preview ignores adhoc and malformed tags even when marked prerelease", () => {
  const malformed = release("0.3.5-preview.1");
  malformed.tag_name = "vadhoc-test";
  assert.equal(releaseVersionForChannel(malformed, "preview"), null);
  assert.equal(
    selectRelease([malformed, release("0.3.3-preview.4")], {
      currentVersion: "0.3.3-preview.3",
      channel: "preview",
      platform: "darwin",
    }).version,
    "0.3.3-preview.4",
  );
});

test("preview falls back when a newer release lacks its platform asset", () => {
  const selected = selectRelease(
    [
      release("0.3.3-preview.5", { assetName: "EchoDesk-unsupported.dmg" }),
      release("0.3.3-preview.4"),
    ],
    {
      currentVersion: "0.3.3-preview.3",
      channel: "preview",
      platform: "darwin",
    },
  );
  assert.equal(selected.version, "0.3.3-preview.4");
});

test("preview stays on the current core train and rejects missing digests", () => {
  assert.equal(
    isCompatibleUpgrade(
      "0.3.4-preview.1",
      "0.3.3-preview.3",
      "preview",
    ),
    false,
  );
  const selected = selectRelease(
    [
      release("0.3.4-preview.1"),
      release("0.3.3-preview.5", { digest: null }),
      release("0.3.3-preview.4"),
    ],
    {
      currentVersion: "0.3.3-preview.3",
      channel: "preview",
      platform: "darwin",
    },
  );
  assert.equal(selected.version, "0.3.3-preview.4");
});

test("asset digest is mandatory and must be GitHub sha256", () => {
  assert.equal(normalizeDigest(DIGEST), "a".repeat(64));
  assert.equal(normalizeDigest("sha512:abc"), null);
  assert.equal(
    selectRelease([release("0.3.4-preview.1", { digest: null })], {
      currentVersion: "0.3.3",
      channel: "preview",
      platform: "darwin",
    }),
    null,
  );
});

test("platform asset names are exact", () => {
  assert.equal(
    updateAssetName("win32", "0.3.4-preview.1"),
    "EchoDesk.Setup.0.3.4-preview.1.exe",
  );
  assert.equal(
    updateAssetName("android", "0.3.4-preview.1"),
    "EchoDesk-0.3.4-preview.1-android-universal-PREVIEW.apk",
  );
  assert.equal(
    updateAssetName("android", "0.3.4"),
    "EchoDesk-0.3.4-android.apk",
  );
});

test("Preview3 discovers the exact Preview4 asset on every supported updater path", () => {
  for (const platform of ["darwin", "win32", "android"]) {
    const selected = selectRelease(
      [release("0.3.3-preview.4", { assetName: updateAssetName(platform, "0.3.3-preview.4") })],
      {
        currentVersion: "0.3.3-preview.3",
        channel: "preview",
        platform,
      },
    );
    assert.equal(selected?.version, "0.3.3-preview.4");
    assert.equal(selected?.asset.name, updateAssetName(platform, "0.3.3-preview.4"));
  }
});

test("detached updater stages and verifies mac payload without command scripts", () => {
  const helper = readFileSync(
    path.resolve(__dirname, "../detached-updater.cjs"),
    "utf8",
  );
  assert.match(helper, /path\.join\(path\.dirname\(plan\.artifactPath\), "Payload"\)/);
  assert.match(helper, /\["-dr", "com\.apple\.quarantine", stagedApp\]/);
  assert.match(helper, /"--deep"[\s\S]*"--sign"[\s\S]*"-"[\s\S]*stagedApp/);
  assert.match(helper, /renameSync\(plan\.backupPath, plan\.currentBundlePath\)/);
  assert.match(helper, /run\(plan\.artifactPath, \["\/S"\]\)/);
  assert.doesNotMatch(helper, /\.command|shell:\s*true/);
});
