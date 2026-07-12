"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  electronNodeRuntimeEnvironment,
} = require("../backend-runtime-env.cjs");

test("backend receives the packaged Electron executable as its Node runtime", () => {
  assert.deepEqual(
    electronNodeRuntimeEnvironment("/Applications/EchoDesk.app/EchoDesk"),
    {
      ECHODESK_NODE_RUNTIME: "/Applications/EchoDesk.app/EchoDesk",
      ECHODESK_NODE_RUNTIME_IS_ELECTRON: "1",
    },
  );
});

test("empty Electron executable fails closed", () => {
  assert.throws(
    () => electronNodeRuntimeEnvironment("  "),
    /executable is required/,
  );
});
