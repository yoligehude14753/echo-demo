"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  projectBackendStatusForRenderer,
} = require("../backend-status-projection.cjs");

test("backend supervisor projection never exposes paths or raw process errors", () => {
  const projected = projectBackendStatusForRenderer({
    state: "restarting",
    attempt: 2,
    backoff_ms: 2_000,
    searched: ["/Users/alice/private/backend/.venv/bin/python"],
    reason: "spawn threw: ENOENT /Users/alice/private/backend/.venv/bin/python",
    last_error: "cwd=/Users/alice/private/backend",
    help_url: "docs/INSTALL.md",
  });

  assert.deepEqual(projected, {
    state: "restarting",
    attempt: 2,
    backoff_ms: 2_000,
    help_url: "docs/INSTALL.md",
    reason_code: "backend-spawn-failed",
    reason: "backend process failed to start",
  });
  assert.doesNotMatch(JSON.stringify(projected), /Users|private|ENOENT|cwd|searched/);
});

test("bundled backend failure is a first-class renderer status", () => {
  assert.deepEqual(
    projectBackendStatusForRenderer({
      state: "bundled-backend-unavailable",
      searched: ["C:\\Users\\alice\\EchoDesk\\echodesk-backend.exe"],
    }),
    { state: "bundled-backend-unavailable" },
  );
});

test("public bootstrap and session failures remain visible without endpoint details", () => {
  assert.deepEqual(
    projectBackendStatusForRenderer({
      state: "degraded",
      mode: "public-service",
      attempt: 2,
      backoff_ms: 2_000,
      reason: "public-bootstrap-unreachable",
      last_error: "ECONNREFUSED https://private.example.test/bootstrap",
    }),
    {
      state: "degraded",
      mode: "public-service",
      attempt: 2,
      backoff_ms: 2_000,
      reason_code: "public-bootstrap-unreachable",
      reason: "public service bootstrap is unreachable; retrying",
    },
  );
});

test("unknown status fields fail closed", () => {
  assert.deepEqual(
    projectBackendStatusForRenderer({
      state: "attacker-controlled",
      port: -1,
      mode: "unsafe",
      help_url: "file:///Users/alice/private",
      reason: "/Users/alice/private",
    }),
    {
      state: "unknown",
      reason_code: "backend-unavailable",
      reason: "backend service is unavailable",
    },
  );
});
