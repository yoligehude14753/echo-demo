const assert = require("node:assert/strict");
const test = require("node:test");

const { preferredReleaseAsset } = require("../release-assets.cjs");

const asset = (name) => ({ name, url: `https://example.invalid/${name}` });

test("desktop update checks never select Android-only release assets", () => {
  const androidOnly = [
    asset("EchoDesk-0.3.1-android.apk"),
    asset("EchoDesk-0.3.1-android-tv.apk"),
    asset("android-signing-lineage.bin"),
  ];

  assert.equal(preferredReleaseAsset(androidOnly, "darwin"), null);
  assert.equal(preferredReleaseAsset(androidOnly, "win32"), null);
  assert.equal(preferredReleaseAsset(androidOnly, "linux"), null);
});

test("desktop update checks select only a platform-compatible package", () => {
  const assets = [
    asset("EchoDesk-0.3.1-android.apk"),
    asset("EchoDesk-0.3.1-arm64-mac.zip"),
    asset("EchoDesk-0.3.1-arm64.dmg"),
    asset("EchoDesk.Setup.0.3.1.exe"),
    asset("EchoDesk-0.3.1-linux-x86_64.AppImage"),
    asset("EchoDesk-0.3.1-linux-amd64.deb"),
  ];

  assert.equal(
    preferredReleaseAsset(assets, "darwin")?.name,
    "EchoDesk-0.3.1-arm64.dmg",
  );
  assert.equal(
    preferredReleaseAsset(assets, "win32")?.name,
    "EchoDesk.Setup.0.3.1.exe",
  );
  assert.equal(
    preferredReleaseAsset(assets, "linux")?.name,
    "EchoDesk-0.3.1-linux-x86_64.AppImage",
  );
  assert.equal(preferredReleaseAsset(assets, "freebsd"), null);
});

test("release asset selection tolerates missing and malformed entries", () => {
  assert.equal(preferredReleaseAsset(undefined, "darwin"), null);
  assert.equal(preferredReleaseAsset([null, {}, { name: 42 }], "darwin"), null);
});
