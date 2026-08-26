#!/usr/bin/env node
/* eslint-disable no-console */
// Removes only obsolete runnable release artifacts. It deliberately cannot
// traverse into user data roots, Application Support, IndexedDB, or ~/.echodesk.
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { spawnSync } = require("node:child_process");
const {
  DEFAULT_MANIFEST_PATH,
  resourceHashes,
  verifyInstalledReleaseManifest,
} = require("./installed-release-manifest.cjs");

const DESKTOP_ROOT = path.resolve(__dirname, "..");
const RELEASE_ROOT = path.join(DESKTOP_ROOT, "release");
const DEFAULT_APP = "/Applications/EchoDesk.app";
const FORBIDDEN_PATH = /(?:^|\/)(?:Library\/Application Support|Application Support|IndexedDB|\.echodesk)(?:\/|$)/i;

function fail(message) { throw new Error(`[release-prune] ${message}`); }
function real(pathname) { return path.resolve(pathname); }
function assertSafe(pathname, root) {
  const target = real(pathname);
  const allowedRoot = real(root);
  if (FORBIDDEN_PATH.test(target)) fail("user data path is forbidden");
  if (target !== allowedRoot && !target.startsWith(`${allowedRoot}${path.sep}`)) fail("path escapes approved release root");
  return target;
}
const RECOVERY_MANIFEST = "installed-release-manifest.json";
const KNOWN_NONCANONICAL_DIRECTORIES = new Set([
  "mac-arm64",
  "legacy-candidate",
  "win-unpacked",
  "linux-unpacked",
  "adhoc",
]);
const CURRENT_CANDIDATE_TRANSIENT_METADATA = new Set([
  "builder-effective-config.yaml",
]);
const KNOWN_NONCANONICAL_FILE = /^(?:EchoDesk(?:[.-].+)?\.(?:dmg|zip|exe|AppImage|deb)|latest(?:-mac)?\.yml|.*\.blockmap|builder-debug\.ya?ml)$/;

