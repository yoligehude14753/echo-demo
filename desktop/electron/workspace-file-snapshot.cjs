"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SNAPSHOT_READ_BYTES = 64 * 1024;
const WORKSPACE_SNAPSHOT_PREFIX = "echodesk-workspace-scan-";
const WORKSPACE_STALE_SNAPSHOT_MS = 24 * 60 * 60 * 1000;

class WorkspaceSnapshotError extends Error {
  constructor(message, code, { cause } = {}) {
    super(message, { cause });
    this.name = "WorkspaceSnapshotError";
    this.code = code;
  }
}

function snapshotError(code, cause = undefined) {
  const messages = {
    WORKSPACE_SNAPSHOT_ROOT_INVALID:
      "workspace snapshot root is not a private stable directory",
    WORKSPACE_SOURCE_INVALID: "workspace source is not a stable regular file",
    WORKSPACE_SOURCE_OUTSIDE_ROOT: "workspace source escaped its authorized root",
    WORKSPACE_SOURCE_TOO_LARGE: "workspace source exceeds the configured byte limit",
  };
  return new WorkspaceSnapshotError(messages[code] || "workspace snapshot failed", code, {
    cause,
  });
}

function ensurePrivateWorkspaceSnapshotRoot(rawRoot) {
  if (!path.isAbsolute(String(rawRoot || ""))) {
    throw new TypeError("workspace snapshot root requires an absolute path");
  }
  const root = path.resolve(rawRoot);
  try {
    fs.mkdirSync(root, { recursive: true, mode: 0o700 });
    const before = fs.lstatSync(root);
    const uid = typeof process.getuid === "function" ? process.getuid() : null;
    if (
      !before.isDirectory() ||
      before.isSymbolicLink() ||
      (uid !== null && before.uid !== uid)
    ) {
      throw snapshotError("WORKSPACE_SNAPSHOT_ROOT_INVALID");
    }
    fs.chmodSync(root, 0o700);
    const canonical = fs.realpathSync.native(root);
    const after = fs.lstatSync(root);
    if (
      !after.isDirectory() ||
      after.isSymbolicLink() ||
      after.dev !== before.dev ||
      after.ino !== before.ino ||
      (uid !== null && after.uid !== uid) ||
      (process.platform !== "win32" && (after.mode & 0o077) !== 0)
    ) {
      throw snapshotError("WORKSPACE_SNAPSHOT_ROOT_INVALID");
    }
    return canonical;
  } catch (cause) {
    if (cause instanceof WorkspaceSnapshotError) throw cause;
    throw snapshotError("WORKSPACE_SNAPSHOT_ROOT_INVALID", cause);
  }
}

async function allowedSnapshotRoots(rawRoots) {
  if (!Array.isArray(rawRoots) || rawRoots.length === 0) {
    throw new TypeError("workspace snapshot validation requires allowed roots");
  }
  const roots = [];
  for (const rawRoot of rawRoots) {
    if (!path.isAbsolute(String(rawRoot || ""))) continue;
    const resolved = path.resolve(rawRoot);
    try {
      const canonical = await fs.promises.realpath(resolved);
      const stat = await fs.promises.stat(resolved);
      if (stat.isDirectory()) roots.push({ resolved, canonical });
    } catch {
      // A missing allowlisted root grants no authority. Other roots remain usable.
    }
  }
  return roots;
}

function pathContains(rawRoot, rawTarget) {
  const root = path.resolve(String(rawRoot || ""));
  const target = path.resolve(String(rawTarget || ""));
  const relative = path.relative(root, target);
  return (
    relative === "" ||
    (relative !== ".." &&
      !relative.startsWith(`..${path.sep}`) &&
      !path.isAbsolute(relative))
  );
}

function throwIfAborted(signal) {
  if (!signal?.aborted) return;
  if (signal.reason instanceof Error) throw signal.reason;
  throw new DOMException("workspace snapshot cancelled", "AbortError");
}

