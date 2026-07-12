"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  abandonUnstartedWorkspaceUploads,
  normalizedWorkspaceRegistry,
  orphanedWorkspaceDocIds,
  prepareWorkspaceUploadsForClear,
  reapOrphanedWorkspaceDocIds,
  shouldRetainWorkspaceFileOnScanFailure,
  withWorkspaceState,
  workspaceDocReferenceCount,
  workspaceProjectionAfterCleanup,
  workspaceProjectionAfterUpload,
  workspaceRendererHandle,
  workspaceStateForOrigin,
} = require("../workspace-registry.cjs");

const ORIGIN_A = "https://workspace-a.example";
const ORIGIN_B = "https://workspace-b.example";

test("schema 2 workspace state migrates only into the active backend origin", () => {
  const registry = normalizedWorkspaceRegistry(
    {
      schema: 2,
      workspaces: ["/knowledge-a", "/knowledge-a"],
      files: {
        "/knowledge-a/brief.md": { doc_id: "doc-a", size: 10 },
      },
      lastScan: { n_indexed: 1 },
    },
    ORIGIN_A,
  );

  assert.deepEqual(workspaceStateForOrigin(registry, ORIGIN_A), {
    workspaces: ["/knowledge-a"],
    files: {
      "/knowledge-a/brief.md": { doc_id: "doc-a", size: 10 },
    },
    doc_ids: ["doc-a"],
    root_identities: {},
    pending_uploads: {},
    lastScan: { n_indexed: 1 },
  });
  assert.deepEqual(workspaceStateForOrigin(registry, ORIGIN_B), {
    workspaces: [],
    files: {},
    doc_ids: [],
    root_identities: {},
    pending_uploads: {},
    lastScan: null,
  });
});

test("origin-scoped state preserves independent doc id cleanup sets", () => {
  let registry = normalizedWorkspaceRegistry(null, ORIGIN_A);
  registry = withWorkspaceState(registry, ORIGIN_A, {
    workspaces: ["/knowledge-a"],
    files: { "/knowledge-a/a.md": { doc_id: "doc-a" } },
    doc_ids: ["doc-a", "orphan-a"],
    lastScan: null,
  });
  registry = withWorkspaceState(registry, ORIGIN_B, {
    workspaces: ["/knowledge-b"],
    files: { "/knowledge-b/b.md": { doc_id: "doc-b" } },
    doc_ids: ["doc-b"],
    lastScan: null,
  });

  assert.deepEqual(
    workspaceStateForOrigin(registry, ORIGIN_A).doc_ids,
    ["doc-a", "orphan-a"],
  );
  assert.deepEqual(
    workspaceStateForOrigin(registry, ORIGIN_B).doc_ids,
    ["doc-b"],
  );
  assert.deepEqual(
    workspaceStateForOrigin(registry, ORIGIN_A).workspaces,
    ["/knowledge-a"],
  );
  assert.deepEqual(
    workspaceStateForOrigin(registry, ORIGIN_B).workspaces,
    ["/knowledge-b"],
  );
});

test("registry drops non-HTTPS and malformed origin buckets", () => {
  const registry = normalizedWorkspaceRegistry(
    {
      schema: 3,
      origins: {
        [ORIGIN_A]: { workspaces: ["/safe"], files: {}, doc_ids: [] },
        "http://workspace-a.example": {
          workspaces: ["/downgraded"],
          files: {},
          doc_ids: ["unsafe"],
        },
        "not a url": {
          workspaces: ["/invalid"],
          files: {},
          doc_ids: ["invalid"],
        },
      },
    },
    ORIGIN_A,
  );

  assert.deepEqual(Object.keys(registry.origins), [ORIGIN_A]);
  assert.deepEqual(workspaceStateForOrigin(registry, ORIGIN_A).workspaces, [
    "/safe",
  ]);
});

