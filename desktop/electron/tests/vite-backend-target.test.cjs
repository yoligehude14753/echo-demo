const assert = require("node:assert/strict");
const test = require("node:test");

const { resolveViteBackendTarget, websocketTarget } = require("../../vite-backend-target.cjs");

test("Vite proxy follows an explicit isolated backend port", () => {
  const target = resolveViteBackendTarget(
    { ECHO_BACKEND_PORT: "18769" },
    "http://127.0.0.1:8769",
  );
  assert.equal(target, "http://127.0.0.1:18769");
  assert.equal(websocketTarget(target), "ws://127.0.0.1:18769");
});

test("VITE_API_TARGET is authoritative and invalid explicit values fail closed", () => {
  assert.equal(
    resolveViteBackendTarget(
      { VITE_API_TARGET: "http://127.0.0.1:19001", ECHO_BACKEND_PORT: "8769" },
      "http://127.0.0.1:8769",
    ),
    "http://127.0.0.1:19001",
  );
  assert.throws(
    () => resolveViteBackendTarget({ VITE_API_TARGET: "" }),
    /VITE_API_TARGET must not be empty/,
  );
  assert.throws(
    () => resolveViteBackendTarget({ ECHO_BACKEND_PORT: "8769junk" }),
    /ECHO_BACKEND_PORT must be an integer/,
  );
  assert.throws(
    () => resolveViteBackendTarget({ ECHO_RUNTIME_MODE: "diagnostic", ECHO_BACKEND_CWD: "/tmp/backend" }),
    /isolated backend target is required/,
  );
});
