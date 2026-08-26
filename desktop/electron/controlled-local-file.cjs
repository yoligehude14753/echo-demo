"use strict";

const fs = require("node:fs");
const path = require("node:path");

const LOCAL_ARTIFACT_EXTENSIONS = new Set([
  ".pptx",
  ".docx",
  ".xlsx",
  ".html",
  ".htm",
  ".md",
  ".markdown",
  ".pdf",
  ".txt",
  ".text",
]);

class ControlledLocalFileError extends Error {
  constructor(message, code, { cause } = {}) {
    super(message, { cause });
    this.name = "ControlledLocalFileError";
    this.code = code;
  }
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

function controlledLocalFileError(code, cause = undefined) {
  const messages = {
    ARTIFACT_PATH_INVALID: "artifact path is invalid",
    ARTIFACT_TYPE_FORBIDDEN: "artifact file type is not allowed",
    ARTIFACT_PATH_UNAVAILABLE: "artifact file is unavailable",
    ARTIFACT_PATH_OUTSIDE_CONTROLLED_ROOT:
      "artifact file is outside controlled local storage",
  };
  return new ControlledLocalFileError(messages[code] || "artifact path denied", code, {
    cause,
  });
}

function resolveControlledLocalArtifactPath(
  rawPath,
  rawRoots,
  { allowedExtensions = LOCAL_ARTIFACT_EXTENSIONS } = {},
) {
  if (typeof rawPath !== "string" || !rawPath.trim() || !path.isAbsolute(rawPath)) {
    throw controlledLocalFileError("ARTIFACT_PATH_INVALID");
  }
  const extension = path.extname(rawPath).toLowerCase();
  if (!allowedExtensions.has(extension)) {
    throw controlledLocalFileError("ARTIFACT_TYPE_FORBIDDEN");
  }

  let target;
  try {
    target = fs.realpathSync.native(rawPath);
    if (!fs.statSync(target).isFile()) {
      throw controlledLocalFileError("ARTIFACT_PATH_UNAVAILABLE");
    }
  } catch (cause) {
    if (cause instanceof ControlledLocalFileError) throw cause;
    throw controlledLocalFileError("ARTIFACT_PATH_UNAVAILABLE", cause);
  }

  const controlledRoots = [];
  for (const rawRoot of rawRoots || []) {
    try {
      const root = fs.realpathSync.native(path.resolve(String(rawRoot || "")));
      if (fs.statSync(root).isDirectory()) controlledRoots.push(root);
    } catch {
      // A missing configured root cannot authorize a target. Other roots remain usable.
    }
  }
  if (!controlledRoots.some((root) => pathContains(root, target))) {
    throw controlledLocalFileError("ARTIFACT_PATH_OUTSIDE_CONTROLLED_ROOT");
  }
  return target;
}

module.exports = {
  ControlledLocalFileError,
  LOCAL_ARTIFACT_EXTENSIONS,
  pathContains,
  resolveControlledLocalArtifactPath,
};