test("orphan cleanup retains failed doc ids and retries them on the next scan", async () => {
  const state = {
    workspaces: ["/knowledge-a"],
    files: {
      "/knowledge-a/current.md": { doc_id: "doc-current", size: 20 },
    },
    doc_ids: ["doc-current", "doc-stale"],
    lastScan: null,
  };
  assert.deepEqual(orphanedWorkspaceDocIds(state), ["doc-stale"]);

  const attempts = [];
  const failed = await reapOrphanedWorkspaceDocIds(state, {
    deleteDoc: async (docId) => {
      attempts.push(docId);
      throw new Error("temporary delete failure");
    },
  });
  assert.deepEqual(attempts, ["doc-stale"]);
  assert.deepEqual(failed.remainingDocIds, ["doc-current", "doc-stale"]);
  assert.deepEqual(failed.deletedDocIds, []);
  assert.deepEqual(
    failed.failures.map(({ docId, error }) => [docId, error.message]),
    [["doc-stale", "temporary delete failure"]],
  );

  const retried = await reapOrphanedWorkspaceDocIds(
    { ...state, doc_ids: failed.remainingDocIds },
    {
      deleteDoc: async (docId) => {
        attempts.push(docId);
      },
    },
  );
  assert.deepEqual(attempts, ["doc-stale", "doc-stale"]);
  assert.deepEqual(retried.remainingDocIds, ["doc-current"]);
  assert.deepEqual(retried.deletedDocIds, ["doc-stale"]);
  assert.deepEqual(retried.failures, []);
});

test("shared remote doc ids are reference-counted across local files", () => {
  const files = {
    "/knowledge-a/one.md": { doc_id: "doc-shared" },
    "/knowledge-a/two.md": { doc_id: "doc-shared" },
    "/knowledge-a/three.md": { doc_id: "doc-other" },
  };
  assert.equal(workspaceDocReferenceCount(files, "doc-shared"), 2);
  assert.equal(workspaceDocReferenceCount(files, "doc-other"), 1);
  assert.equal(workspaceDocReferenceCount(files, "missing"), 0);

  const withoutOne = { ...files };
  delete withoutOne["/knowledge-a/one.md"];
  assert.equal(workspaceDocReferenceCount(withoutOne, "doc-shared"), 1);
  assert.deepEqual(
    orphanedWorkspaceDocIds({ files: withoutOne, doc_ids: ["doc-shared"] }),
    [],
  );

  delete withoutOne["/knowledge-a/two.md"];
  assert.equal(workspaceDocReferenceCount(withoutOne, "doc-shared"), 0);
  assert.deepEqual(
    orphanedWorkspaceDocIds({ files: withoutOne, doc_ids: ["doc-shared"] }),
    ["doc-shared"],
  );
});

test("renderer workspace handles are origin-bound and never contain the absolute path", () => {
  const secret = Buffer.alloc(32, 7);
  const absolutePath = "/Users/alice/Private/Client Knowledge";
  const handleA = workspaceRendererHandle(ORIGIN_A, absolutePath, secret);
  const repeatedA = workspaceRendererHandle(ORIGIN_A, absolutePath, secret);
  const handleB = workspaceRendererHandle(ORIGIN_B, absolutePath, secret);

  assert.equal(handleA, repeatedA);
  assert.notEqual(handleA, handleB);
  assert.match(handleA, /^Client Knowledge \[ws:[a-f0-9]{16}\]$/);
  assert.doesNotMatch(handleA, /\/Users\/alice|Private/);
});

test("scan failures retain old mappings only inside configured failed subtrees", () => {
  const configuredRoots = ["/knowledge/current"];
  const failedPaths = ["/knowledge/current/unmounted"];

  assert.equal(
    shouldRetainWorkspaceFileOnScanFailure(
      "/knowledge/current/unmounted/brief.md",
      configuredRoots,
      failedPaths,
    ),
    true,
  );
  assert.equal(
    shouldRetainWorkspaceFileOnScanFailure(
      "/knowledge/current/deleted.md",
      configuredRoots,
      failedPaths,
    ),
    false,
  );
  assert.equal(
    shouldRetainWorkspaceFileOnScanFailure(
      "/knowledge/removed-root/brief.md",
      configuredRoots,
      ["/knowledge/removed-root"],
    ),
    false,
    "a root removed from configuration must not be retained by a stale failure marker",
  );
});

test("pre-upload intent survives normalization before any remote side effect", () => {
  const sourcePath = "/knowledge/current/brief.md";
  const pending = {
    snapshot_path: "/tmp/echodesk-workspace-scan-crash/brief.snapshot",
    sha256: "a".repeat(64),
    size: 42,
    mtime: 1234,
    file_name: "brief.md",
    title: "brief",
    previous_doc_id: "doc-old",
    uploaded_doc_id: "",
    upload_started_at: null,
    clear_requested: false,
    queued_at: 5678,
  };
  const state = workspaceStateForOrigin(
    withWorkspaceState(
      normalizedWorkspaceRegistry(null, ORIGIN_A),
      ORIGIN_A,
      {
        workspaces: ["/knowledge/current"],
        files: { [sourcePath]: { doc_id: "doc-old" } },
        doc_ids: ["doc-old"],
        pending_uploads: { [sourcePath]: pending },
      },
    ),
    ORIGIN_A,
  );

  assert.deepEqual(state.pending_uploads[sourcePath], pending);
  assert.equal(state.files[sourcePath].doc_id, "doc-old");
  assert.deepEqual(state.doc_ids, ["doc-old"]);
});

