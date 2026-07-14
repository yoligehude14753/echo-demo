import assert from "node:assert/strict";
import test from "node:test";
import {
  SyncWorkerCore,
  type SyncClientLike,
  type SyncChangeResult,
  type SyncPushResult,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncWorkerCore.ts";
import {
  enqueueSyncOperation,
  ensureSyncDeviceId,
  loadSyncState,
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
}

function change(id: string, cursor: string): SyncChangeResult["changes"][number] {
  return {
    operation_id: `remote-${id}`,
    device_id: "remote-device",
    entity_type: "transcript_segment",
    entity_id: `meeting-1:${id}`,
    revision: 4,
    updated_at: "2026-07-14T12:01:00.000Z",
    cursor,
    payload: {
      meeting_id: "meeting-1",
      text: id,
      start_ms: 0,
      end_ms: 1000,
    },
  };
}

function queueItem(deviceId: string, operationId: string, storage: SyncStorage) {
  return enqueueSyncOperation(
    {
      operation_id: operationId,
      device_id: deviceId,
      entity_type: "transcript_segment",
      entity_id: `meeting-1:${operationId}`,
      base_revision: 2,
      updated_at: "2026-07-14T12:00:00.000Z",
      payload: { meeting_id: "meeting-1", text: operationId },
    },
    storage,
  );
}

test("bounded push treats duplicate as success and applies conflict current once", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token", cursor: "c1" }, storage);
  const deviceId = ensureSyncDeviceId(storage);
  const first = queueItem(deviceId, "op-duplicate", storage);
  queueItem(deviceId, "op-conflict", storage);
  const applied: string[] = [];
  const client: SyncClientLike = {
    push: async (item): Promise<SyncPushResult> =>
      item.operation_id === first.operation_id
        ? { result: "duplicate" }
        : { result: "conflict", current: change("server-current", "c2") },
    changes: async () => ({ changes: [], cursor: "c2" }),
    snapshot: async () => ({ changes: [], cursor: "snapshot" }),
  };
  const worker = new SyncWorkerCore(client, (remote) => applied.push(remote.entity_id), storage);

  const result = await worker.pushBatch(1);
  assert.equal(result.attempted, 1);
  assert.equal(result.completed, 1);
  assert.equal(result.duplicates, 1);
  assert.equal(loadSyncState(storage).outbox.length, 1);

  const rest = await worker.pushBatch(20);
  assert.equal(rest.conflicts, 1);
  assert.equal(rest.completed, 1);
  assert.deepEqual(applied, ["meeting-1:server-current"]);
  assert.equal(loadSyncState(storage).outbox.length, 0);
});

test("changes apply through the injected repository and persist cursor without creating outbox", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token", cursor: "c1" }, storage);
  const applied: string[] = [];
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async (cursor) => ({
      changes: [change(cursor === "c1" ? "remote-1" : "unexpected", "c2")],
      cursor: "c2",
    }),
    snapshot: async () => ({ changes: [], cursor: "snapshot" }),
  };
  const worker = new SyncWorkerCore(client, (remote) => applied.push(remote.entity_id), storage);
  const result = await worker.receiveChanges();

  assert.equal(result.received, 1);
  assert.equal(result.used_snapshot, false);
  assert.equal(loadSyncState(storage).cursor, "c2");
  assert.deepEqual(applied, ["meeting-1:remote-1"]);
  assert.equal(loadSyncState(storage).outbox.length, 0);
});

test("invalid cursor or server snapshot_required falls back to full snapshot", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token", cursor: "stale" }, storage);
  let snapshotCalls = 0;
  const applied: string[] = [];
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async () => {
      throw Object.assign(new Error("cursor invalid"), { status: 409, code: "cursor_invalid" });
    },
    snapshot: async () => {
      snapshotCalls += 1;
      return { changes: [change("from-snapshot", "snap-1")], cursor: "snap-1" };
    },
  };
  const worker = new SyncWorkerCore(client, (remote) => applied.push(remote.entity_id), storage);
  const result = await worker.receiveChanges();

  assert.equal(result.used_snapshot, true);
  assert.equal(snapshotCalls, 1);
  assert.equal(loadSyncState(storage).cursor, "snap-1");
  assert.deepEqual(applied, ["meeting-1:from-snapshot"]);
});

test("conflict without current keeps the outbox item and failed status survives receive", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token", cursor: "c1" }, storage);
  const deviceId = ensureSyncDeviceId(storage);
  const item = queueItem(deviceId, "op-conflict-without-current", storage);
  const client: SyncClientLike = {
    push: async () => ({ result: "conflict" }),
    changes: async () => ({ changes: [], cursor: "c2" }),
    snapshot: async () => ({ changes: [], cursor: "snapshot" }),
  };
  const worker = new SyncWorkerCore(client, () => undefined, storage);

  const result = await worker.reconcile();
  const state = loadSyncState(storage);

  assert.equal(result.conflicts, 1);
  assert.equal(state.status, "failed");
  assert.equal(state.outbox.length, 1);
  assert.equal(state.outbox[0]?.operation_id, item.operation_id);
  assert.equal(state.outbox[0]?.status, "failed");
  assert.equal(state.outbox[0]?.retry_count, 1);
});
