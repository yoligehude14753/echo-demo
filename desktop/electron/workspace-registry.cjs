"use strict";

const crypto = require("node:crypto");
const path = require("node:path");

const { normalizedPublicOrigin } = require("./workspace-backend-transport.cjs");

const WORKSPACE_REGISTRY_SCHEMA = 3;

function emptyWorkspaceState() {
  return {
    workspaces: [],
    files: {},
    doc_ids: [],
    root_identities: {},
    pending_uploads: {},
    lastScan: null,
  };
}

function normalizedPendingUploads(raw) {
  const pending = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return pending;
  for (const [sourcePath, candidate] of Object.entries(raw)) {
    if (
      !path.isAbsolute(sourcePath) ||
      !candidate ||
      typeof candidate !== "object" ||
      Array.isArray(candidate) ||
      !path.isAbsolute(String(candidate.snapshot_path || "")) ||
      !/^[a-f0-9]{64}$/.test(String(candidate.sha256 || "")) ||
      !Number.isSafeInteger(candidate.size) ||
      candidate.size < 0 ||
      !Number.isFinite(candidate.mtime) ||
      typeof candidate.file_name !== "string" ||
      !candidate.file_name ||
      path.basename(candidate.file_name) !== candidate.file_name
    ) {
      continue;
    }
    const previousDocId =
      typeof candidate.previous_doc_id === "string"
        ? candidate.previous_doc_id.trim().slice(0, 256)
        : "";
    const uploadedDocId =
      typeof candidate.uploaded_doc_id === "string"
        ? candidate.uploaded_doc_id.trim().slice(0, 256)
        : "";
    pending[path.resolve(sourcePath)] = {
      snapshot_path: path.resolve(candidate.snapshot_path),
      sha256: candidate.sha256,
      size: candidate.size,
      mtime: candidate.mtime,
      file_name: candidate.file_name.slice(0, 255),
      title:
        typeof candidate.title === "string" && candidate.title.trim()
          ? candidate.title.trim().slice(0, 512)
          : candidate.file_name.slice(0, 255),
      previous_doc_id: previousDocId,
      uploaded_doc_id: uploadedDocId,
      upload_started_at: Number.isFinite(candidate.upload_started_at)
        ? candidate.upload_started_at
        : null,
      clear_requested: candidate.clear_requested === true,
      queued_at: Number.isFinite(candidate.queued_at)
        ? candidate.queued_at
        : Date.now(),
    };
  }
  return pending;
}

function normalizedWorkspaceState(raw) {
  const candidate = raw && typeof raw === "object" ? raw : {};
  const workspaces = Array.isArray(candidate.workspaces)
    ? candidate.workspaces.filter((value) => typeof value === "string" && value.trim())
    : [];
  const files =
    candidate.files &&
    typeof candidate.files === "object" &&
    !Array.isArray(candidate.files)
      ? { ...candidate.files }
      : {};
  const docIds = new Set(
    Array.isArray(candidate.doc_ids)
      ? candidate.doc_ids.filter((value) => typeof value === "string" && value.trim())
      : [],
  );
  for (const metadata of Object.values(files)) {
    if (typeof metadata?.doc_id === "string" && metadata.doc_id.trim()) {
      docIds.add(metadata.doc_id.trim());
    }
  }
  const pendingUploads = normalizedPendingUploads(candidate.pending_uploads);
  const rootIdentities = {};
  if (
    candidate.root_identities &&
    typeof candidate.root_identities === "object" &&
    !Array.isArray(candidate.root_identities)
  ) {
    for (const [root, identity] of Object.entries(candidate.root_identities)) {
      if (
        path.isAbsolute(root) &&
        identity &&
        typeof identity === "object" &&
        /^\d+$/.test(String(identity.dev || "")) &&
        /^\d+$/.test(String(identity.ino || ""))
      ) {
        rootIdentities[path.resolve(root)] = {
          dev: String(identity.dev),
          ino: String(identity.ino),
        };
      }
    }
  }
  for (const pending of Object.values(pendingUploads)) {
    if (pending.previous_doc_id) docIds.add(pending.previous_doc_id);
    if (pending.uploaded_doc_id) docIds.add(pending.uploaded_doc_id);
  }
  return {
    workspaces: Array.from(new Set(workspaces)),
    files,
    doc_ids: Array.from(docIds).sort(),
    root_identities: rootIdentities,
    pending_uploads: pendingUploads,
    lastScan:
      candidate.lastScan && typeof candidate.lastScan === "object"
        ? { ...candidate.lastScan }
        : null,
  };
}