function sameFileIdentity(left, right) {
  if (left.dev !== right.dev || left.ino !== right.ino) return false;
  return left.isFile() && right.isFile();
}

function cleanupFailureCode(error) {
  return String(error?.code || error?.name || "ERROR")
    .toUpperCase()
    .replace(/[^A-Z0-9_-]/g, "_")
    .slice(0, 64) || "ERROR";
}

async function syncSnapshotParentDirectory(directory) {
  if (process.platform === "win32") return;
  let handle = null;
  try {
    handle = await fs.promises.open(
      directory,
      fs.constants.O_RDONLY | (fs.constants.O_DIRECTORY || 0),
    );
    await handle.sync();
  } finally {
    await handle?.close();
  }
}

async function validateRetainedWorkspaceSnapshot(
  retainedPath,
  { allowedRoots = [os.tmpdir()], platform = process.platform } = {},
) {
  const resolvedPath = path.resolve(String(retainedPath || ""));
  const directory = path.dirname(resolvedPath);
  const roots = await allowedSnapshotRoots(allowedRoots);
  const authorizedRoot = roots.find(
    (candidate) => path.dirname(directory) === candidate.resolved,
  );
  if (
    !path.isAbsolute(String(retainedPath || "")) ||
    !authorizedRoot ||
    !path.basename(directory).startsWith(WORKSPACE_SNAPSHOT_PREFIX) ||
    !path.basename(resolvedPath).endsWith(".snapshot")
  ) {
    throw snapshotError("WORKSPACE_SOURCE_INVALID");
  }
  const uid = typeof process.getuid === "function" ? process.getuid() : null;
  const enforcePosixMode = platform !== "win32";
  let directoryStat;
  let sourceStat;
  let canonicalDirectory;
  try {
    [directoryStat, sourceStat, canonicalDirectory] = await Promise.all([
      fs.promises.lstat(directory),
      fs.promises.lstat(resolvedPath),
      fs.promises.realpath(directory),
    ]);
  } catch (cause) {
    throw snapshotError("WORKSPACE_SOURCE_INVALID", cause);
  }
  if (
    !directoryStat.isDirectory() ||
    directoryStat.isSymbolicLink() ||
    (enforcePosixMode && (directoryStat.mode & 0o077) !== 0) ||
    !sourceStat.isFile() ||
    sourceStat.isSymbolicLink() ||
    (enforcePosixMode && (sourceStat.mode & 0o077) !== 0) ||
    path.dirname(canonicalDirectory) !== authorizedRoot.canonical ||
    path.basename(canonicalDirectory) !== path.basename(directory) ||
    (uid !== null && (directoryStat.uid !== uid || sourceStat.uid !== uid))
  ) {
    throw snapshotError("WORKSPACE_SOURCE_INVALID");
  }
  return { path: resolvedPath, directory, canonicalDirectory };
}

