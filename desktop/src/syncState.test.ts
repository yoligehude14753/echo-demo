import assert from "node:assert/strict";
import test from "node:test";
import {
  clearPairing,
  completeSyncOperation,
  enqueueSyncOperation,
  ensureSyncDeviceId,
  failSyncOperation,
  knownSyncEntityRevision,
  loadSyncState,
  makeOperationId,
  markSyncOperationSending,
  pendingSyncOperations,
  rememberSyncEntityRevision,
  resetSyncStateForTest,
  setPairingState,
  type SyncStorage,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncState.ts";

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

test("pairing token and cursor survive restart and unpair keeps outbox", () => {
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
  assert.equal(paired.cursor, "cursor-7");
  assert.equal(paired.outbox[0]?.device_id, "hub-device-1");

  const restarted = loadSyncState(storage);
  assert.equal(restarted.sync_token, "sync-token");
  assert.equal(restarted.cursor, "cursor-7");
  clearPairing(storage);
  const unpaired = loadSyncState(storage);
  assert.equal(unpaired.status, "unpaired");
  assert.equal(unpaired.sync_token, null);
  assert.equal(unpaired.outbox.length, 1);
});

test("canonical numeric zero cursor stays in the string state representation", () => {
  const storage = new MemoryStorage();
  const state = setPairingState({ sync_token: "sync-token", cursor: 0 }, storage);
  assert.equal(state.cursor, "0");
  assert.equal(typeof state.cursor, "string");
  assert.equal(loadSyncState(storage).cursor, "0");
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
