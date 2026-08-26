const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  assertSafe,
  currentCandidateTransientEntry,
  knownNoncanonicalEntry,
  pruneNoncanonicalRelease,
  rebuildFinalRecoveryArchive,
} = require("./prune-noncanonical-release.cjs");

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-release-prune-"));
  fs.mkdirSync(path.join(root, "final"), { recursive: true });
  fs.writeFileSync(path.join(root, "final", "EchoDesk-final.zip"), "archive");
  fs.writeFileSync(path.join(root, "final", "installed-release-manifest.json"), "{}");
  fs.mkdirSync(path.join(root, "mac-arm64"));
  fs.writeFileSync(path.join(root, "EchoDesk-0.3.5-win-x64.zip"), "candidate");
  fs.writeFileSync(path.join(root, "builder-effective-config.yaml"), "candidate metadata");
  return root;
}
test("prune keeps exactly one final archive and removes only release artifacts", () => {
  const root = fixture();
  const result = pruneNoncanonicalRelease({
    releaseRoot: root,
    workspaceRoot: root,
    archiveMatches: () => true,
    manifestVerifier: () => {},
    strictVerifier: () => {},
  });
  assert.deepEqual(result, {
    final_archive_count: 1,
    removed_count: 2,
    retained_current_candidate_metadata_count: 1,
  });
  assert.equal(fs.existsSync(path.join(root, "final", "EchoDesk-final.zip")), true);
  assert.equal(fs.existsSync(path.join(root, "mac-arm64")), false);
  assert.equal(fs.existsSync(path.join(root, "builder-effective-config.yaml")), true);
});
test("prune rejects user data roots, unknown artifacts, and ambiguous recovery archives", () => {
  assert.throws(() => assertSafe("/Users/a/Library/Application Support/EchoDesk", "/Users/a"), /user data/);
  const root = fixture();
  fs.writeFileSync(path.join(root, "final", "second.zip"), "archive");
  assert.throws(() => pruneNoncanonicalRelease({ releaseRoot: root, workspaceRoot: root }), /exactly one final/);
  const unknown = fixture();
  fs.writeFileSync(path.join(unknown, "unowned.txt"), "unknown");
  assert.throws(
    () => pruneNoncanonicalRelease({
      releaseRoot: unknown,
      workspaceRoot: unknown,
      archiveMatches: () => true,
      manifestVerifier: () => {},
      strictVerifier: () => {},
    }),
    /unknown release entry/,
  );
  const similarYaml = fixture();
  fs.writeFileSync(path.join(similarYaml, "builder-effective-config.yml"), "not the fixed candidate metadata");
  assert.throws(
    () => pruneNoncanonicalRelease({
      releaseRoot: similarYaml,
      workspaceRoot: similarYaml,
      archiveMatches: () => true,
      manifestVerifier: () => {},
      strictVerifier: () => {},
    }),
    /unknown release entry/,
  );
  assert.equal(knownNoncanonicalEntry({ isDirectory: () => true, isFile: () => false, name: "mac-arm64" }), true);
  assert.equal(knownNoncanonicalEntry({ isDirectory: () => true, isFile: () => false, name: "legacy-candidate" }), true);
  assert.equal(knownNoncanonicalEntry({ isDirectory: () => false, isFile: () => true, name: "builder-debug.yml" }), true);
  assert.equal(currentCandidateTransientEntry({ isDirectory: () => false, isFile: () => true, name: "builder-effective-config.yaml" }), true);
  assert.equal(currentCandidateTransientEntry({ isDirectory: () => false, isFile: () => true, name: "builder-effective-config.yml" }), false);
  assert.equal(knownNoncanonicalEntry({ isDirectory: () => false, isFile: () => true, name: "unowned.txt" }), false);
});
test("candidate metadata is retained before promotion and removed only after canonical promotion", () => {
  const root = fixture();
  const common = {
    releaseRoot: root,
    workspaceRoot: root,
    archiveMatches: () => true,
    manifestVerifier: () => {},
    strictVerifier: () => {},
  };
  const beforePromotion = pruneNoncanonicalRelease({ ...common });
  assert.equal(beforePromotion.retained_current_candidate_metadata_count, 1);
  assert.equal(fs.existsSync(path.join(root, "builder-effective-config.yaml")), true);

  const afterPromotion = pruneNoncanonicalRelease({ ...common, candidatePromoted: true });
  assert.deepEqual(afterPromotion, {
    final_archive_count: 1,
    removed_count: 1,
    retained_current_candidate_metadata_count: 0,
  });
  assert.equal(fs.existsSync(path.join(root, "builder-effective-config.yaml")), false);
});
test("stale final archive is deleted and rebuilt while a matching final archive is retained", () => {
  const root = fixture();
  const calls = [];
  const runner = (_command, _args) => { calls.push(true); fs.writeFileSync(_args.at(-1), "replacement"); return { status: 0 }; };
  let checks = 0;
  const stale = rebuildFinalRecoveryArchive({ releaseRoot: root, workspaceRoot: root, archiveMatches: () => ++checks > 1, runner });
  assert.deepEqual(stale, { final_archive_count: 1, rebuilt: true });
  assert.equal(calls.length, 1);
  const retained = rebuildFinalRecoveryArchive({ releaseRoot: root, workspaceRoot: root, archiveMatches: () => true, runner });
  assert.deepEqual(retained, { final_archive_count: 1, rebuilt: false });
});
