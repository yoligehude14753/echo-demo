"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  WORKSPACE_SNAPSHOT_PREFIX,
  cleanupStaleWorkspaceSnapshotDirs,
  cleanupWorkspaceSnapshotDirectory,
  copyRetainedWorkspaceSnapshot,
  createWorkspaceFileSnapshot,
  ensurePrivateWorkspaceSnapshotRoot,
  removeRetainedWorkspaceSnapshotFile,
  validateRetainedWorkspaceSnapshot,
} = require("../workspace-file-snapshot.cjs");

function fixture(t) {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-workspace-snapshot-"));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const root = path.join(temp, "workspace");
  const snapshots = path.join(temp, "snapshots");
  fs.mkdirSync(root);
  fs.mkdirSync(snapshots, { mode: 0o700 });
  return {
    temp,
    root: fs.realpathSync.native(root),
    snapshots: fs.realpathSync.native(snapshots),
  };
}

test("workspace snapshot is bounded, hashed, private and detached from later source changes", async (t) => {
  const { root, snapshots } = fixture(t);
  const source = path.join(root, "brief.md");
  fs.writeFileSync(source, "trusted content");
  const snapshot = await createWorkspaceFileSnapshot({
    sourcePath: source,
    authorizedRoot: root,
    snapshotDirectory: snapshots,
    maxBytes: 1024,
  });
  fs.writeFileSync(source, "changed later");

  assert.equal(fs.readFileSync(snapshot.path, "utf8"), "trusted content");
  assert.equal(snapshot.size, Buffer.byteLength("trusted content"));
  assert.equal(snapshot.sha256.length, 64);
  assert.equal(fs.statSync(snapshot.path).mode & 0o777, 0o600);
});

test("snapshot is acknowledged only after its parent directory fsync", async (t) => {
  const { root, snapshots } = fixture(t);
  const source = path.join(root, "durable.md");
  fs.writeFileSync(source, "durable");
  const synced = [];
  const snapshot = await createWorkspaceFileSnapshot({
    sourcePath: source,
    authorizedRoot: root,
    snapshotDirectory: snapshots,
    maxBytes: 1024,
    syncDirectory: async (directory) => {
      synced.push(directory);
      assert.equal(fs.readdirSync(directory).length, 1);
    },
  });
  assert.deepEqual(synced, [snapshots]);
  assert.equal(fs.existsSync(snapshot.path), true);
});

test("parent directory fsync failure removes the uncommitted snapshot", async (t) => {
  const { root, snapshots } = fixture(t);
  const source = path.join(root, "power-loss.md");
  fs.writeFileSync(source, "not committed");
  await assert.rejects(
    createWorkspaceFileSnapshot({
      sourcePath: source,
      authorizedRoot: root,
      snapshotDirectory: snapshots,
      maxBytes: 1024,
      syncDirectory: async () => {
        throw Object.assign(new Error("simulated directory fsync failure"), {
          code: "EIO",
        });
      },
    }),
    (error) => error.code === "EIO",
  );
  assert.deepEqual(fs.readdirSync(snapshots), []);
});

test("workspace snapshot rejects a source swapped to an outside symlink after open", async (t) => {
  const { temp, root, snapshots } = fixture(t);
  const source = path.join(root, "brief.md");
  const outside = path.join(temp, "private.md");
  fs.writeFileSync(source, "safe");
  fs.writeFileSync(outside, "private");

  await assert.rejects(
    createWorkspaceFileSnapshot({
      sourcePath: source,
      authorizedRoot: root,
      snapshotDirectory: snapshots,
      maxBytes: 1024,
      afterSourceOpen: async () => {
        fs.unlinkSync(source);
        fs.symlinkSync(outside, source);
      },
    }),
    (error) => error.code === "WORKSPACE_SOURCE_INVALID",
  );
  assert.deepEqual(fs.readdirSync(snapshots), []);
});

