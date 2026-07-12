"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = readFileSync(path.resolve(__dirname, "../../src/ws.ts"), "utf8");

test("pending authenticated connection cannot create a socket after hook cleanup", () => {
  const awaitConnection = source.indexOf(
    "connection = await authenticatedWsConnection()",
  );
  const postAwaitStop = source.indexOf("if (stopRef.current)", awaitConnection);
  const socketCreate = source.indexOf("new WebSocket(url)", awaitConnection);
  assert.ok(awaitConnection >= 0);
  assert.ok(postAwaitStop > awaitConnection && postAwaitStop < socketCreate);
  assert.match(
    source.slice(awaitConnection, socketCreate),
    /stateRef\.current = "closed";[\s\S]+?setConnected\(false\)/,
  );
  assert.match(
    source,
    /catch \(error\) \{[\s\S]+?if \(stopRef\.current\) \{[\s\S]+?stateRef\.current = "closed"/,
  );
});

test("renderer rejects non-string and oversized WS frames before JSON.parse", () => {
  const messageStart = source.indexOf("ws.onmessage = (evt) =>");
  const parse = source.indexOf("JSON.parse(evt.data)", messageStart);
  const typeGuard = source.indexOf("isBoundedWsTextFrame(evt.data)", messageStart);
  assert.match(source, /WS_MAX_INBOUND_BYTES = 1024 \* 1024/);
  assert.ok(messageStart >= 0 && typeGuard > messageStart && typeGuard < parse);
  assert.match(source, /value\.length > WS_MAX_INBOUND_BYTES/);
  assert.match(source, /wsFrameEncoder\.encodeInto\(value, wsFrameScratch\)/);
  assert.doesNotMatch(source, /wsFrameEncoder\.encode\(evt\.data\)/);
  assert.match(source.slice(typeGuard, parse), /ws\.close\(4008,/);
});