function workspaceProjectionAfterUpload(
  rawState,
  sourcePath,
  rawPending,
  uploaded,
) {
  const state = normalizedWorkspaceState(rawState);
  const pending = normalizedPendingUploads({ [sourcePath]: rawPending })[
    path.resolve(sourcePath)
  ];
  const uploadedDocId =
    typeof uploaded?.doc_id === "string" ? uploaded.doc_id.trim().slice(0, 256) : "";
  if (!pending || !uploadedDocId) {
    throw new TypeError("workspace upload projection is invalid");
  }
  const normalizedSource = path.resolve(sourcePath);
  const pendingUploads = {
    ...state.pending_uploads,
    [normalizedSource]: {
      ...pending,
      title:
        typeof uploaded?.title === "string" && uploaded.title.trim()
          ? uploaded.title.trim().slice(0, 512)
          : pending.title,
      uploaded_doc_id: uploadedDocId,
      upload_started_at: pending.upload_started_at,
    },
  };
  const docIds = new Set(state.doc_ids);
  docIds.add(uploadedDocId);
  if (pending.previous_doc_id) docIds.add(pending.previous_doc_id);
  return {
    ...state,
    files: {
      ...state.files,
      [normalizedSource]: {
        doc_id: uploadedDocId,
        title: pendingUploads[normalizedSource].title,
        size: pending.size,
        mtime: pending.mtime,
        sha256: pending.sha256,
        ingested_at: Date.now(),
      },
    },
    doc_ids: Array.from(docIds).sort(),
    pending_uploads: pendingUploads,
  };
}

function workspaceProjectionAfterCleanup(
  rawState,
  sourcePath,
  { previousDeleted = false } = {},
) {
  const state = normalizedWorkspaceState(rawState);
  const normalizedSource = path.resolve(sourcePath);
  const pending = state.pending_uploads[normalizedSource];
  const pendingUploads = { ...state.pending_uploads };
  delete pendingUploads[normalizedSource];
  const docIds = new Set(state.doc_ids);
  if (
    previousDeleted &&
    pending?.previous_doc_id &&
    workspaceDocReferenceCount(state.files, pending.previous_doc_id) === 0
  ) {
    docIds.delete(pending.previous_doc_id);
  }
  return {
    ...state,
    doc_ids: Array.from(docIds).sort(),
    pending_uploads: pendingUploads,
  };
}

function abandonUnstartedWorkspaceUploads(rawState) {
  const state = normalizedWorkspaceState(rawState);
  const pendingUploads = { ...state.pending_uploads };
  const abandonedSnapshotPaths = [];
  for (const [sourcePath, pending] of Object.entries(state.pending_uploads)) {
    if (pending.uploaded_doc_id || pending.upload_started_at !== null) continue;
    abandonedSnapshotPaths.push(pending.snapshot_path);
    delete pendingUploads[sourcePath];
  }
  return {
    state: { ...state, pending_uploads: pendingUploads },
    abandonedSnapshotPaths,
  };
}

function prepareWorkspaceUploadsForClear(rawState) {
  const abandoned = abandonUnstartedWorkspaceUploads(rawState);
  const pendingUploads = {};
  for (const [sourcePath, pending] of Object.entries(
    abandoned.state.pending_uploads,
  )) {
    pendingUploads[sourcePath] = { ...pending, clear_requested: true };
  }
  return {
    state: { ...abandoned.state, pending_uploads: pendingUploads },
    abandonedSnapshotPaths: abandoned.abandonedSnapshotPaths,
  };
}

function workspacePendingSnapshotDirectories(rawState) {
  const state = normalizedWorkspaceState(rawState);
  return Array.from(
    new Set(
      Object.values(state.pending_uploads).map((pending) =>
        path.dirname(pending.snapshot_path),
      ),
    ),
  );
}

function workspaceRegistryPendingSnapshotDirectories(registry) {
  const directories = new Set();
  for (const state of Object.values(registry?.origins || {})) {
    for (const directory of workspacePendingSnapshotDirectories(state)) {
      directories.add(directory);
    }
  }
  return Array.from(directories);
}

function workspaceDocReferenceCount(rawFiles, rawDocId) {
  const docId = typeof rawDocId === "string" ? rawDocId.trim() : "";
  if (!docId) return 0;
  const files =
    rawFiles && typeof rawFiles === "object" && !Array.isArray(rawFiles)
      ? rawFiles
      : {};
  let count = 0;
  for (const metadata of Object.values(files)) {
    if (typeof metadata?.doc_id === "string" && metadata.doc_id.trim() === docId) {
      count += 1;
    }
  }
  return count;
}

function workspaceRendererHandle(rawOrigin, rawPath, secret) {
  const origin = normalizedPublicOrigin(rawOrigin, "workspace handle");
  const absolutePath = String(rawPath || "");
  if (!path.isAbsolute(absolutePath)) {
    throw new TypeError("workspace handle requires an absolute main-process path");
  }
  if (!Buffer.isBuffer(secret) || secret.byteLength < 16) {
    throw new TypeError("workspace handle requires a process-private secret");
  }
  const normalizedPath = path.normalize(absolutePath);
  const id = crypto
    .createHmac("sha256", secret)
    .update(origin)
    .update("\0")
    .update(normalizedPath)
    .digest("hex")
    .slice(0, 16);
  const label = (path.basename(normalizedPath) || "工作区")
    .replace(/[\r\n[\]]/g, " ")
    .trim()
    .slice(0, 80) || "工作区";
  return `${label} [ws:${id}]`;
}

