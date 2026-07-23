"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  BackendContractError,
  validatePublicBackendContract,
} = require("../backend-contract.cjs");

function publicBootstrap(overrides = {}) {
  return {
    schema_version: 1,
    api_version: "0.3",
    session_required: true,
    ws_path: "/ws/echo",
    session_path: "/session",
    minimum_client_version: "0.3.3-preview.2",
    ...overrides,
  };
}

test("public bootstrap accepts only the session-bound public contract", () => {
  assert.deepEqual(
    validatePublicBackendContract(publicBootstrap()),
    publicBootstrap(),
  );
  assert.throws(
    () => validatePublicBackendContract(publicBootstrap({ session_required: false })),
    (error) => error instanceof BackendContractError && error.code === "public-session-contract-mismatch",
  );
});
