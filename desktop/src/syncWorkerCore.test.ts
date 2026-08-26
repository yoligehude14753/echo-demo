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
  updateSyncState,
  type SyncStorage,
  type SyncHistoryManifest,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncState.ts";

function historyManifest(
  cursor: string,
  counts: Partial<SyncHistoryManifest["entity_counts"]> = {},
): SyncHistoryManifest {
  return {
    cursor,
    entity_counts: {
      meeting: counts.meeting ?? 0,
      transcript_segment: counts.transcript_segment ?? 0,
      meeting_summary: counts.meeting_summary ?? 0,
      artifact: counts.artifact ?? 0,
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

function historyChange(
  entityType: SyncChangeResult["changes"][number]["entity_type"],
  entityId: string,
  cursor: string,
): SyncChangeResult["changes"][number] {
  return {
    ...change(`${entityType}-${entityId}`, cursor),
    entity_type: entityType,
    entity_id: entityId,
    payload: { entity_id: entityId },
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

function pairWithSnapshot(storage: SyncStorage, cursor = "c1") {
  setPairingState({ sync_token: "token", cursor }, storage);
  updateSyncState(
    (state) => ({
      ...state,
      cursor,
      snapshot_complete: true,
      history_manifest: historyManifest(cursor),
      status: "synced",
    }),
    storage,
  );
}

test("bounded push treats duplicate as success and applies conflict current once", async () => {
  const storage = new MemoryStorage();
  pairWithSnapshot(storage);
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

test("first receive after pairing drains bounded change pages from zero", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token", cursor: "pairing-latest" }, storage);
  let snapshotCalls = 0;
  const changeCursors: Array<string | null> = [];
  const applied: string[] = [];
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async (cursor) => {
      changeCursors.push(cursor);
      if (cursor === "0") {
        return {
          changes: [change("history-1", "1"), change("history-2", "2")],
          cursor: "2",
          manifest: historyManifest("3", { transcript_segment: 3 }),
        };
      }
      if (cursor === "2") {
        return {
          changes: [change("history-3", "3")],
          cursor: "3",
          manifest: historyManifest("3", { transcript_segment: 3 }),
        };
      }
      return {
        changes: [],
        cursor: cursor ?? "3",
        manifest: historyManifest("3", { transcript_segment: 3 }),
      };
    },
    snapshot: async () => {
      snapshotCalls += 1;
      return { changes: [change("unexpected-snapshot", "snapshot-cursor")], cursor: "snapshot-cursor" };
    },
  };
  const worker = new SyncWorkerCore(client, (remote) => applied.push(remote.entity_id), storage);

  const first = await worker.receiveChanges(false, 2);
  assert.equal(first.received, 3);
  assert.equal(first.replayed_from_zero, false);
  assert.equal(snapshotCalls, 0);
  assert.deepEqual(changeCursors, ["0", "2"]);
  assert.equal(loadSyncState(storage).snapshot_complete, true);
  assert.deepEqual(applied, ["meeting-1:history-1", "meeting-1:history-2", "meeting-1:history-3"]);
  assert.equal(Object.keys(loadSyncState(storage).canonical_revisions).length, 3);

  const second = await worker.receiveChanges();
  assert.equal(second.replayed_from_zero, false);
  assert.equal(snapshotCalls, 0);
  assert.deepEqual(changeCursors, ["0", "2", "3"]);
});

test("an empty four-class manifest can complete without a snapshot request", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token", cursor: "0" }, storage);
  let snapshotCalls = 0;
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async () => ({ changes: [], cursor: "0", manifest: historyManifest("0") }),
    snapshot: async () => {
      snapshotCalls += 1;
      return { changes: [], cursor: "0" };
    },
  };
  const worker = new SyncWorkerCore(client, () => undefined, storage);

  await worker.receiveChanges();
  const state = loadSyncState(storage);
  assert.equal(snapshotCalls, 0);
  assert.equal(state.snapshot_complete, true);
  assert.equal(state.status, "synced");
});

test("all four non-empty history classes must be applied before sync is ready", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token", cursor: "0" }, storage);
  const changes = [
    historyChange("meeting", "m1", "1"),
    historyChange("transcript_segment", "s1", "2"),
    historyChange("meeting_summary", "m1", "3"),
    historyChange("artifact", "a1", "4"),
  ];
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async () => ({
      changes,
      cursor: "4",
      manifest: historyManifest("4", {
        meeting: 1,
        transcript_segment: 1,
        meeting_summary: 1,
        artifact: 1,
      }),
    }),
    snapshot: async () => ({ changes: [], cursor: "0" }),
  };
  const worker = new SyncWorkerCore(client, () => undefined, storage);

  await worker.receiveChanges();
  const state = loadSyncState(storage);
  assert.equal(Object.keys(state.canonical_revisions).length, 4);
  assert.equal(state.snapshot_complete, true);
  assert.equal(state.status, "synced");
});

test("a disconnected history pull resumes from its durable cursor and completes", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token", cursor: "0" }, storage);
  const firstCursors: Array<string | null> = [];
  const firstClient: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async (cursor) => {
      firstCursors.push(cursor);
      if (cursor === "0") {
        return {
          changes: [change("history-1", "1")],
          cursor: "1",
          manifest: historyManifest("2", { transcript_segment: 2 }),
        };
      }
      throw new Error("network unavailable");
    },
    snapshot: async () => ({ changes: [], cursor: "0" }),
  };
  const firstWorker = new SyncWorkerCore(firstClient, () => undefined, storage);

  await assert.rejects(() => firstWorker.receiveChanges(false, 1), /network unavailable/);
  const interrupted = loadSyncState(storage);
  assert.deepEqual(firstCursors, ["0", "1"]);
  assert.equal(interrupted.cursor, "1");
  assert.equal(interrupted.snapshot_complete, false);
  assert.equal(interrupted.status, "failed");

  const resumedCursors: Array<string | null> = [];
  const resumedClient: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async (cursor) => {
      resumedCursors.push(cursor);
      return {
        changes: [change("history-2", "2")],
        cursor: "2",
        manifest: historyManifest("2", { transcript_segment: 2 }),
      };
    },
    snapshot: async () => ({ changes: [], cursor: "0" }),
  };
  const resumedWorker = new SyncWorkerCore(resumedClient, () => undefined, storage);

  await resumedWorker.receiveChanges(false, 1);
  const complete = loadSyncState(storage);
  assert.deepEqual(resumedCursors, ["1"]);
  assert.equal(complete.cursor, "2");
  assert.equal(complete.snapshot_complete, true);
  assert.equal(complete.status, "synced");
});