async function copyRetainedWorkspaceSnapshot({
  retainedPath,
  snapshotDirectory,
  expectedSha256,
  expectedSize,
  maxBytes,
  signal = undefined,
  allowedRoots = [os.tmpdir()],
}) {
  const retained = await validateRetainedWorkspaceSnapshot(retainedPath, {
    allowedRoots,
  });
  const copied = await createWorkspaceFileSnapshot({
    sourcePath: retained.path,
    authorizedRoot: retained.canonicalDirectory,
    snapshotDirectory,
    maxBytes,
    signal,
  });
  if (copied.sha256 !== expectedSha256 || copied.size !== expectedSize) {
    try {
      await fs.promises.unlink(copied.path);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    throw snapshotError("WORKSPACE_SOURCE_INVALID");
  }
  return copied;
}

async function removeRetainedWorkspaceSnapshotFile(
  retainedPath,
  { logger = () => {}, allowedRoots = [os.tmpdir()] } = {},
) {
  let retained;
  try {
    retained = await validateRetainedWorkspaceSnapshot(retainedPath, {
      allowedRoots,
    });
    await fs.promises.unlink(retained.path);
  } catch (error) {
    if (error?.code !== "ENOENT") {
      logger(`workspace retained snapshot cleanup failed [${cleanupFailureCode(error)}]`);
    }
    return false;
  }
  try {
    // Multiple durable intents from one scan intentionally share a private
    // directory. Remove it only when the last snapshot has converged.
    await fs.promises.rmdir(retained.directory);
  } catch (error) {
    if (error?.code !== "ENOTEMPTY" && error?.code !== "EEXIST") {
      logger(`workspace retained directory cleanup deferred [${cleanupFailureCode(error)}]`);
    }
  }
  return true;
}

async function cleanupWorkspaceSnapshotDirectory(
  directory,
  {
    remove = fs.promises.rm,
    logger = () => {},
    schedule = setTimeout,
    retryDelayMs = 5_000,
    retry = true,
  } = {},
) {
  try {
    await remove(directory, { recursive: true, force: true });
    return true;
  } catch (error) {
    logger(`workspace snapshot cleanup failed [${cleanupFailureCode(error)}]`);
    if (retry) {
      const timer = schedule(() => {
        void cleanupWorkspaceSnapshotDirectory(directory, {
          remove,
          logger,
          schedule,
          retryDelayMs,
          retry: false,
        });
      }, retryDelayMs);
      timer?.unref?.();
    }
    return false;
  }
}

async function cleanupStaleWorkspaceSnapshotDirs(
  tempRoot,
  {
    now = Date.now(),
    staleAfterMs = WORKSPACE_STALE_SNAPSHOT_MS,
    logger = () => {},
    protectedDirectories = [],
  } = {},
) {
  let entries;
  try {
    entries = await fs.promises.readdir(tempRoot, { withFileTypes: true });
  } catch (error) {
    logger(`workspace snapshot sweep failed [${cleanupFailureCode(error)}]`);
    return 0;
  }
  let removed = 0;
  const protectedPaths = new Set(
    (protectedDirectories || []).map((directory) => path.resolve(directory)),
  );
  const currentUid = typeof process.getuid === "function" ? process.getuid() : null;
  for (const entry of entries) {
    if (!entry.isDirectory() || !entry.name.startsWith(WORKSPACE_SNAPSHOT_PREFIX)) {
      continue;
    }
    const candidate = path.join(tempRoot, entry.name);
    if (protectedPaths.has(path.resolve(candidate))) continue;
    try {
      const stat = await fs.promises.lstat(candidate);
      if (
        !stat.isDirectory() ||
        stat.isSymbolicLink() ||
        (currentUid !== null && stat.uid !== currentUid) ||
        now - stat.mtimeMs < staleAfterMs
      ) {
        continue;
      }
      if (
        await cleanupWorkspaceSnapshotDirectory(candidate, {
          logger,
          retry: false,
        })
      ) {
        removed += 1;
      }
    } catch (error) {
      logger(`workspace snapshot sweep entry failed [${cleanupFailureCode(error)}]`);
    }
  }
  return removed;
}

async function createWorkspaceFileSnapshot({
  sourcePath,
  authorizedRoot,
  snapshotDirectory,
  maxBytes,
  signal = undefined,
  // Deterministic race hooks are test-only. Production callers leave them unset.
  afterSourceOpen = undefined,
  afterSourceValidated = undefined,
  syncDirectory = syncSnapshotParentDirectory,
  onCleanupError = () => {},
}) {
  if (
    !path.isAbsolute(String(sourcePath || "")) ||
    !path.isAbsolute(String(authorizedRoot || "")) ||
    !path.isAbsolute(String(snapshotDirectory || "")) ||
    !Number.isSafeInteger(maxBytes) ||
    maxBytes < 1
  ) {
    throw new TypeError("workspace snapshot arguments are invalid");
  }
  throwIfAborted(signal);

  let source = null;
  let target = null;
  let targetPath = null;
  let completed = false;
  try {
    const noFollow = fs.constants.O_NOFOLLOW || 0;
    source = await fs.promises.open(sourcePath, fs.constants.O_RDONLY | noFollow);
    const openedStat = await source.stat();
    if (!openedStat.isFile()) throw snapshotError("WORKSPACE_SOURCE_INVALID");
    if (openedStat.size > maxBytes) {
      throw snapshotError("WORKSPACE_SOURCE_TOO_LARGE");
    }
    if (afterSourceOpen) await afterSourceOpen();
    throwIfAborted(signal);

    let currentStat;
    let canonicalSource;
    try {
      [currentStat, canonicalSource] = await Promise.all([
        fs.promises.lstat(sourcePath),
        fs.promises.realpath(sourcePath),
      ]);
    } catch (cause) {
      throw snapshotError("WORKSPACE_SOURCE_INVALID", cause);
    }
    if (!sameFileIdentity(openedStat, currentStat)) {
      throw snapshotError("WORKSPACE_SOURCE_INVALID");
    }
    // authorizedRoot is a canonical root captured before enumeration. Do not
    // resolve it again here: a swapped ancestor symlink must not redefine the
    // authorization boundary between enumeration and ingest.
    if (!pathContains(authorizedRoot, canonicalSource)) {
      throw snapshotError("WORKSPACE_SOURCE_OUTSIDE_ROOT");
    }
    if (afterSourceValidated) await afterSourceValidated();
    throwIfAborted(signal);

    targetPath = path.join(
      snapshotDirectory,
      `${crypto.randomBytes(16).toString("hex")}.snapshot`,
    );
    target = await fs.promises.open(
      targetPath,
      fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY,
      0o600,
    );
    const digest = crypto.createHash("sha256");
    const buffer = Buffer.allocUnsafe(SNAPSHOT_READ_BYTES);
    let size = 0;
    let position = 0;
    while (true) {
      throwIfAborted(signal);
      const { bytesRead } = await source.read(
        buffer,
        0,
        buffer.byteLength,
        position,
      );
      if (bytesRead === 0) break;
      size += bytesRead;
      if (size > maxBytes) {
        throw snapshotError("WORKSPACE_SOURCE_TOO_LARGE");
      }
      const chunk = buffer.subarray(0, bytesRead);
      await target.write(chunk, 0, bytesRead, null);
      digest.update(chunk);
      position += bytesRead;
    }
    throwIfAborted(signal);
    await target.sync();
    await target.close();
    target = null;
    await fs.promises.chmod(targetPath, 0o600);
    // fsync(file) persists bytes; fsync(parent) persists the directory entry.
    // The durable upload intent must never point at a name lost on power failure.
    await syncDirectory(snapshotDirectory);
    completed = true;
    return {
      path: targetPath,
      sha256: digest.digest("hex"),
      size,
      mtime: openedStat.mtimeMs,
    };
  } finally {
    if (target) {
      try {
        await target.close();
      } catch (error) {
        onCleanupError(error);
      }
    }
    if (source) {
      try {
        await source.close();
      } catch (error) {
        onCleanupError(error);
      }
    }
    if (!completed && targetPath) {
      try {
        await fs.promises.unlink(targetPath);
      } catch (error) {
        if (error?.code !== "ENOENT") onCleanupError(error);
      }
    }
  }
}

module.exports = {
  WORKSPACE_SNAPSHOT_PREFIX,
  WorkspaceSnapshotError,
  cleanupStaleWorkspaceSnapshotDirs,
  cleanupWorkspaceSnapshotDirectory,
  copyRetainedWorkspaceSnapshot,
  createWorkspaceFileSnapshot,
  ensurePrivateWorkspaceSnapshotRoot,
  removeRetainedWorkspaceSnapshotFile,
  syncSnapshotParentDirectory,
  validateRetainedWorkspaceSnapshot,
};
