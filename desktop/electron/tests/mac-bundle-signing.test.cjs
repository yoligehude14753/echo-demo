const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");

const desktopRoot = join(__dirname, "../..");
const packageJson = JSON.parse(readFileSync(join(desktopRoot, "package.json"), "utf8"));
const afterPack = readFileSync(join(desktopRoot, "scripts", "after-pack-mac.cjs"), "utf8");
const signer = readFileSync(join(desktopRoot, "scripts", "mac-bundle-sign.cjs"), "utf8");
const mainEntitlements = readFileSync(
  join(desktopRoot, "build", "entitlements.mac.plist"),
  "utf8",
);
const inheritEntitlements = readFileSync(
  join(desktopRoot, "build", "entitlements.mac.inherit.plist"),
  "utf8",
);
const { assertDeveloperIdSigningHash } = require("../../scripts/mac-bundle-sign.cjs");

test("mac release entrypoints expose only the formal canonical build", () => {
  assert.equal(packageJson.scripts["app:dist"], "npm run release:canonical:mac");
  assert.equal(packageJson.scripts["app:dist:mac"], "node scripts/desktop-release-signing.cjs mac");
  assert.equal(packageJson.scripts["release:canonical:mac"], "node scripts/canonical-mac-release.cjs");
  assert.equal(packageJson.scripts["app:build:mac:test"], undefined);
  assert.equal(packageJson.scripts["app:dist:mac:adhoc"], undefined);
  assert.equal(packageJson.scripts["app:dist:mac:adhoc-test"], undefined);
  assert.equal(packageJson.build.afterSign, undefined);
  assert.match(afterPack, /helper plist patched; signing is deferred until the final bundle stage/);
  assert.doesNotMatch(afterPack, /codesign/);
});

test("mac bundle signer retains required packaged resources and strict verification", () => {
  assert.match(signer, /resources, "app\.asar"/);
  assert.match(signer, /resources, "backend", "echodesk-backend"/);
  assert.doesNotMatch(signer, /agent-runtime|fused worker/);
  assert.match(signer, /--verify", "--deep", "--strict", "--verbose=4/);
  assert.match(signer, /--display", "--verbose=4/);
  assert.doesNotMatch(signer, /"--force",\s*"--deep"/);
});

test("Developer ID bundle signing accepts only an exact SHA-1 identity hash", () => {
  const hash = "A".repeat(40);
  assert.equal(assertDeveloperIdSigningHash(hash), hash);
  assert.throws(
    () => assertDeveloperIdSigningHash("Developer ID Application: Example (ABCDE12345)"),
    /40-character SHA-1 hash/,
  );
  assert.throws(() => assertDeveloperIdSigningHash(hash.slice(0, -1)), /40-character SHA-1 hash/);
});

test("mac signing preserves the minimal Electron microphone and JIT entitlements", () => {
  assert.equal(packageJson.build.mac.hardenedRuntime, true);
  assert.equal(
    packageJson.build.mac.entitlements,
    "build/entitlements.mac.plist",
  );
  assert.equal(
    packageJson.build.mac.entitlementsInherit,
    "build/entitlements.mac.inherit.plist",
  );

  for (const [label, plist] of [
    ["main", mainEntitlements],
    ["helper", inheritEntitlements],
  ]) {
    for (const key of [
      "com.apple.security.device.audio-input",
      "com.apple.security.cs.allow-jit",
      "com.apple.security.cs.allow-unsigned-executable-memory",
    ]) {
      const escapedKey = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      assert.match(
        plist,
        new RegExp(`<key>\\s*${escapedKey}\\s*</key>\\s*<true\\s*/>`),
        `${label} must keep ${key}`,
      );
    }
    assert.doesNotMatch(plist, /com\.apple\.security\.device\.camera/);
    assert.doesNotMatch(plist, /com\.apple\.security\.app-sandbox/);
  }

  assert.match(signer, /for \(const target of targets\.machOFiles\)/);
  assert.match(signer, /for \(const target of targets\.codeBundles\)/);
  assert.match(signer, /entitlements: inheritEntitlements/);
  assert.match(signer, /entitlements: mainEntitlements/);
  assert.match(signer, /assertRequiredEntitlements\(helperApp/);
});