test("tail page without a four-class manifest remains syncing", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token", cursor: 0 }, storage);
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async () => ({ changes: [], cursor: "0" }),
    snapshot: async () => ({ changes: [], cursor: "0" }),
  };
  const worker = new SyncWorkerCore(client, () => undefined, storage);

  await worker.receiveChanges();
  const state = loadSyncState(storage);
  assert.equal(state.snapshot_complete, false);
  assert.equal(state.status, "syncing");
});

test("manifest count mismatch fails closed at the target cursor", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token", cursor: 0 }, storage);
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async () => ({
      changes: [change("history-1", "1")],
      cursor: "1",
      manifest: historyManifest("1", { meeting: 1, transcript_segment: 1 }),
    }),
    snapshot: async () => ({ changes: [], cursor: "0" }),
  };
  const worker = new SyncWorkerCore(client, () => undefined, storage);

  await worker.receiveChanges();
  const state = loadSyncState(storage);
  assert.equal(state.cursor, "1");
  assert.equal(state.snapshot_complete, false);
  assert.equal(state.status, "syncing");
});

test("a previously complete client becomes syncing when a newer manifest is only partially covered", async () => {
  const storage = new MemoryStorage();
  pairWithSnapshot(storage, "1");
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async () => ({
      changes: [change("history-2", "2")],
      cursor: "2",
      manifest: historyManifest("3", { transcript_segment: 2 }),
    }),
    snapshot: async () => ({ changes: [], cursor: "0" }),
  };
  const worker = new SyncWorkerCore(client, () => undefined, storage);

  await worker.receiveChanges();
  const state = loadSyncState(storage);
  assert.equal(state.cursor, "2");
  assert.equal(state.snapshot_complete, false);
  assert.equal(state.status, "syncing");
});

test("changes apply through the injected repository and persist cursor without creating outbox", async () => {
  const storage = new MemoryStorage();
  pairWithSnapshot(storage);
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
  assert.equal(result.replayed_from_zero, false);
  assert.equal(loadSyncState(storage).cursor, "c2");
  assert.deepEqual(applied, ["meeting-1:remote-1"]);
  assert.equal(loadSyncState(storage).outbox.length, 0);
});

