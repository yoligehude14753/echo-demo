import assert from "node:assert/strict";
import test from "node:test";
import {
  advanceLegacyHistoryPage,
  beginLegacyHistorySync,
  clearPairing,
  completeSyncOperation,
  enqueueSyncOperation,
  ensureSyncDeviceId,
  failSyncOperation,
  isSyncHistoryManifestCovered,
  isSyncStateReady,
  knownSyncEntityRevision,
  loadSyncState,
  makeOperationId,
  markSyncOperationSending,
  pendingSyncOperations,
  projectSyncStatus,
  rememberSyncEntityRevision,
  resetSyncStateForTest,
  setPairingState,
  setLegacyHistoryPage,
  updateSyncState,
  type SyncStorage,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncState.ts";

function zeroHistoryManifest(cursor = "0") {
  return {
    cursor,
    entity_counts: {
      meeting: 0,
      transcript_segment: 0,
      meeting_summary: 0,
      artifact: 0,
    },
  };
}

class MemoryStorage implements SyncStorage {
  private values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

function transcriptItem(deviceId: string, operationId: string) {
  return {
    operation_id: operationId,
    device_id: deviceId,
    entity_type: "transcript_segment" as const,
    entity_id: "meeting-1:0:1000",
    base_revision: 3,
    updated_at: "2026-07-14T12:00:00.000Z",
    payload: { meeting_id: "meeting-1", text: "hello", start_ms: 0, end_ms: 1000 },
  };
}

test("first startup creates a durable random device id and restart reuses it", () => {
  const storage = new MemoryStorage();
  const first = ensureSyncDeviceId(storage);
  const second = ensureSyncDeviceId(storage);

  assert.match(first, /^device-[0-9a-f-]{32,36}$/);
  assert.equal(second, first);
  assert.equal(loadSyncState(storage).device_id, first);
  assert.equal(loadSyncState(storage).schema, 2);
});

test("sync operation ids stay outside the capture idempotency namespace", () => {
  const operationId = makeOperationId("transcript_segment", "meeting-1:0:1000");

  assert.match(operationId, /^transcript_segment:meeting-1:0:1000:/);
  assert.equal(operationId.startsWith("capture:"), false);
});

test("capture idempotency keys cannot enter the sync outbox", () => {
  const storage = new MemoryStorage();
  const deviceId = ensureSyncDeviceId(storage);

  assert.throws(
    () =>
      enqueueSyncOperation(
        {
          operation_id: "capture:g1:7",
          device_id: deviceId,
          entity_type: "transcript_segment",
          entity_id: "meeting-1:0:1000",
          base_revision: 0,
          updated_at: "2026-07-14T12:00:00.000Z",
          payload: { meeting_id: "meeting-1", text: "hello" },
        },
        storage,
      ),
    /capture.*sync outbox/,
  );
  assert.equal(loadSyncState(storage).outbox.length, 0);
});

test("pairing starts full-history pagination at zero and unpair keeps outbox", () => {
  const storage = new MemoryStorage();
  const deviceId = ensureSyncDeviceId(storage);
  enqueueSyncOperation(transcriptItem(deviceId, "op-1"), storage);

  setPairingState(
    { device_id: "hub-device-1", sync_token: "sync-token", cursor: "cursor-7" },
    storage,
  );
  const paired = loadSyncState(storage);
  assert.equal(paired.device_id, "hub-device-1");
  assert.equal(paired.sync_token, "sync-token");
  assert.equal(paired.cursor, "0");
  assert.equal(paired.status, "syncing");
  assert.equal(paired.snapshot_complete, false);
  assert.equal(paired.history_manifest, null);
  assert.equal(paired.outbox[0]?.device_id, "hub-device-1");

  updateSyncState(
    (state) => ({
      ...state,
      snapshot_complete: true,
      history_manifest: zeroHistoryManifest(),
    }),
    storage,
  );

  const restarted = loadSyncState(storage);
  assert.equal(restarted.sync_token, "sync-token");
  assert.equal(restarted.cursor, "0");
  assert.equal(restarted.snapshot_complete, true);
  clearPairing(storage);
  const unpaired = loadSyncState(storage);
  assert.equal(unpaired.status, "unpaired");
  assert.equal(unpaired.sync_token, null);
  assert.equal(unpaired.snapshot_complete, false);
  assert.equal(unpaired.outbox.length, 1);
});

test("canonical numeric zero cursor stays in the string state representation", () => {
  const storage = new MemoryStorage();
  const state = setPairingState({ sync_token: "sync-token", cursor: 0 }, storage);
  assert.equal(state.cursor, "0");
  assert.equal(typeof state.cursor, "string");
  assert.equal(loadSyncState(storage).cursor, "0");
});

test("legacy paired state without a history marker restarts from zero", () => {
  const storage = new MemoryStorage();
  storage.setItem(
    "echodesk.syncState.v1",
    JSON.stringify({
      schema: 1,
      device_id: "device-legacy",
      device_name: "EchoDesk",
      platform: "web",
      sync_token: "legacy-token",
      cursor: "20992",
      status: "synced",
      last_error: null,
      last_synced_at: null,
      outbox: [],
      canonical_revisions: { "transcript_segment:stale": 9 },
      snapshot_complete: true,
      legacy_history: null,
    }),
  );

  const migrated = loadSyncState(storage);
  assert.equal(migrated.cursor, "0");
  assert.equal(migrated.status, "syncing");
  assert.equal(migrated.snapshot_complete, false);
  assert.equal(migrated.history_manifest, null);
  assert.deepEqual(migrated.canonical_revisions, {});
  assert.equal(migrated.schema, 2);
  assert.equal(
    JSON.parse(storage.getItem("echodesk.syncState.v1") ?? "null").schema,
    2,
  );
});

test("four-class manifest is required before history can be marked complete", () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "sync-token", cursor: 4 }, storage);
  const manifest = {
    cursor: "4",
    entity_counts: {
      meeting: 1,
      transcript_segment: 1,
      meeting_summary: 1,
      artifact: 1,
    },
  };
  const incomplete = updateSyncState(
    (state) => ({
      ...state,
      cursor: "4",
      history_manifest: manifest,
      canonical_revisions: {
        "meeting:m1": 1,
        "transcript_segment:s1": 1,
        "meeting_summary:m1": 1,
      },
    }),
    storage,
  );
  assert.equal(isSyncHistoryManifestCovered(incomplete), false);
  assert.equal(isSyncStateReady(incomplete), false);

  const complete = updateSyncState(
    (state) => ({
      ...state,
      canonical_revisions: {
        ...state.canonical_revisions,
        "artifact:a1": 1,
      },
    }),
    storage,
  );
  assert.equal(isSyncHistoryManifestCovered(complete), true);
});

