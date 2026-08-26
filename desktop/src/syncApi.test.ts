import assert from "node:assert/strict";
import test from "node:test";
import {
  normalizePairingCreateResponse,
  normalizeClaimResponse,
  SyncHubClient,
  type SyncTransport,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncProtocol.ts";
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

test("fake Hub adapter covers claim persistence and duplicate push", async () => {
  const storage = new MemoryStorage();
  const deviceId = ensureSyncDeviceId(storage);
  const calls: Array<{ path: string; auth: string; body: string }> = [];
  const transport: SyncTransport = {
    request: async (path, init, auth) => {
      calls.push({ path, auth, body: String(init.body ?? "") });
      if (path === "/hub/v1/pairings/claim") {
        return new Response(
          JSON.stringify({ device_id: "hub-device", sync_token: "token-1", cursor: 0 }),
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
  assert.equal(paired.cursor, "0");
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

test("claim cursor accepts canonical numbers and numeric strings", () => {
  assert.equal(
    normalizeClaimResponse({ device_id: "device", sync_token: "token", cursor: 0 }).cursor,
    "0",
  );
  assert.equal(
    normalizeClaimResponse({ device_id: "device", sync_token: "token", cursor: "7" }).cursor,
    "7",
  );
});

test("current device initialization creates then claims a short-lived pairing", async () => {
  const storage = new MemoryStorage();
  const paths: Array<{ path: string; auth: string }> = [];
  const client = new SyncHubClient(
    {
      request: async (path, _init, auth) => {
        paths.push({ path, auth });
        if (path === "/hub/v1/pairings") {
          return new Response(
            JSON.stringify({ pairing_code: "pair-123", expires_at: "2026-07-26T02:00:00Z" }),
            { status: 201, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({ device_id: "hub-device", sync_token: "token-1", cursor: 0 }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    },
    storage,
  );

  await client.initializeCurrentDevice();
  assert.deepEqual(paths, [
    { path: "/hub/v1/pairings", auth: "session" },
    { path: "/hub/v1/pairings/claim", auth: "session" },
  ]);
  assert.equal(loadSyncState(storage).sync_token, "token-1");
});

test("pairing creation requires a non-empty code", () => {
  assert.throws(
    () => normalizePairingCreateResponse({ pairing_code: "" }),
    /配对码/,
  );
});

test("claim cursor rejects negative, fractional, non-finite, and non-numeric values", () => {
  for (const cursor of [-1, 1.5, Number.NaN, "-1", "1.5", "NaN"]) {
    assert.throws(
      () => normalizeClaimResponse({ device_id: "device", sync_token: "token", cursor }),
      /同步游标/,
    );
  }
});

test("changes keep numeric zero cursor and readable structured errors", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token-1", cursor: "0" }, storage);
  const paths: string[] = [];
  const transport: SyncTransport = {
    request: async (path) => {
      paths.push(path);
      return new Response(JSON.stringify({
        changes: [],
        cursor: 0,
        manifest: {
          cursor: 0,
          entity_counts: {
            meeting: 0,
            transcript_segment: 0,
            meeting_summary: 0,
            artifact: 0,
          },
        },
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  };
  const client = new SyncHubClient(transport, storage);

  const first = await client.changes(loadSyncState(storage).cursor);
  const second = await client.changes(first.cursor);
  assert.equal(first.cursor, "0");
  assert.equal(second.cursor, "0");
  assert.deepEqual(first.manifest?.entity_counts, {
    meeting: 0,
    transcript_segment: 0,
    meeting_summary: 0,
    artifact: 0,
  });
  assert.equal(new URL(`http://hub.test${paths[0]}`).searchParams.get("cursor"), "0");
  assert.equal(new URL(`http://hub.test${paths[1]}`).searchParams.get("cursor"), "0");
});

test("structured sync errors do not render as object strings", async () => {
  const storage = new MemoryStorage();
  setPairingState({ sync_token: "token-1", cursor: "0" }, storage);
  const client = new SyncHubClient(
    {
      request: async () =>
        new Response(JSON.stringify({ detail: [{ msg: "cursor must be an integer" }] }), {
          status: 422,
          headers: { "Content-Type": "application/json" },
        }),
    },
    storage,
  );

  await assert.rejects(
    () => client.changes("0"),
    (error: unknown) => {
      assert(error instanceof Error);
      assert.match(error.message, /cursor must be an integer/);
      assert.doesNotMatch(error.message, /\[object Object\]/);
      return true;
    },
  );
});
