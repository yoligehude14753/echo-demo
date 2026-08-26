"use strict";

const fs = require("node:fs");
const path = require("node:path");

class WorkspaceRootIdentityError extends Error {
  constructor(message, code, { cause } = {}) {
    super(message, { cause });
    this.name = "WorkspaceRootIdentityError";
    this.code = code;
  }
}

function rootError(code, cause = undefined) {
  return new WorkspaceRootIdentityError(
    code === "WORKSPACE_ROOT_IDENTITY_CHANGED"
      ? "workspace root identity changed"
      : "workspace root is not a stable canonical directory",
    code,
    { cause },
  );
}

function identityFromStat(stat) {
  return { dev: String(stat.dev), ino: String(stat.ino) };
}

function sameIdentity(stat, identity) {
  return (
    identity &&
    String(stat.dev) === String(identity.dev) &&
    String(stat.ino) === String(identity.ino)
  );
}

function sameCanonicalWorkspaceRootPath(
  left,
  right,
  platform = process.platform,
) {
  // Windows realpath may preserve different casing than the configured path.
  // It can also return an extended-length `\\?\` spelling for the exact same
  // drive/UNC path.  Normalize only that Windows namespace decoration before
  // comparing; resolving `..` first keeps sibling paths distinct.
  if (platform === "win32") {
    const normalizeWindowsCanonicalPath = (value) => {
      let normalized = path.win32.normalize(path.win32.resolve(value));
      if (/^\\\\\?\\UNC\\/i.test(normalized)) {
        normalized = `\\\\${normalized.slice(8)}`;
      } else if (/^\\\\\?\\/i.test(normalized)) {
        normalized = normalized.slice(4);
      }
      return normalized.toLowerCase();
    };
    return (
      normalizeWindowsCanonicalPath(left) ===
      normalizeWindowsCanonicalPath(right)
    );
  }
  return (
    path.posix.relative(path.posix.resolve(left), path.posix.resolve(right)) ===
    ""
  );
}

async function verifyWorkspaceRootIdentity({
  root,
  expectedIdentity = null,
  afterInitialLstat = undefined,
  platform = process.platform,
}) {
  if (!path.isAbsolute(String(root || ""))) {
    throw new TypeError("workspace root identity requires an absolute path");
  }
  const resolved = path.resolve(root);
  let initial;
  try {
    // Windows file IDs are 64-bit values.  BigIntStats prevents precision loss
    // before the durable identity is serialized as a decimal string.
    initial = await fs.promises.lstat(resolved, { bigint: true });
  } catch (cause) {
    throw rootError("WORKSPACE_ROOT_INVALID", cause);
  }
  if (!initial.isDirectory() || initial.isSymbolicLink()) {
    throw rootError("WORKSPACE_ROOT_INVALID");
  }
  if (expectedIdentity && !sameIdentity(initial, expectedIdentity)) {
    throw rootError("WORKSPACE_ROOT_IDENTITY_CHANGED");
  }
  if (afterInitialLstat) await afterInitialLstat();

  let canonical;
  let current;
  try {
    [canonical, current] = await Promise.all([
      fs.promises.realpath(resolved),
      fs.promises.lstat(resolved, { bigint: true }),
    ]);
  } catch (cause) {
    throw rootError("WORKSPACE_ROOT_INVALID", cause);
  }
  if (
    // Windows can report a short-name and long-name spelling for the same
    // directory across realpath implementations.  Its full 64-bit file ID is
    // the stronger authority; POSIX keeps the strict canonical spelling gate.
    (platform !== "win32" &&
      !sameCanonicalWorkspaceRootPath(canonical, resolved, platform)) ||
    !current.isDirectory() ||
    current.isSymbolicLink() ||
    !sameIdentity(current, identityFromStat(initial)) ||
    (expectedIdentity && !sameIdentity(current, expectedIdentity))
  ) {
    throw rootError("WORKSPACE_ROOT_IDENTITY_CHANGED");
  }
  return { canonical, identity: identityFromStat(current) };
}

module.exports = {
  WorkspaceRootIdentityError,
  sameCanonicalWorkspaceRootPath,
  verifyWorkspaceRootIdentity,
};
