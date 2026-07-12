"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  atomicWritePrivateJsonFile,
  readPrivateJsonFile,
} = require("../private-json-store.cjs");

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-private-json-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return { root, target: path.join(root, "workspaces.json") };
}

test("private JSON read refuses symlinks without touching their target", (t) => {
  const { root, target } = fixture(t);
  const outside = path.join(root, "outside.json");
  fs.writeFileSync(outside, '{"secret":"unchanged"}', { mode: 0o644 });
  fs.symlinkSync(outside, target);

  assert.throws(
    () => readPrivateJsonFile(target),
    (error) => error.code === "PRIVATE_STORE_INVALID",
  );
  assert.equal(fs.readFileSync(outside, "utf8"), '{"secret":"unchanged"}');
  assert.equal(fs.statSync(outside).mode & 0o777, 0o644);
});

test("private JSON read verifies a regular inode and tightens legacy mode", (t) => {
  const { target } = fixture(t);
  fs.writeFileSync(target, '{"schema":3}', { mode: 0o644 });

  assert.deepEqual(readPrivateJsonFile(target), { schema: 3 });
  assert.equal(fs.statSync(target).mode & 0o777, 0o600);
});

test("atomic private JSON write commits mode 0600 and leaves no temp file", (t) => {
  const { root, target } = fixture(t);
  const payload = { schema: 3, origins: { "https://safe.example": {} } };

  assert.deepEqual(
    atomicWritePrivateJsonFile(target, payload, {
      randomSuffix: () => "deterministic123",
    }),
    payload,
  );
  assert.deepEqual(JSON.parse(fs.readFileSync(target, "utf8")), payload);
  assert.equal(fs.statSync(target).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(root), ["workspaces.json"]);
});
