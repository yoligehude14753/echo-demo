import assert from "node:assert/strict";
import test from "node:test";

import { isCaptureSpoolOriginCompatible } from "./captureSpoolOrigin.ts";

test("same remote origin remains compatible", () => {
  assert.equal(
    isCaptureSpoolOriginCompatible(
      "https://gateway.example",
      "https://gateway.example",
    ),
    true,
  );
});

test("local backend port changes are compatible within a generation", () => {
  assert.equal(
    isCaptureSpoolOriginCompatible(
      "http://127.0.0.1:58163",
      "http://127.0.0.1:59421",
    ),
    true,
  );
});

test("remote origin, host, and protocol changes remain fenced", () => {
  assert.equal(
    isCaptureSpoolOriginCompatible(
      "https://gateway.example",
      "https://other.example",
    ),
    false,
  );
  assert.equal(
    isCaptureSpoolOriginCompatible(
      "http://127.0.0.1:58163",
      "http://localhost:59421",
    ),
    false,
  );
  assert.equal(
    isCaptureSpoolOriginCompatible(
      "https://127.0.0.1:58163",
      "https://127.0.0.1:59421",
    ),
    false,
  );
});