test("host pairing cannot override failed or in-progress renderer sync", () => {
  const storage = new MemoryStorage();
  const syncing = setPairingState({ sync_token: "sync-token", cursor: 0 }, storage);
  assert.equal(projectSyncStatus(syncing, true), "syncing");
  const failed = updateSyncState(
    (state) => ({ ...state, status: "failed", last_error: "network" }),
    storage,
  );
  assert.equal(projectSyncStatus(failed, true), "failed");
});

test("legacy history manifest advances every non-empty class before complete", () => {
  const storage = new MemoryStorage();
  beginLegacyHistorySync(
    {
      fingerprint: "aggregate-fingerprint",
      meeting_count: 1,
      segment_count: 1,
      summary_count: 1,
      artifact_count: 1,
    },
    storage,
  );
  for (const phase of ["meetings", "segments", "summaries", "artifacts"] as const) {
    const before = loadSyncState(storage).legacy_history;
    assert.equal(before?.phase, phase);
    setLegacyHistoryPage(
      {
        fingerprint: "aggregate-fingerprint",
        phase,
        offset: 0,
        next_offset: 1,
        done: true,
      },
      storage,
    );
    advanceLegacyHistoryPage("aggregate-fingerprint", phase, 1, storage);
  }
  assert.equal(loadSyncState(storage).legacy_history?.phase, "complete");
});

