"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = readFileSync(
  path.join(__dirname, "..", "..", "src", "syncWorker.ts"),
  "utf8",
);

test("legacy history pages continue promptly without weakening failed-sync backoff", () => {
  assert.match(source, /const LEGACY_HISTORY_CONTINUATION_MS = 1_000/);
  assert.match(source, /state\.status !== "failed" && legacy && legacy\.phase !== "complete"/);
  assert.match(source, /const delay = syncWorkerPollDelay\(state\)/);
  assert.match(source, /this\.schedulePoll\(delay\)/);
  assert.match(source, /private schedulePoll\(delay = SYNC_WORKER_POLL_MS\)/);
});
