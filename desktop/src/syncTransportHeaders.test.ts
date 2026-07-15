import assert from "node:assert/strict";
import test from "node:test";
import {
  prepareSyncRequest,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncTransportHeaders.ts";

test("sync REST uses the sync header and never Authorization", () => {
  const request = prepareSyncRequest(
    { method: "POST", headers: { Authorization: "Bearer stale" } },
    "sync",
    "sync-token",
  );
  const headers = new Headers(request.init.headers);

  assert.equal(headers.get("X-Echo-Sync-Token"), "sync-token");
  assert.equal(headers.get("Authorization"), null);
  assert.equal(request.bearerToken, null);
});

test("claim REST uses the server session Bearer token and no sync header", () => {
  const request = prepareSyncRequest(
    { method: "POST", headers: { "X-Echo-Sync-Token": "stale-sync" } },
    "session",
    "session-token",
  );
  const headers = new Headers(request.init.headers);

  assert.equal(headers.get("X-Echo-Sync-Token"), null);
  assert.equal(request.bearerToken, "session-token");
});

test("ordinary business REST keeps the existing session Bearer policy", () => {
  const request = prepareSyncRequest({ method: "GET" }, "session", "business-token");

  assert.equal(new Headers(request.init.headers).get("X-Echo-Sync-Token"), null);
  assert.equal(request.bearerToken, "business-token");
});