test("completed legacy phase repairs a stale scan-complete projection", () => {
  const storage = new MemoryStorage();
  const state = beginLegacyHistorySync(
    {
      fingerprint: "completed-projection",
      meeting_count: 0,
      segment_count: 0,
      summary_count: 0,
      artifact_count: 0,
    },
    storage,
  );
  storage.setItem(
    "echodesk.syncState.v1",
    JSON.stringify({
      ...state,
      legacy_scan_complete: false,
      legacy_history: { ...state.legacy_history, phase: "complete" },
    }),
  );
  const repaired = loadSyncState(storage);
  assert.equal(repaired.legacy_history?.phase, "complete");
  assert.equal(repaired.legacy_scan_complete, true);
});

test("remote canonical revisions survive reload and older revisions do not overwrite them", () => {
  const storage = new MemoryStorage();
  rememberSyncEntityRevision("transcript_segment", "meeting-1:0:1000", 7, storage);
  rememberSyncEntityRevision("transcript_segment", "meeting-1:0:1000", 3, storage);

  assert.equal(
    knownSyncEntityRevision("transcript_segment", "meeting-1:0:1000", storage),
    7,
  );
  assert.equal(loadSyncState(storage).canonical_revisions["transcript_segment:meeting-1:0:1000"], 7);
});

test("outbox deduplicates operation ids and failed sends are retryable", () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "sync-token", cursor: 0 }, storage);
  updateSyncState(
    (state) => ({
      ...state,
      snapshot_complete: true,
      history_manifest: zeroHistoryManifest(),
      status: "synced",
    }),
    storage,
  );
  const deviceId = ensureSyncDeviceId(storage);
  const item = transcriptItem(deviceId, "op-duplicate");
  enqueueSyncOperation(item, storage);
  enqueueSyncOperation({ ...item, payload: { ...item.payload, text: "changed" } }, storage);
  assert.equal(loadSyncState(storage).outbox.length, 1);
  assert.equal(pendingSyncOperations(20, storage)[0]?.payload.text, "hello");

  markSyncOperationSending(item.operation_id, storage);
  assert.equal(pendingSyncOperations(20, storage).length, 1);
  const reloaded = loadSyncState(storage);
  assert.equal(reloaded.outbox[0]?.status, "pending");

  failSyncOperation(item.operation_id, "network", storage);
  const failed = loadSyncState(storage);
  assert.equal(failed.status, "failed");
  assert.equal(failed.outbox[0]?.retry_count, 1);
  assert.equal(failed.outbox[0]?.retryable, true);
  assert.equal(pendingSyncOperations(20, storage).length, 1);

  completeSyncOperation(item.operation_id, storage);
  assert.equal(loadSyncState(storage).outbox.length, 0);
  assert.equal(loadSyncState(storage).status, "synced");
});

test("completing one operation preserves another failed outbox error", () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "sync-token", cursor: 0 }, storage);
  const deviceId = ensureSyncDeviceId(storage);
  const failedItem = transcriptItem(deviceId, "op-failed");
  const completedItem = transcriptItem(deviceId, "op-completed");
  enqueueSyncOperation(failedItem, storage);
  enqueueSyncOperation(completedItem, storage);
  failSyncOperation(failedItem.operation_id, "network", storage, { retryable: false });

  completeSyncOperation(completedItem.operation_id, storage);
  const state = loadSyncState(storage);
  assert.equal(state.status, "failed");
  assert.equal(state.last_error, "network");
  assert.equal(state.outbox.length, 1);
});

test("corrupt sync state is replaced without touching caller storage keys", () => {
  const storage = new MemoryStorage();
  storage.setItem("other-key", "keep");
  storage.setItem("echodesk.syncState.v1", "not-json");

  const state = loadSyncState(storage);
  assert.match(state.device_id, /^device-/);
  assert.equal(storage.getItem("other-key"), "keep");
  resetSyncStateForTest(storage);
  assert.equal(storage.getItem("echodesk.syncState.v1"), null);
});
