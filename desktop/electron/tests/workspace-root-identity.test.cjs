"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  sameCanonicalWorkspaceRootPath,
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

function createDirectoryLink(target, link) {
  fs.symlinkSync(
    target,
    link,
    process.platform === "win32" ? "junction" : "dir",
  );
}

test("canonical root comparison follows host path case semantics", () => {
  assert.equal(
    sameCanonicalWorkspaceRootPath(
      "C:\\Users\\Alice\\Workspace",
      "c:\\users\\alice\\workspace",
      "win32",
    ),
    true,
  );
  assert.equal(
    sameCanonicalWorkspaceRootPath(
      "\\\\?\\C:\\Users\\Alice\\Workspace",
      "c:\\users\\alice\\workspace",
      "win32",
    ),
    true,
  );
  assert.equal(
    sameCanonicalWorkspaceRootPath(
      "\\\\?\\UNC\\server\\share\\Workspace",
      "\\\\server\\share\\workspace",
      "win32",
    ),
    true,
  );
  assert.equal(
    sameCanonicalWorkspaceRootPath(
      "C:\\Users\\Alice\\Workspace",
      "C:\\Users\\Alice\\Outside",
      "win32",
    ),
    false,
  );
  assert.equal(
    sameCanonicalWorkspaceRootPath(
      "/Users/Alice/Workspace",
      "/users/alice/workspace",
      "linux",
    ),
    false,
  );
});

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

test("64-bit workspace file ids cannot alias through Number rounding", async (t) => {
  const { root } = fixture(t);
  const firstId = 9_007_199_254_740_992n;
  let currentId = firstId;
  t.mock.method(fs.promises, "lstat", async (_target, options = {}) => {
    const id = options.bigint ? currentId : Number(currentId);
    return {
      dev: options.bigint ? 1n : 1,
      ino: id,
      isDirectory: () => true,
      isSymbolicLink: () => false,
    };
  });

  const captured = await verifyWorkspaceRootIdentity({ root });
  assert.deepEqual(captured.identity, {
    dev: "1",
    ino: String(firstId),
  });

  // These adjacent IDs collapse to the same IEEE-754 Number. The verifier
  // must still reject the replacement because every lstat requests BigIntStats.
  currentId = firstId + 1n;
  assert.equal(Number(firstId), Number(currentId));
  await assert.rejects(
    verifyWorkspaceRootIdentity({ root, expectedIdentity: captured.identity }),
    (error) => error.code === "WORKSPACE_ROOT_IDENTITY_CHANGED",
  );
});

test("Windows accepts realpath spelling drift only while the file ID is stable", async (t) => {
  const { root } = fixture(t);
  let currentId = 42n;
  t.mock.method(fs.promises, "lstat", async () => ({
    dev: 1n,
    ino: currentId,
    isDirectory: () => true,
    isSymbolicLink: () => false,
  }));
  t.mock.method(
    fs.promises,
    "realpath",
    async () => "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\root",
  );

  const captured = await verifyWorkspaceRootIdentity({ root, platform: "win32" });
  assert.deepEqual(captured.identity, { dev: "1", ino: "42" });
  currentId = 43n;
  await assert.rejects(
    verifyWorkspaceRootIdentity({
      root,
      platform: "win32",
      expectedIdentity: captured.identity,
    }),
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
        createDirectoryLink(outside, root);
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
  createDirectoryLink(outside, root);
  await assert.rejects(
    verifyWorkspaceRootIdentity({ root }),
    (error) => error.code === "WORKSPACE_ROOT_INVALID",
  );
});