function workspacePathContains(rawRoot, rawTarget) {
  const root = path.resolve(String(rawRoot || ""));
  const target = path.resolve(String(rawTarget || ""));
  const relative = path.relative(root, target);
  return (
    relative === "" ||
    (relative !== ".." &&
      !relative.startsWith(`..${path.sep}`) &&
      !path.isAbsolute(relative))
  );
}

function shouldRetainWorkspaceFileOnScanFailure(
  sourcePath,
  configuredRoots,
  failedPaths,
) {
  const stillConfigured = (configuredRoots || []).some((root) =>
    workspacePathContains(root, sourcePath),
  );
  if (!stillConfigured) return false;
  return (failedPaths || []).some((failedPath) =>
    workspacePathContains(failedPath, sourcePath),
  );
}

function orphanedWorkspaceDocIds(rawState) {
  const state = normalizedWorkspaceState(rawState);
  const referenced = new Set();
  for (const metadata of Object.values(state.files)) {
    if (typeof metadata?.doc_id === "string" && metadata.doc_id.trim()) {
      referenced.add(metadata.doc_id.trim());
    }
  }
  return state.doc_ids.filter((docId) => !referenced.has(docId));
}

function throwIfReapAborted(signal) {
  if (!signal?.aborted) return;
  if (signal.reason instanceof Error) throw signal.reason;
  throw new DOMException("workspace orphan cleanup cancelled", "AbortError");
}

async function reapOrphanedWorkspaceDocIds(
  rawState,
  { deleteDoc, signal = undefined } = {},
) {
  if (typeof deleteDoc !== "function") {
    throw new TypeError("workspace orphan cleanup requires deleteDoc");
  }
  const state = normalizedWorkspaceState(rawState);
  const remaining = new Set(state.doc_ids);
  const deletedDocIds = [];
  const failures = [];
  for (const docId of orphanedWorkspaceDocIds(state)) {
    throwIfReapAborted(signal);
    try {
      await deleteDoc(docId);
      remaining.delete(docId);
      deletedDocIds.push(docId);
    } catch (error) {
      throwIfReapAborted(signal);
      failures.push({ docId, error });
    }
  }
  throwIfReapAborted(signal);
  return {
    remainingDocIds: Array.from(remaining).sort(),
    deletedDocIds,
    failures,
  };
}

function normalizedWorkspaceRegistry(raw, legacyOrigin) {
  const origins = {};
  if (
    raw?.schema === WORKSPACE_REGISTRY_SCHEMA &&
    raw.origins &&
    typeof raw.origins === "object" &&
    !Array.isArray(raw.origins)
  ) {
    for (const [rawOrigin, state] of Object.entries(raw.origins)) {
      try {
        const origin = normalizedPublicOrigin(rawOrigin, "workspace registry");
        origins[origin] = normalizedWorkspaceState(state);
      } catch {
        // Invalid or downgraded origins are never made available to workspace IPC.
      }
    }
  } else if (raw && typeof raw === "object") {
    const origin = normalizedPublicOrigin(legacyOrigin, "legacy workspace backend");
    origins[origin] = normalizedWorkspaceState(raw);
  }
  return { schema: WORKSPACE_REGISTRY_SCHEMA, origins };
}

function workspaceStateForOrigin(registry, rawOrigin) {
  const origin = normalizedPublicOrigin(rawOrigin, "workspace backend");
  return normalizedWorkspaceState(registry?.origins?.[origin]);
}

function withWorkspaceState(registry, rawOrigin, state) {
  const origin = normalizedPublicOrigin(rawOrigin, "workspace backend");
  return {
    schema: WORKSPACE_REGISTRY_SCHEMA,
    origins: {
      ...(registry?.origins || {}),
      [origin]: normalizedWorkspaceState(state),
    },
  };
}

module.exports = {
  WORKSPACE_REGISTRY_SCHEMA,
  abandonUnstartedWorkspaceUploads,
  emptyWorkspaceState,
  normalizedWorkspaceRegistry,
  normalizedWorkspaceState,
  orphanedWorkspaceDocIds,
  reapOrphanedWorkspaceDocIds,
  prepareWorkspaceUploadsForClear,
  shouldRetainWorkspaceFileOnScanFailure,
  withWorkspaceState,
  workspaceDocReferenceCount,
  workspacePendingSnapshotDirectories,
  workspaceProjectionAfterCleanup,
  workspaceProjectionAfterUpload,
  workspaceRegistryPendingSnapshotDirectories,
  workspaceRendererHandle,
  workspaceStateForOrigin,
};
