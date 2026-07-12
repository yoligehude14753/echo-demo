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

async function verifyWorkspaceRootIdentity({
  root,
  expectedIdentity = null,
  afterInitialLstat = undefined,
}) {
  if (!path.isAbsolute(String(root || ""))) {
    throw new TypeError("workspace root identity requires an absolute path");
  }
  const resolved = path.resolve(root);
  let initial;
  try {
    initial = await fs.promises.lstat(resolved);
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
      fs.promises.lstat(resolved),
    ]);
  } catch (cause) {
    throw rootError("WORKSPACE_ROOT_INVALID", cause);
  }
  if (
    canonical !== resolved ||
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
  verifyWorkspaceRootIdentity,
};
