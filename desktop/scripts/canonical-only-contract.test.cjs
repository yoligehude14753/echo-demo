const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  LEGACY_EXACT_PATHS,
  assertCanonicalOnly,
  scanCanonicalOnly,
} = require("./canonical-only-contract.cjs");

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-canonical-only-"));
  fs.mkdirSync(path.join(root, "desktop"), { recursive: true });
  fs.writeFileSync(path.join(root, "desktop", "package.json"), JSON.stringify({
    main: "electron/main.cjs",
    scripts: { "app:dist:mac": "node scripts/desktop-release-signing.cjs mac" },
  }));
  return root;
}

test("canonical workspace reports zero legacy runtime entries", () => {
  const root = fixture();
  assert.deepEqual(assertCanonicalOnly(root), {
    exact_legacy_entry_count: 0,
    legacy_package_script_count: 0,
    temporary_runtime_copy_count: 0,
    manual_installer_entry_count: 0,
    violation_count: 0,
  });
});

test("legacy package scripts, runtime copies, and manual installers fail closed", () => {
  const root = fixture();
  fs.writeFileSync(path.join(root, "main.cjs"), "legacy");
  fs.writeFileSync(path.join(root, "desktop", "package.json"), JSON.stringify({
    scripts: { "hotpatch:apply": "node scripts/desktop-resource-hotpatch.cjs apply" },
  }));
  fs.mkdirSync(path.join(root, ".codex-tmp", "old", "electron"), { recursive: true });
  fs.writeFileSync(path.join(root, ".codex-tmp", "old", "electron", "main.cjs"), "legacy");
  fs.mkdirSync(path.join(root, "dist", "manual"), { recursive: true });
  fs.writeFileSync(path.join(root, "dist", "manual", "unknown.bin"), "legacy");

  assert.deepEqual(scanCanonicalOnly(root), {
    exact_legacy_entry_count: 1,
    legacy_package_script_count: 1,
    temporary_runtime_copy_count: 1,
    manual_installer_entry_count: 1,
    violation_count: 4,
  });
  assert.throws(() => assertCanonicalOnly(root), /violation_count=4/);
});

test("a standalone prune package entrypoint fails closed", () => {
  const root = fixture();
  fs.writeFileSync(path.join(root, "desktop", "package.json"), JSON.stringify({
    scripts: { "release:prune": "node scripts/prune-noncanonical-release.cjs" },
  }));
  assert.equal(scanCanonicalOnly(root).legacy_package_script_count, 1);
  assert.throws(() => assertCanonicalOnly(root), /violation_count=1/);
});

test("every retired release and bootstrap asset is rejected by exact path", () => {
  for (const relativePath of LEGACY_EXACT_PATHS) {
    const root = fixture();
    const retiredPath = path.join(root, relativePath);
    fs.mkdirSync(path.dirname(retiredPath), { recursive: true });
    fs.writeFileSync(retiredPath, "retired");
    const result = scanCanonicalOnly(root);
    assert.equal(result.exact_legacy_entry_count, 1, relativePath);
    assert.equal(result.violation_count, 1, relativePath);
  }
});
