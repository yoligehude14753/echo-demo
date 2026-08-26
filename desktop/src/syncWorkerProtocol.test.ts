import assert from "node:assert/strict";
import test from "node:test";
import {
  parseSyncFrame,
  syncHubWebSocketUrl,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncWorkerProtocol.ts";

test("builds the Hub WebSocket endpoint with the current cursor", () => {
  const url = new URL(syncHubWebSocketUrl("https://sync.example.com/base", "cursor 2"));

  assert.equal(url.protocol, "wss:");
  assert.equal(url.host, "sync.example.com");
  assert.equal(url.pathname, "/hub/v1/sync/events");
  assert.equal(url.searchParams.get("cursor"), "cursor 2");
});

test("parses direct and wrapped change frames without changing the payload", () => {
  const change = {
    operation_id: "remote-op-1",
    device_id: "remote-device",
    entity_type: "transcript_segment",
    entity_id: "meeting-1:segment-1",
    revision: 3,
    updated_at: "2026-07-14T12:00:00.000Z",
    payload: { meeting_id: "meeting-1", text: "hello" },
  };

  const direct = parseSyncFrame(JSON.stringify({ ...change, cursor: "c3" }));
  assert.deepEqual(direct?.change, { ...change, cursor: "c3" });
  assert.equal(direct?.cursor, "c3");

  const wrapped = parseSyncFrame(JSON.stringify({ type: "change", cursor: "c4", change }));
  assert.deepEqual(wrapped?.change, { ...change, cursor: "c4" });
  assert.equal(wrapped?.cursor, "c4");
});

test("recognizes ping, hello and snapshot-required control frames", () => {
  assert.equal(parseSyncFrame('{"type":"server_ping"}')?.ping, true);
  assert.equal(parseSyncFrame('{"type":"server_hello","cursor":"c5"}')?.cursor, "c5");
  assert.equal(
    parseSyncFrame('{"type":"cursor_invalid","cursor":"stale"}')?.snapshotRequired,
    true,
  );
});

test("normalizes numeric zero WS cursors and rejects invalid cursors", () => {
  assert.equal(parseSyncFrame('{"type":"server_hello","cursor":0}')?.cursor, "0");
  const change = parseSyncFrame(
    JSON.stringify({
      entity_type: "transcript_segment",
      entity_id: "meeting-1:segment-1",
      payload: { text: "hello" },
      cursor: 0,
    }),
  );
  assert.equal(change?.cursor, "0");
  assert.equal(change?.change?.cursor, "0");

  for (const cursor of ["", -1, 1.5]) {
    assert.equal(parseSyncFrame(JSON.stringify({ type: "server_hello", cursor })), null);
  }
});

test("rejects malformed and oversized frames", () => {
  assert.equal(parseSyncFrame("not json"), null);
  assert.equal(parseSyncFrame(JSON.stringify({ type: "unknown" })), null);
  assert.equal(
    parseSyncFrame(
      JSON.stringify({
        entity_type: "future_entity",
        entity_id: "future-1",
        payload: {},
      }),
    ),
    null,
  );
  assert.equal(parseSyncFrame("x".repeat(1024 * 1024 + 1)), null);
});
