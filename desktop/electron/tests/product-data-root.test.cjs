"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  LEGACY_CANDIDATE_PRODUCT_NAME,
  resolveProductDataRoot,
} = require("../product-data-root.cjs");

test("canonical product keeps the canonical data root", () => {
  assert.equal(
    resolveProductDataRoot({ env: {}, homeDir: "/Users/example", productName: "EchoDesk" }),
    "/Users/example/.echodesk",
  );
});

test("legacy candidate receives its own persistent data root", () => {
  assert.equal(
    resolveProductDataRoot({
      env: {},
      homeDir: "/Users/example",
      productName: LEGACY_CANDIDATE_PRODUCT_NAME,
    }),
    "/Users/example/.echodesk-legacy-candidate",
  );
});

test("an explicit absolute data root remains injectable", () => {
  assert.equal(
    resolveProductDataRoot({
      env: { ECHO_USER_DIR: "/tmp/echodesk-acceptance" },
      homeDir: "/Users/example",
      productName: LEGACY_CANDIDATE_PRODUCT_NAME,
    }),
    "/tmp/echodesk-acceptance",
  );
});

test("relative data roots fail closed", () => {
  assert.throws(
    () => resolveProductDataRoot({
      env: { ECHO_USER_DIR: "./runtime-user" },
      homeDir: "/Users/example",
      productName: LEGACY_CANDIDATE_PRODUCT_NAME,
    }),
    /ECHO_USER_DIR must be absolute/,
  );
});