test("numeric zero changes cursor persists as string and is reused on the next poll", async () => {
  const storage = new MemoryStorage();
  pairWithSnapshot(storage, "0");
  const calls: Array<string | null> = [];
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async (cursor) => {
      calls.push(cursor);
      return { changes: [], cursor: 0 as unknown as string };
    },
    snapshot: async () => ({ changes: [], cursor: "0" }),
  };
  const worker = new SyncWorkerCore(client, () => undefined, storage);

  await worker.receiveChanges();
  assert.equal(loadSyncState(storage).cursor, "0");
  await worker.receiveChanges();
  assert.deepEqual(calls, ["0", "0"]);
});

test("invalid cursor replays bounded change pages from zero without a full snapshot", async () => {
  const storage = new MemoryStorage();
  pairWithSnapshot(storage, "stale");
  let snapshotCalls = 0;
  const changeCursors: Array<string | null> = [];
  const applied: string[] = [];
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async (cursor) => {
      changeCursors.push(cursor);
      if (cursor !== "0") {
        throw Object.assign(new Error("cursor invalid"), { status: 409, code: "cursor_invalid" });
      }
      return {
        changes: [change("from-zero", "1")],
        cursor: "1",
        manifest: historyManifest("1", { transcript_segment: 1 }),
      };
    },
    snapshot: async () => {
      snapshotCalls += 1;
      return { changes: [change("from-snapshot", "snap-1")], cursor: "snap-1" };
    },
  };
  const worker = new SyncWorkerCore(client, (remote) => applied.push(remote.entity_id), storage);
  const result = await worker.receiveChanges();

  assert.equal(result.replayed_from_zero, true);
  assert.equal(snapshotCalls, 0);
  assert.deepEqual(changeCursors, ["stale", "0"]);
  assert.equal(loadSyncState(storage).cursor, "1");
  assert.deepEqual(applied, ["meeting-1:from-zero"]);
});

test("snapshot-required control restarts bounded changes at zero without calling snapshot", async () => {
  const storage = new MemoryStorage();
  pairWithSnapshot(storage, "7");
  let snapshotCalls = 0;
  const changeCursors: Array<string | null> = [];
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async (cursor) => {
      changeCursors.push(cursor);
      if (cursor !== "0") return { changes: [], cursor, snapshot_required: true };
      return { changes: [], cursor: "0", manifest: historyManifest("0") };
    },
    snapshot: async () => {
      snapshotCalls += 1;
      return { changes: [], cursor: "0" };
    },
  };
  const worker = new SyncWorkerCore(client, () => undefined, storage);

  const result = await worker.receiveChanges();
  assert.equal(result.replayed_from_zero, true);
  assert.equal(snapshotCalls, 0);
  assert.deepEqual(changeCursors, ["7", "0"]);
  assert.equal(loadSyncState(storage).snapshot_complete, true);
  assert.equal(loadSyncState(storage).status, "synced");
});

test("a regressed server cursor also replays from zero for older Hub compatibility", async () => {
  const storage = new MemoryStorage();
  pairWithSnapshot(storage, "7");
  const changeCursors: Array<string | null> = [];
  const client: SyncClientLike = {
    push: async () => ({ result: "applied" }),
    changes: async (cursor) => {
      changeCursors.push(cursor);
      if (cursor === "7") return { changes: [], cursor: "3", manifest: historyManifest("3") };
      return { changes: [], cursor: "0", manifest: historyManifest("0") };
    },
    snapshot: async () => ({ changes: [], cursor: "0" }),
  };
  const worker = new SyncWorkerCore(client, () => undefined, storage);

  await worker.receiveChanges();
  assert.deepEqual(changeCursors, ["7", "0"]);
  assert.equal(loadSyncState(storage).cursor, "0");
  assert.equal(loadSyncState(storage).status, "synced");
});

test("conflict without current is terminal failed and is not retried automatically", async () => {
  const storage = new MemoryStorage();
  pairWithSnapshot(storage);
  const deviceId = ensureSyncDeviceId(storage);
  const item = queueItem(deviceId, "op-conflict-without-current", storage);
  let pushCalls = 0;
  const client: SyncClientLike = {
    push: async () => {
      pushCalls += 1;
      return { result: "conflict" };
    },
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
  assert.equal(state.outbox[0]?.retryable, false);

  const retry = await worker.pushBatch();
  assert.equal(retry.attempted, 0);
  assert.equal(pushCalls, 1);
});