test("workspace snapshot detects growth beyond the cap and removes the partial copy", async (t) => {
  const { root, snapshots } = fixture(t);
  const source = path.join(root, "growing.txt");
  fs.writeFileSync(source, "small");

  await assert.rejects(
    createWorkspaceFileSnapshot({
      sourcePath: source,
      authorizedRoot: root,
      snapshotDirectory: snapshots,
      maxBytes: 16,
      afterSourceValidated: async () => {
        fs.appendFileSync(source, "x".repeat(64));
      },
    }),
    (error) => error.code === "WORKSPACE_SOURCE_TOO_LARGE",
  );
  assert.deepEqual(fs.readdirSync(snapshots), []);
});

test("workspace snapshot cancellation leaves no temporary file", async (t) => {
  const { root, snapshots } = fixture(t);
  const source = path.join(root, "cancel.txt");
  fs.writeFileSync(source, "cancel me");
  const controller = new AbortController();

  await assert.rejects(
    createWorkspaceFileSnapshot({
      sourcePath: source,
      authorizedRoot: root,
      snapshotDirectory: snapshots,
      maxBytes: 1024,
      signal: controller.signal,
      afterSourceValidated: async () => controller.abort(new Error("cancelled")),
    }),
    /cancelled/,
  );
  assert.deepEqual(fs.readdirSync(snapshots), []);
});

test("snapshot directory cleanup reports failure and schedules one safe retry", async () => {
  const messages = [];
  const scheduled = [];
  let attempts = 0;
  const removed = await cleanupWorkspaceSnapshotDirectory("/private/snapshot", {
    remove: async () => {
      attempts += 1;
      if (attempts === 1) {
        const error = new Error("private path must not be logged");
        error.code = "EBUSY";
        throw error;
      }
    },
    logger: (message) => messages.push(message),
    schedule: (callback) => {
      scheduled.push(callback);
      return { unref() {} };
    },
  });
  assert.equal(removed, false);
  assert.deepEqual(messages, ["workspace snapshot cleanup failed [EBUSY]"]);
  assert.equal(scheduled.length, 1);
  scheduled[0]();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(attempts, 2);
});

test("stale private snapshot directories are swept without touching fresh scans", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-snapshot-sweep-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const stale = path.join(root, `${WORKSPACE_SNAPSHOT_PREFIX}stale`);
  const fresh = path.join(root, `${WORKSPACE_SNAPSHOT_PREFIX}fresh`);
  fs.mkdirSync(stale, { mode: 0o700 });
  fs.mkdirSync(fresh, { mode: 0o700 });
  fs.writeFileSync(path.join(stale, "secret.snapshot"), "private", { mode: 0o600 });
  const now = Date.now();
  fs.utimesSync(stale, new Date(now - 48 * 60 * 60 * 1000), new Date(now - 48 * 60 * 60 * 1000));

  const removed = await cleanupStaleWorkspaceSnapshotDirs(root, { now });
  assert.equal(removed, 1);
  assert.equal(fs.existsSync(stale), false);
  assert.equal(fs.existsSync(fresh), true);
});

test("stale sweep retains directories referenced by durable upload intents", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-snapshot-protected-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const protectedDir = path.join(root, `${WORKSPACE_SNAPSHOT_PREFIX}pending`);
  fs.mkdirSync(protectedDir, { mode: 0o700 });
  fs.writeFileSync(path.join(protectedDir, "pending.snapshot"), "private", {
    mode: 0o600,
  });
  const now = Date.now();
  fs.utimesSync(
    protectedDir,
    new Date(now - 48 * 60 * 60 * 1000),
    new Date(now - 48 * 60 * 60 * 1000),
  );

  const removed = await cleanupStaleWorkspaceSnapshotDirs(root, {
    now,
    protectedDirectories: [protectedDir],
  });
  assert.equal(removed, 0);
  assert.equal(fs.existsSync(protectedDir), true);
});

