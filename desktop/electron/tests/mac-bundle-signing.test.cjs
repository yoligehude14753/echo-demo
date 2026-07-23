const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");

const desktopRoot = join(__dirname, "../..");
const packageJson = JSON.parse(readFileSync(join(desktopRoot, "package.json"), "utf8"));
const afterPack = readFileSync(join(desktopRoot, "scripts", "after-pack-mac.cjs"), "utf8");
const signer = readFileSync(join(desktopRoot, "scripts", "mac-bundle-sign.cjs"), "utf8");
const previewPackager = readFileSync(
  join(desktopRoot, "scripts", "package-macos-adhoc-preview.cjs"),
  "utf8",
);

test("mac preview entrypoints use one dir-sign-archive pipeline", () => {
  const command = packageJson.scripts["app:build:mac:test"];
  assert.equal(command, "node scripts/package-macos-adhoc-preview.cjs");
  assert.equal(
    packageJson.scripts["app:dist:mac:adhoc-test"],
    "node scripts/package-macos-adhoc-preview.cjs",
  );
  assert.equal(
    packageJson.scripts["app:dist:mac:adhoc"],
    "npm run app:dist:mac:adhoc-test",
  );
  assert.equal(packageJson.build.afterSign, undefined);
  assert.match(afterPack, /helper plist patched; signing is deferred until the final bundle stage/);
  assert.doesNotMatch(afterPack, /codesign/);
  assert.match(previewPackager, /backend:build:mac/);
  assert.match(previewPackager, /\["run", "build"\]/);
  assert.match(previewPackager, /"--dir"/);
  assert.match(previewPackager, /signBundle\(appPath\);/);
  assert.match(previewPackager, /archiveSignedApp\(\{ appPath, archivePath, runCommand \}\)/);
  assert.ok(
    previewPackager.indexOf('"--dir"') < previewPackager.indexOf("signBundle(appPath);"),
    "electron-builder --dir must finish before final signing",
  );
  assert.ok(
    previewPackager.indexOf("signBundle(appPath);") <
      previewPackager.indexOf("archiveSignedApp({ appPath, archivePath, runCommand })"),
    "strict final signing must finish before archive creation",
  );
  assert.match(previewPackager, /--sequesterRsrc/);
  assert.match(previewPackager, /archive does not contain \$\{appName\}\/Contents\//);
  assert.doesNotMatch(previewPackager, /--mac", "dmg"|--mac dmg/);
});

test("mac bundle signer retains required packaged resources and strict verification", () => {
  assert.match(signer, /resources, "app\.asar"/);
  assert.match(signer, /resources, "backend", "echodesk-backend"/);
  assert.match(signer, /resources, "agent-runtime", "worker\.mjs"/);
  assert.match(signer, /--verify", "--deep", "--strict", "--verbose=4/);
  assert.match(signer, /--display", "--verbose=4/);
});
