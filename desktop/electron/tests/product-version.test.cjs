const assert = require("node:assert/strict");
const { mkdtempSync, rmSync, writeFileSync, readFileSync } = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { resolveDesktopProductVersion } = require("../product-version.cjs");

test("dev and packaged product version resolution comes from EchoDesk package metadata", () => {
  const sourcePackage = path.resolve(__dirname, "../../package.json");
  assert.equal(resolveDesktopProductVersion(sourcePackage), "0.3.5");

  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-packaged-version-"));
  const packagedPackage = path.join(root, "package.json");
  try {
    writeFileSync(packagedPackage, JSON.stringify({ name: "EchoDesk", version: "9.9.9" }));
    assert.equal(resolveDesktopProductVersion(packagedPackage), "9.9.9");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("backend contract path does not use Electron runtime version", () => {
  const main = readFileSync(path.resolve(__dirname, "../main.cjs"), "utf8");
  assert.match(main, /productVersion:\s*DESKTOP_PRODUCT_VERSION/);
  assert.doesNotMatch(main, /productVersion:\s*app\.getVersion\(\)/);
});