test("retained upload recovery accepts only private direct-child snapshots", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), WORKSPACE_SNAPSHOT_PREFIX));
  const output = fs.mkdtempSync(path.join(os.tmpdir(), WORKSPACE_SNAPSHOT_PREFIX));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  t.after(() => fs.rmSync(output, { recursive: true, force: true }));
  fs.chmodSync(root, 0o700);
  fs.chmodSync(output, 0o700);
  const retained = path.join(root, "pending.snapshot");
  fs.writeFileSync(retained, "durable bytes", { mode: 0o600 });

  const copied = await copyRetainedWorkspaceSnapshot({
    retainedPath: retained,
    snapshotDirectory: output,
    expectedSha256:
      "849e9d3592edcb72635d1e74af2b7ded2c07f6b79f4b27de7e4bc2e507169213",
    expectedSize: Buffer.byteLength("durable bytes"),
    maxBytes: 1024,
  });
  assert.equal(fs.readFileSync(copied.path, "utf8"), "durable bytes");

  const outside = path.join(os.tmpdir(), `echodesk-outside-${process.pid}.snapshot`);
  fs.writeFileSync(outside, "outside", { mode: 0o600 });
  t.after(() => fs.rmSync(outside, { force: true }));
  await assert.rejects(
    copyRetainedWorkspaceSnapshot({
      retainedPath: outside,
      snapshotDirectory: output,
      expectedSha256: "0".repeat(64),
      expectedSize: 7,
      maxBytes: 1024,
    }),
    (error) => error.code === "WORKSPACE_SOURCE_INVALID",
  );
});

test("durable snapshot root is private and explicitly coexists with legacy tmp recovery", async (t) => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-durable-snapshot-"));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const durableRoot = ensurePrivateWorkspaceSnapshotRoot(
    path.join(temp, "user-data", "workspace-upload-snapshots"),
  );
  assert.equal(fs.statSync(durableRoot).mode & 0o777, 0o700);

  const durableDirectory = fs.mkdtempSync(
    path.join(durableRoot, WORKSPACE_SNAPSHOT_PREFIX),
  );
  fs.chmodSync(durableDirectory, 0o700);
  const durableSnapshot = path.join(durableDirectory, "durable.snapshot");
  fs.writeFileSync(durableSnapshot, "durable", { mode: 0o600 });

  const legacyDirectory = fs.mkdtempSync(
    path.join(os.tmpdir(), WORKSPACE_SNAPSHOT_PREFIX),
  );
  t.after(() => fs.rmSync(legacyDirectory, { recursive: true, force: true }));
  fs.chmodSync(legacyDirectory, 0o700);
  const legacySnapshot = path.join(legacyDirectory, "legacy.snapshot");
  fs.writeFileSync(legacySnapshot, "legacy", { mode: 0o600 });

  const output = fs.mkdtempSync(path.join(os.tmpdir(), WORKSPACE_SNAPSHOT_PREFIX));
  t.after(() => fs.rmSync(output, { recursive: true, force: true }));
  fs.chmodSync(output, 0o700);
  const allowedRoots = [durableRoot, os.tmpdir()];
  for (const [retainedPath, content] of [
    [durableSnapshot, "durable"],
    [legacySnapshot, "legacy"],
  ]) {
    const copied = await copyRetainedWorkspaceSnapshot({
      retainedPath,
      snapshotDirectory: output,
      expectedSha256: crypto.createHash("sha256").update(content).digest("hex"),
      expectedSize: Buffer.byteLength(content),
      maxBytes: 1024,
      allowedRoots,
    });
    assert.equal(fs.readFileSync(copied.path, "utf8"), content);
    fs.unlinkSync(copied.path);
  }

  const unlistedRoot = path.join(temp, "unlisted");
  fs.mkdirSync(unlistedRoot, { mode: 0o700 });
  const unlistedDirectory = fs.mkdtempSync(
    path.join(unlistedRoot, WORKSPACE_SNAPSHOT_PREFIX),
  );
  fs.chmodSync(unlistedDirectory, 0o700);
  const unlistedSnapshot = path.join(unlistedDirectory, "blocked.snapshot");
  fs.writeFileSync(unlistedSnapshot, "blocked", { mode: 0o600 });
  await assert.rejects(
    copyRetainedWorkspaceSnapshot({
      retainedPath: unlistedSnapshot,
      snapshotDirectory: output,
      expectedSha256: crypto.createHash("sha256").update("blocked").digest("hex"),
      expectedSize: Buffer.byteLength("blocked"),
      maxBytes: 1024,
      allowedRoots,
    }),
    (error) => error.code === "WORKSPACE_SOURCE_INVALID",
  );

  assert.equal(
    await removeRetainedWorkspaceSnapshotFile(durableSnapshot, { allowedRoots }),
    true,
  );
  assert.equal(
    await removeRetainedWorkspaceSnapshotFile(legacySnapshot, { allowedRoots }),
    true,
  );
});

