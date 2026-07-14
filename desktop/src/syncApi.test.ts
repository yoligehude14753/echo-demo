import assert from "node:assert/strict";
import test from "node:test";
import {
  SyncHubClient,
  type SyncTransport,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncProtocol.ts";
import {
  enqueueSyncOperation,
  ensureSyncDeviceId,
  loadSyncState,
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

test("fake Hub adapter covers claim persistence and duplicate push", async () => {
  const storage = new MemoryStorage();
  const deviceId = ensureSyncDeviceId(storage);
  const calls: Array<{ path: string; auth: string; body: string }> = [];
  const transport: SyncTransport = {
    request: async (path, init, auth) => {
      calls.push({ path, auth, body: String(init.body ?? "") });
      if (path === "/hub/v1/pairings/claim") {
        return new Response(
          JSON.stringify({ device_id: "hub-device", sync_token: "token-1", cursor: "cursor-1" }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ result: "duplicate" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  };
  const client = new SyncHubClient(transport, storage);

  await client.claimPairing("  pair-123  ");
  const paired = loadSyncState(storage);
  assert.equal(paired.device_id, "hub-device");
  assert.equal(paired.sync_token, "token-1");
  assert.equal(paired.cursor, "cursor-1");
  assert.equal(JSON.parse(calls[0].body).device_id, deviceId);
  assert.equal(calls[0].auth, "session");

  const queued = enqueueSyncOperation(
    {
      operation_id: "op-1",
      device_id: paired.device_id,
      entity_type: "transcript_segment",
      entity_id: "meeting-1:0:1000",
      base_revision: 0,
      updated_at: "2026-07-14T12:00:00.000Z",
      payload: { meeting_id: "meeting-1", text: "hello" },
    },
    storage,
  );
  const result = await client.push(queued);
  assert.equal(result.result, "duplicate");
  assert.equal(calls[1].auth, "sync");
  assert.equal(JSON.parse(calls[1].body).operation_id, "op-1");
});