function finalArchives(releaseRoot) {
  const finalRoot = path.join(releaseRoot, "final");
  if (!fs.existsSync(finalRoot)) fail("final archive directory missing");
  return fs.readdirSync(finalRoot, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".zip"))
    .map((entry) => path.join(finalRoot, entry.name));
}
function assertFinalLayout(releaseRoot) {
  const finalRoot = path.join(releaseRoot, "final");
  const archives = finalArchives(releaseRoot);
  if (archives.length !== 1) fail("exactly one final recovery archive is required");
  const allowed = new Set([path.basename(archives[0]), RECOVERY_MANIFEST]);
  for (const entry of fs.readdirSync(finalRoot, { withFileTypes: true })) {
    if (!entry.isFile() || !allowed.has(entry.name)) {
      fail("unknown final release entry");
    }
  }
  const manifest = path.join(finalRoot, RECOVERY_MANIFEST);
  if (!fs.existsSync(manifest)) fail("installed release manifest missing");
  return { archive: archives[0], manifest };
}
function archiveEntries(archivePath) {
  const result = spawnSync("/usr/bin/unzip", ["-Z1", archivePath], { encoding: "utf8" });
  if (result.status !== 0) return [];
  return String(result.stdout || "").split(/\r?\n/).filter(Boolean);
}
function archiveHash(archivePath, entry) {
  const result = spawnSync("/usr/bin/unzip", ["-p", archivePath, entry], { maxBuffer: 1024 * 1024 * 1024 });
  if (result.status !== 0) return null;
  return createHash("sha256").update(result.stdout).digest("hex");
}
function archiveMatchesInstalled(archivePath, appPath = DEFAULT_APP) {
  const entries = archiveEntries(archivePath);
  const installed = resourceHashes(appPath);
  const suffixes = {
    executable_sha256: "/Contents/MacOS/EchoDesk",
    renderer_sha256: "/Contents/Resources/app.asar",
    backend_sha256: "/Contents/Resources/backend/echodesk-backend",
  };
  return Object.entries(suffixes).every(([field, suffix]) => {
    const matches = entries.filter((entry) => entry.endsWith(suffix));
    return matches.length === 1 && archiveHash(archivePath, matches[0]) === installed[field];
  });
}
function rebuildFinalRecoveryArchive({ releaseRoot = RELEASE_ROOT, appPath = DEFAULT_APP, workspaceRoot = DESKTOP_ROOT, archiveMatches = archiveMatchesInstalled, runner = spawnSync } = {}) {
  const root = assertSafe(releaseRoot, workspaceRoot);
  const { archive } = assertFinalLayout(root);
  if (archiveMatches(archive, appPath)) return { final_archive_count: 1, rebuilt: false };
  const replacement = assertSafe(path.join(root, "final", "EchoDesk-current-recovery.zip"), root);
  const temporary = `${replacement}.${process.pid}.tmp`;
  const created = runner("/usr/bin/ditto", ["-c", "-k", "--sequesterRsrc", "--keepParent", appPath, temporary]);
  if (created.status !== 0 || !fs.existsSync(temporary)) fail("recovery archive creation failed");
  if (!archiveMatches(temporary, appPath)) {
    fs.rmSync(temporary, { force: true });
    fail("recovery archive hash mismatch");
  }
  fs.rmSync(archive, { force: true });
  fs.renameSync(temporary, replacement);
  if (!archiveMatches(replacement, appPath)) fail("recovery archive replacement mismatch");
  return { final_archive_count: 1, rebuilt: true };
}
function knownNoncanonicalEntry(entry) {
  return entry.isDirectory()
    ? KNOWN_NONCANONICAL_DIRECTORIES.has(entry.name)
    : entry.isFile() && KNOWN_NONCANONICAL_FILE.test(entry.name);
}
function currentCandidateTransientEntry(entry) {
  return entry.isFile() && CURRENT_CANDIDATE_TRANSIENT_METADATA.has(entry.name);
}
function verifyStrictMacBundle(appPath, runner = spawnSync) {
  const result = runner("/usr/bin/codesign", ["--verify", "--deep", "--strict", "--verbose=2", appPath], { encoding: "utf8" });
  if (result.status !== 0) fail("installed strict codesign verification failed");
}
function pruneNoncanonicalRelease({
  releaseRoot = RELEASE_ROOT,
  appPath = DEFAULT_APP,
  dryRun = false,
  workspaceRoot = DESKTOP_ROOT,
  manifestPath = DEFAULT_MANIFEST_PATH,
  archiveMatches = archiveMatchesInstalled,
  strictVerifier = verifyStrictMacBundle,
  manifestVerifier = verifyInstalledReleaseManifest,
  candidatePromoted = false,
} = {}) {
  const root = assertSafe(releaseRoot, workspaceRoot);
  if (real(appPath) !== DEFAULT_APP) fail("only canonical installed app is eligible");
  const { archive } = assertFinalLayout(root);
  manifestVerifier({ appPath, manifestPath });
  strictVerifier(appPath);
  if (!archiveMatches(archive, appPath)) fail("recovery archive hash mismatch");
  const removals = [];
  let retainedCurrentCandidateMetadataCount = 0;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (entry.name === "final") continue;
    if (currentCandidateTransientEntry(entry)) {
      if (candidatePromoted) removals.push(assertSafe(path.join(root, entry.name), root));
      else retainedCurrentCandidateMetadataCount += 1;
      continue;
    }
    if (!knownNoncanonicalEntry(entry)) fail("unknown release entry");
    removals.push(assertSafe(path.join(root, entry.name), root));
  }
  if (!dryRun) {
    for (const target of removals) fs.rmSync(target, { recursive: true, force: true });
  }
  return {
    final_archive_count: 1,
    removed_count: removals.length,
    retained_current_candidate_metadata_count: retainedCurrentCandidateMetadataCount,
  };
}
module.exports = {
  archiveMatchesInstalled,
  assertFinalLayout,
  assertSafe,
  currentCandidateTransientEntry,
  knownNoncanonicalEntry,
  pruneNoncanonicalRelease,
  rebuildFinalRecoveryArchive,
  verifyStrictMacBundle,
};
