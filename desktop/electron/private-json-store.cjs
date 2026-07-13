"use strict";

const fs = require("node:fs");
const path = require("node:path");

class PrivateJsonStoreError extends Error {
  constructor(message, code, { cause } = {}) {
    super(message, { cause });
    this.name = "PrivateJsonStoreError";
    this.code = code;
  }
}

function storeError(code, cause = undefined) {
  const messages = {
    PRIVATE_STORE_INVALID: "private JSON store is not a stable regular file",
    PRIVATE_STORE_OWNER_INVALID: "private JSON store has an unexpected owner",
    PRIVATE_STORE_JSON_INVALID: "private JSON store contains invalid JSON",
    PRIVATE_STORE_DIRECTORY_INVALID: "private JSON store directory is unsafe",
  };
  return new PrivateJsonStoreError(messages[code] || "private JSON store failed", code, {
    cause,
  });
}

function currentUid() {
  return typeof process.getuid === "function" ? process.getuid() : null;
}

function assertOwned(stat, code = "PRIVATE_STORE_OWNER_INVALID") {
  const uid = currentUid();
  if (uid !== null && stat.uid !== uid) throw storeError(code);
}

function sameIdentity(left, right) {
  return left.dev === right.dev && left.ino === right.ino;
}

function safeParentDirectory(target) {
  const parent = path.dirname(path.resolve(target));
  let stat;
  try {
    stat = fs.lstatSync(parent);
  } catch (cause) {
    throw storeError("PRIVATE_STORE_DIRECTORY_INVALID", cause);
  }
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw storeError("PRIVATE_STORE_DIRECTORY_INVALID");
  }
  assertOwned(stat, "PRIVATE_STORE_DIRECTORY_INVALID");
  return parent;
}

function readPrivateJsonFile(target) {
  let before;
  try {
    before = fs.lstatSync(target);
  } catch (cause) {
    if (cause?.code === "ENOENT") return null;
    throw storeError("PRIVATE_STORE_INVALID", cause);
  }
  if (!before.isFile() || before.isSymbolicLink()) {
    throw storeError("PRIVATE_STORE_INVALID");
  }
  assertOwned(before);

  let fd = null;
  try {
    fd = fs.openSync(
      target,
      fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0),
    );
    const opened = fs.fstatSync(fd);
    if (!opened.isFile() || !sameIdentity(before, opened)) {
      throw storeError("PRIVATE_STORE_INVALID");
    }
    assertOwned(opened);
    fs.fchmodSync(fd, 0o600);
    const raw = fs.readFileSync(fd, "utf8");
    try {
      return JSON.parse(raw);
    } catch (cause) {
      throw storeError("PRIVATE_STORE_JSON_INVALID", cause);
    }
  } catch (cause) {
    if (cause instanceof PrivateJsonStoreError) throw cause;
    throw storeError("PRIVATE_STORE_INVALID", cause);
  } finally {
    if (fd !== null) fs.closeSync(fd);
  }
}

function fsyncParentDirectory(parent) {
  if (process.platform === "win32") return;
  let fd = null;
  try {
    fd = fs.openSync(
      parent,
      fs.constants.O_RDONLY | (fs.constants.O_DIRECTORY || 0),
    );
    fs.fsyncSync(fd);
  } finally {
    if (fd !== null) fs.closeSync(fd);
  }
}

function atomicWritePrivateJsonFile(target, value, { randomSuffix } = {}) {
  const parent = safeParentDirectory(target);
  const suffix =
    typeof randomSuffix === "function"
      ? String(randomSuffix())
      : require("node:crypto").randomBytes(12).toString("hex");
  if (!/^[A-Za-z0-9_-]{8,128}$/.test(suffix)) {
    throw new TypeError("private JSON store suffix is invalid");
  }
  const temp = path.join(parent, `.${path.basename(target)}.${suffix}.tmp`);
  let fd = null;
  try {
    fd = fs.openSync(
      temp,
      fs.constants.O_CREAT |
        fs.constants.O_EXCL |
        fs.constants.O_WRONLY |
        (fs.constants.O_NOFOLLOW || 0),
      0o600,
    );
    const opened = fs.fstatSync(fd);
    if (!opened.isFile()) throw storeError("PRIVATE_STORE_INVALID");
    assertOwned(opened);
    fs.writeFileSync(fd, JSON.stringify(value, null, 2), "utf8");
    fs.fchmodSync(fd, 0o600);
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = null;

    fs.renameSync(temp, target);
    // Re-open without following links and verify the renamed inode before
    // acknowledging the commit. This also repairs legacy permissive modes.
    const committed = readPrivateJsonFile(target);
    fsyncParentDirectory(parent);
    return committed;
  } finally {
    if (fd !== null) fs.closeSync(fd);
    try {
      fs.unlinkSync(temp);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
}

module.exports = {
  PrivateJsonStoreError,
  atomicWritePrivateJsonFile,
  readPrivateJsonFile,
};
