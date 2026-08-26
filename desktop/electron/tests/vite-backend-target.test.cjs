const assert = require("node:assert/strict");
const test = require("node:test");

const { resolveViteBackendTarget, websocketTarget } = require("../../vite-backend-target.cjs");

test("Vite proxy follows the injected runtime endpoint", () => {
  const target = resolveViteBackendTarget({ ECHODESK_BASE_URL: "http://127.0.0.1:19345" });
  assert.equal(target, "http://127.0.0.1:19345");
  assert.equal(websocketTarget(target), "ws://127.0.0.1:19345");
});

test("missing or invalid injected endpoint fails closed", () => {
  assert.throws(
    () => resolveViteBackendTarget({}),
    /ECHODESK_BASE_URL must be injected/,
  );
  assert.throws(
    () => resolveViteBackendTarget({ ECHODESK_BASE_URL: "" }),
    /ECHODESK_BASE_URL must not be empty/,
  );
});
