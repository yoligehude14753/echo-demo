#!/usr/bin/env node
/* eslint-disable no-console */
const fs = require("node:fs");
const path = require("node:path");

const DESKTOP_ROOT = path.resolve(__dirname, "..");
const WORKSPACE_ROOT = path.resolve(DESKTOP_ROOT, "..");

const LEGACY_EXACT_PATHS = Object.freeze([
  "main.cjs",
  ".codex_tmp_start_backend_supervisor.ps1",
  ".github/workflows/build-android-tv-release.yml",
  ".github/workflows/build-desktop-release-candidates.yml",
  ".github/workflows/build-windows-installer.yml",
  ".github/workflows/live-contract.yml",
  ".github/workflows/migrate-android-release-secrets.yml",
  "docs/tv-install.html",
  "scripts/check-ci-action-pins.py",
  "scripts/check-pip-audit-evidence.py",
  "scripts/generate-android-sbom.py",
  "scripts/generate-release-sbom.py",
  "desktop/public/boot-fallback.js",
  "desktop/scripts/desktop-resource-hotpatch.cjs",
  "desktop/scripts/desktop-resource-hotpatch.test.cjs",
  "desktop/scripts/package-tv-installer.cjs",
  "desktop/scripts/package-macos-adhoc-preview.cjs",
  "desktop/scripts/package-macos-preview-bootstrap.sh",
  "desktop/scripts/package-macos-preview-bootstrap.test.cjs",
  "desktop/scripts/templates/Install EchoDesk Preview.command",
  "desktop/tests/e2e-real/installed-local-workflow.spec.ts",
]);

const LEGACY_PACKAGE_SCRIPT = /desktop-resource-hotpatch|package-macos-(?:adhoc-preview|preview-bootstrap)|package-tv-installer|prune-noncanonical-release|Install EchoDesk Preview/i;

function filesUnder(root) {
  if (!fs.existsSync(root)) return [];
  const pending = [root];
  const files = [];
  while (pending.length > 0) {
    const current = pending.pop();
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const target = path.join(current, entry.name);
      if (entry.isDirectory()) pending.push(target);
      else if (entry.isFile()) files.push(target);
    }
  }
  return files;
}

function scanCanonicalOnly(workspaceRoot = WORKSPACE_ROOT) {
  const root = path.resolve(workspaceRoot);
  const exactLegacyEntryCount = LEGACY_EXACT_PATHS
    .filter((relativePath) => fs.existsSync(path.join(root, relativePath))).length;

  let legacyPackageScriptCount = 0;
  const packagePath = path.join(root, "desktop", "package.json");
  if (fs.existsSync(packagePath)) {
    const scripts = JSON.parse(fs.readFileSync(packagePath, "utf8")).scripts || {};
    legacyPackageScriptCount = Object.entries(scripts)
      .filter(([name, command]) => /hotpatch/i.test(name) || LEGACY_PACKAGE_SCRIPT.test(String(command)))
      .length;
  }

  const temporaryRuntimeCopyCount = [
    path.join(root, ".codex-tmp"),
    path.join(root, "desktop", ".codex-tmp"),
  ].flatMap(filesUnder).filter((filePath) => path.basename(filePath) === "main.cjs").length;

  // dist/manual has never been a canonical release root. Any file there is
  // unknown ownership and therefore fails closed instead of being allowlisted.
  const manualInstallerEntryCount = filesUnder(path.join(root, "dist", "manual")).length;

  const counts = {
    exact_legacy_entry_count: exactLegacyEntryCount,
    legacy_package_script_count: legacyPackageScriptCount,
    temporary_runtime_copy_count: temporaryRuntimeCopyCount,
    manual_installer_entry_count: manualInstallerEntryCount,
  };
  return {
    ...counts,
    violation_count: Object.values(counts).reduce((total, count) => total + count, 0),
  };
}

function assertCanonicalOnly(workspaceRoot = WORKSPACE_ROOT) {
  const result = scanCanonicalOnly(workspaceRoot);
  if (result.violation_count !== 0) {
    throw new Error(`[canonical-only] violation_count=${result.violation_count}`);
  }
  return result;
}

if (require.main === module) {
  console.log(JSON.stringify(assertCanonicalOnly()));
}

module.exports = {
  LEGACY_EXACT_PATHS,
  assertCanonicalOnly,
  filesUnder,
  scanCanonicalOnly,
};