test("crash projections preserve both ids before cleanup and converge after retry", () => {
  const sourcePath = "/knowledge/current/brief.md";
  const pending = {
    snapshot_path: "/tmp/echodesk-workspace-scan-crash/brief.snapshot",
    sha256: "b".repeat(64),
    size: 84,
    mtime: 4321,
    file_name: "brief.md",
    title: "brief",
    previous_doc_id: "doc-old",
    uploaded_doc_id: "",
    upload_started_at: 8766,
    clear_requested: false,
    queued_at: 8765,
  };
  const before = {
    workspaces: ["/knowledge/current"],
    files: { [sourcePath]: { doc_id: "doc-old" } },
    doc_ids: ["doc-old"],
    pending_uploads: { [sourcePath]: pending },
  };

  const durableAfterUpload = workspaceProjectionAfterUpload(
    before,
    sourcePath,
    pending,
    { doc_id: "doc-new", title: "New brief" },
  );
  assert.equal(durableAfterUpload.files[sourcePath].doc_id, "doc-new");
  assert.equal(
    durableAfterUpload.pending_uploads[sourcePath].uploaded_doc_id,
    "doc-new",
  );
  assert.deepEqual(durableAfterUpload.doc_ids, ["doc-new", "doc-old"]);

  const converged = workspaceProjectionAfterCleanup(durableAfterUpload, sourcePath, {
    previousDeleted: true,
  });
  assert.equal(converged.files[sourcePath].doc_id, "doc-new");
  assert.deepEqual(converged.pending_uploads, {});
  assert.deepEqual(converged.doc_ids, ["doc-new"]);
});

test("clear can abandon an intent proven not to have started network upload", () => {
  const unstarted = {
    snapshot_path: "/tmp/echodesk-workspace-scan-clear/unstarted.snapshot",
    sha256: "c".repeat(64),
    size: 1,
    mtime: 1,
    file_name: "clear.md",
    title: "clear",
    previous_doc_id: "doc-old",
    uploaded_doc_id: "",
    upload_started_at: null,
    clear_requested: false,
    queued_at: 1,
  };
  const ambiguous = {
    ...unstarted,
    snapshot_path: "/tmp/echodesk-workspace-scan-clear/ambiguous.snapshot",
    file_name: "ambiguous.md",
    upload_started_at: 2,
  };
  const { state, abandonedSnapshotPaths } = abandonUnstartedWorkspaceUploads({
    files: { "/knowledge/clear.md": { doc_id: "doc-old" } },
    doc_ids: ["doc-old"],
    pending_uploads: {
      "/knowledge/clear.md": unstarted,
      "/knowledge/ambiguous.md": ambiguous,
    },
  });

  assert.deepEqual(abandonedSnapshotPaths, [unstarted.snapshot_path]);
  assert.deepEqual(Object.keys(state.pending_uploads), ["/knowledge/ambiguous.md"]);
  assert.equal(state.files["/knowledge/clear.md"].doc_id, "doc-old");
});

test("clear tombstones an ambiguous upload without blocking on immediate recovery", () => {
  const ambiguous = {
    snapshot_path: "/tmp/echodesk-workspace-scan-clear/ambiguous.snapshot",
    sha256: "d".repeat(64),
    size: 2,
    mtime: 2,
    file_name: "ambiguous.md",
    title: "ambiguous",
    previous_doc_id: "doc-old",
    uploaded_doc_id: "",
    upload_started_at: 2,
    clear_requested: false,
    queued_at: 1,
  };
  const prepared = prepareWorkspaceUploadsForClear({
    files: { "/knowledge/ambiguous.md": { doc_id: "doc-old" } },
    doc_ids: ["doc-old"],
    pending_uploads: { "/knowledge/ambiguous.md": ambiguous },
  });

  assert.deepEqual(prepared.abandonedSnapshotPaths, []);
  assert.equal(
    prepared.state.pending_uploads["/knowledge/ambiguous.md"].clear_requested,
    true,
  );
  assert.equal(
    prepared.state.files["/knowledge/ambiguous.md"].doc_id,
    "doc-old",
  );
});
