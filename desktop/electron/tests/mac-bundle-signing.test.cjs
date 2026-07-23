const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");

const desktopRoot = join(__dirname, "../..");
const packageJson = JSON.parse(readFileSync(join(desktopRoot, "package.json"), "utf8"));
const afterPack = readFileSync(join(desktopRoot, "scripts", "after-pack-mac.cjs"), "utf8");
const signer = readFileSync(join(desktopRoot, "scripts", "mac-bundle-sign.cjs"), "utf8");

test("mac dir bootstrap signs only after electron-builder completes", () => {
  const command = packageJson.scripts["app:build:mac:test"];
  assert.match(command, /electron-builder --mac --arm64 --dir --publish never && node scripts\/mac-bundle-sign\.cjs release\/mac-arm64\/EchoDesk\.app/);
  assert.match(afterPack, /helper plist patched; signing is deferred until the final bundle stage/);
  assert.doesNotMatch(afterPack, /codesign/);
});

test("mac bundle signer retains required packaged resources and strict verification", () => {
  assert.match(signer, /resources, "app\.asar"/);
  assert.match(signer, /resources, "backend", "echodesk-backend"/);
  assert.match(signer, /resources, "agent-runtime", "worker\.mjs"/);
  assert.match(signer, /--verify", "--deep", "--strict", "--verbose=4/);
  assert.match(signer, /--display", "--verbose=4/);
});
