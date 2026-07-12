"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  verifyWorkspaceRootIdentity,
} = require("../workspace-root-identity.cjs");

function fixture(t) {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-root-identity-"));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const root = path.join(temp, "root");
  const outside = path.join(temp, "outside");
  fs.mkdirSync(root);
  fs.mkdirSync(outside);
  return { root: fs.realpathSync(root), outside: fs.realpathSync(outside) };
}

test("workspace root identity persists dev/ino and rejects replacement", async (t) => {
  const { root } = fixture(t);
  const captured = await verifyWorkspaceRootIdentity({ root });
  fs.renameSync(root, `${root}.old`);
  fs.mkdirSync(root);

  await assert.rejects(
    verifyWorkspaceRootIdentity({ root, expectedIdentity: captured.identity }),
    (error) => error.code === "WORKSPACE_ROOT_IDENTITY_CHANGED",
  );
});

test("configured root swapped to an outside symlink never redefines authorization", async (t) => {
  const { root, outside } = fixture(t);
  const captured = await verifyWorkspaceRootIdentity({ root });

  await assert.rejects(
    verifyWorkspaceRootIdentity({
      root,
      expectedIdentity: captured.identity,
      afterInitialLstat: async () => {
        fs.renameSync(root, `${root}.old`);
        fs.symlinkSync(outside, root);
      },
    }),
    (error) =>
      error.code === "WORKSPACE_ROOT_IDENTITY_CHANGED" ||
      error.code === "WORKSPACE_ROOT_INVALID",
  );
});

test("a configured symlink root is rejected even before identity capture", async (t) => {
  const { root, outside } = fixture(t);
  fs.rmdirSync(root);
  fs.symlinkSync(outside, root);
  await assert.rejects(
    verifyWorkspaceRootIdentity({ root }),
    (error) => error.code === "WORKSPACE_ROOT_INVALID",
  );
});