test("Windows recovery ignores POSIX mode bits without weakening path or file checks", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), WORKSPACE_SNAPSHOT_PREFIX));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const snapshot = path.join(directory, "windows.snapshot");
  fs.writeFileSync(snapshot, "windows", { mode: 0o666 });
  fs.chmodSync(directory, 0o777);
  fs.chmodSync(snapshot, 0o666);

  await assert.rejects(
    validateRetainedWorkspaceSnapshot(snapshot, {
      allowedRoots: [os.tmpdir()],
      platform: "linux",
    }),
    (error) => error.code === "WORKSPACE_SOURCE_INVALID",
  );
  const retained = await validateRetainedWorkspaceSnapshot(snapshot, {
    allowedRoots: [os.tmpdir()],
    platform: "win32",
  });
  assert.equal(retained.path, path.resolve(snapshot));

  const outside = path.join(path.dirname(directory), "outside.snapshot");
  fs.writeFileSync(outside, "outside", { mode: 0o666 });
  t.after(() => fs.rmSync(outside, { force: true }));
  await assert.rejects(
    validateRetainedWorkspaceSnapshot(outside, {
      allowedRoots: [os.tmpdir()],
      platform: "win32",
    }),
    (error) => error.code === "WORKSPACE_SOURCE_INVALID",
  );
});

test("converging one of two pending snapshots never destroys its sibling", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), WORKSPACE_SNAPSHOT_PREFIX));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  fs.chmodSync(directory, 0o700);
  const first = path.join(directory, "first.snapshot");
  const second = path.join(directory, "second.snapshot");
  fs.writeFileSync(first, "first", { mode: 0o600 });
  fs.writeFileSync(second, "second", { mode: 0o600 });

  assert.equal(await removeRetainedWorkspaceSnapshotFile(first), true);
  assert.equal(fs.existsSync(first), false);
  assert.equal(fs.readFileSync(second, "utf8"), "second");
  assert.equal(fs.existsSync(directory), true);

  assert.equal(await removeRetainedWorkspaceSnapshotFile(second), true);
  assert.equal(fs.existsSync(directory), false);
});

test("failed retained-snapshot recovery does not delete a sibling intent", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), WORKSPACE_SNAPSHOT_PREFIX));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  fs.chmodSync(directory, 0o700);
  const invalid = path.join(directory, "invalid.snapshot");
  const sibling = path.join(directory, "sibling.snapshot");
  fs.writeFileSync(invalid, "invalid", { mode: 0o644 });
  fs.writeFileSync(sibling, "recover me", { mode: 0o600 });

  assert.equal(await removeRetainedWorkspaceSnapshotFile(invalid), false);
  assert.equal(fs.readFileSync(sibling, "utf8"), "recover me");
  assert.equal(fs.existsSync(directory), true);
});
