"use strict";

const assert = require("node:assert/strict");
const { existsSync, readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { DebugLogger } = require("builder-util/out/DebugLogger");
const {
  validateConfiguration,
} = require("app-builder-lib/out/util/config/config");

const desktopRoot = path.resolve(__dirname, "../..");
const repoRoot = path.resolve(desktopRoot, "..");

const retiredWorkflows = [
  ".github/workflows/build-android-tv-release.yml",
  ".github/workflows/build-desktop-release-candidates.yml",
  ".github/workflows/build-windows-installer.yml",
  ".github/workflows/live-contract.yml",
  ".github/workflows/migrate-android-release-secrets.yml",
];

const retiredScripts = [
  "scripts/check-ci-action-pins.py",
  "scripts/check-pip-audit-evidence.py",
  "scripts/generate-android-sbom.py",
  "scripts/generate-release-sbom.py",
  "desktop/scripts/package-tv-installer.cjs",
];

test("electron-builder accepts the committed package configuration", async () => {
  const pkg = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  );
  assert.equal(pkg.desktopName, "com.echodesk.app.desktop");
  await validateConfiguration(pkg.build, new DebugLogger(false));
});
test("package scripts expose one default release path and no retired installer", () => {
  const pkg = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  );
  assert.equal(pkg.main, "electron/main.cjs");
  assert.equal(pkg.scripts["app:dist"], "npm run release:canonical:mac");
  assert.equal(
    pkg.scripts["preapp:dist:mac"],
    "node scripts/canonical-only-contract.cjs",
  );
  assert.equal(
    pkg.scripts["app:dist:mac"],
    "node scripts/desktop-release-signing.cjs mac",
  );
  assert.equal(
    pkg.scripts["release:canonical:mac"],
    "node scripts/canonical-mac-release.cjs",
  );
  assert.equal(pkg.scripts["release:prune-noncanonical"], undefined);
  assert.equal(pkg.scripts["release:retire-canonical"], undefined);
  assert.equal(pkg.scripts["app:package:tv"], undefined);
  assert.equal(pkg.scripts["app:dist:mac:adhoc-test"], undefined);
});

test("retired release workflows and helper scripts stay absent", () => {
  for (const relativePath of [...retiredWorkflows, ...retiredScripts]) {
    assert.equal(existsSync(path.join(repoRoot, relativePath)), false, relativePath);
  }
});

test("current CI files do not call retired release entrypoints", () => {
  const currentWorkflows = [
    ".github/workflows/ci.yml",
    ".github/workflows/windows-desktop-artifact.yml",
  ];
  const retiredReference =
    /build-android-tv-release|build-desktop-release-candidates|build-windows-installer|live-contract|migrate-android-release-secrets|package-tv-installer|generate-(?:android-)?release-sbom|check-(?:ci-action-pins|pip-audit-evidence)/;

  for (const relativePath of currentWorkflows) {
    const source = readFileSync(path.join(repoRoot, relativePath), "utf8");
    assert.doesNotMatch(source, retiredReference, relativePath);
  }

  const windows = readFileSync(
    path.join(repoRoot, ".github/workflows/windows-desktop-artifact.yml"),
    "utf8",
  );
  assert.match(windows, /npm run app:dist:win:unsigned-test/);
  assert.doesNotMatch(windows, /npm run app:dist:win(?:\s|$)/m);
});

test("Electron identity IPC binds every issued session to the public backend origin", () => {
  const main = readFileSync(
    path.join(desktopRoot, "electron", "main.cjs"),
    "utf8",
  );
  assert.match(main, /backend_origin:\s*publicSessionOrigin\(\)/g);
  assert.match(
    main,
    /backendBoundJsonFetch\(\{[\s\S]*backendOrigin: publicSessionOrigin\(\),[\s\S]*pathname,/,
  );
  assert.doesNotMatch(main, /fetch\(new URL\(pathname/);
});
