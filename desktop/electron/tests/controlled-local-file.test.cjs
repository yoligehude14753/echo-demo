"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  resolveControlledLocalArtifactPath,
} = require("../controlled-local-file.cjs");

test("artifact path policy accepts only regular generated files under a controlled root", (t) => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-artifact-policy-"));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const controlled = path.join(temp, "skill_build");
  const outside = path.join(temp, "outside");
  fs.mkdirSync(controlled);
  fs.mkdirSync(outside);
  const artifact = path.join(controlled, "deck.pptx");
  const secret = path.join(outside, "private.pdf");
  fs.writeFileSync(artifact, "deck");
  fs.writeFileSync(secret, "secret");

  assert.equal(
    resolveControlledLocalArtifactPath(artifact, [controlled]),
    fs.realpathSync.native(artifact),
  );
  assert.throws(
    () => resolveControlledLocalArtifactPath(secret, [controlled]),
    (error) => error.code === "ARTIFACT_PATH_OUTSIDE_CONTROLLED_ROOT",
  );
  assert.throws(
    () => resolveControlledLocalArtifactPath(controlled, [controlled]),
    (error) => error.code === "ARTIFACT_TYPE_FORBIDDEN",
  );
});

test("artifact path policy rejects unsupported types and symlink escapes", (t) => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-artifact-symlink-"));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const controlled = path.join(temp, "storage");
  const outside = path.join(temp, "outside");
  fs.mkdirSync(controlled);
  fs.mkdirSync(outside);
  const sqlite = path.join(controlled, "echo.db");
  const secret = path.join(outside, "private.pdf");
  const symlink = path.join(controlled, "linked.pdf");
  fs.writeFileSync(sqlite, "db");
  fs.writeFileSync(secret, "secret");
  fs.symlinkSync(secret, symlink);

  assert.throws(
    () => resolveControlledLocalArtifactPath(sqlite, [controlled]),
    (error) => error.code === "ARTIFACT_TYPE_FORBIDDEN",
  );
  assert.throws(
    () => resolveControlledLocalArtifactPath(symlink, [controlled]),
    (error) => error.code === "ARTIFACT_PATH_OUTSIDE_CONTROLLED_ROOT",
  );
  assert.throws(
    () => resolveControlledLocalArtifactPath("relative.pdf", [controlled]),
    (error) => error.code === "ARTIFACT_PATH_INVALID",
  );
});
