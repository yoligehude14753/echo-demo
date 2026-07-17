/* B12 package layout resolver: package-relative, hash-verified and fail-closed. */
const {
  createHash,
} = require("node:crypto");
const {
  lstatSync,
  readFileSync,
  realpathSync,
} = require("node:fs");
const path = require("node:path");

const PACKAGE_LAYOUT_RESOLVER_ID = "echo.b12.package-layout-resolver.v1";
const SHA256_PATTERN = /^[0-9a-f]{64}$/i;
const PLACEMENTS = new Set(["asar", "asarUnpack", "extraResources", "auto"]);

class PackageLayoutError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "PackageLayoutError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new PackageLayoutError(code, message);
}

function isWindowsAbsolute(value) {
  return /^[A-Za-z]:[\\/]/.test(value) || /^\\\\/.test(value);
}

function normalizeHash(value, field) {
  const hash = String(value || "").trim().replace(/^sha256:/i, "").toLowerCase();
  if (!SHA256_PATTERN.test(hash)) fail("PACKAGE_RESOURCE_HASH_INVALID", `${field} must be a SHA-256 digest`);
  return hash;
}

function normalizePlacement(value) {
  const placement = String(value || "").trim();
  if (!PLACEMENTS.has(placement)) {
    fail("PACKAGE_RESOURCE_PLACEMENT_INVALID", "placement must be asar, asarUnpack, extraResources or auto");
  }
  return placement;
}

function normalizeResourcePath(value, placement) {
  const normalizedPlacement = normalizePlacement(placement);
  const raw = String(value || "");
  if (!raw || raw.includes("\0") || raw.includes("\\") || path.posix.isAbsolute(raw) || isWindowsAbsolute(raw)) {
    fail("PACKAGE_RESOURCE_PATH_INVALID", "resource path must be a relative POSIX path");
  }

  let parts = raw.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) {
    fail("PACKAGE_RESOURCE_PATH_INVALID", "resource path contains an empty, dot or parent segment");
  }

  if (parts[0] === "Resources") parts = parts.slice(1);
  if (normalizedPlacement === "asar" && parts[0] === "app.asar") parts = parts.slice(1);
  if (normalizedPlacement === "asarUnpack" && parts[0] === "app.asar.unpacked") parts = parts.slice(1);
  if (parts.length === 0) fail("PACKAGE_RESOURCE_PATH_INVALID", "resource path is empty");

  return parts.join("/");
}

function resourcesRoot(resourcesPath = process.resourcesPath) {
  const raw = String(resourcesPath || "");
  if (!path.isAbsolute(raw) || isWindowsAbsolute(raw) && path.sep !== "\\") {
    fail("PACKAGE_RESOURCES_ROOT_INVALID", "resourcesPath must be an absolute local path");
  }

  const configured = path.normalize(raw);
  const candidate = path.basename(configured).toLowerCase() === "resources"
    ? configured
    : path.join(configured, "Resources");
  let stat;
  try {
    stat = lstatSync(candidate);
  } catch {
    fail("PACKAGE_RESOURCES_ROOT_MISSING", "Resources root does not exist");
  }
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    fail("PACKAGE_RESOURCES_ROOT_INVALID", "Resources root must be a real directory");
  }
  return realpathSync.native(candidate);
}

function candidatePaths(root, relativePath, placement) {
  const candidates = [];
  if (placement === "asar" || placement === "auto") candidates.push(path.join(root, "app.asar", relativePath));
  if (placement === "asarUnpack" || placement === "auto") candidates.push(path.join(root, "app.asar.unpacked", relativePath));
  if (placement === "extraResources" || placement === "auto") candidates.push(path.join(root, relativePath));
  return candidates;
}

function rejectSymlinkComponents(root, candidate) {
  const relative = path.relative(root, candidate);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    fail("PACKAGE_RESOURCE_PATH_ESCAPE", "resolved resource escaped Resources root");
  }
  let current = root;
  for (const component of relative.split(path.sep)) {
    current = path.join(current, component);
    let stat;
    try {
      stat = lstatSync(current);
    } catch {
      return;
    }
    if (stat.isSymbolicLink()) fail("PACKAGE_RESOURCE_SYMLINK", "symbolic-link resource components are forbidden");
    // Electron exposes app.asar as a virtual archive file; its children are
    // resolved by Electron and cannot be inspected as host filesystem nodes.
    if (component === "app.asar" && !stat.isDirectory()) return;
  }
}

function selectCandidate(root, relativePath, placement) {
  const candidates = candidatePaths(root, relativePath, placement);
  const existing = candidates.filter((candidate) => {
    try {
      const stat = lstatSync(candidate);
      return stat.isSymbolicLink() || stat.isFile() || (candidate.includes(`${path.sep}app.asar${path.sep}`) && !stat.isDirectory());
    } catch {
      return false;
    }
  });
  if (existing.length === 0) fail("PACKAGE_RESOURCE_MISSING", `resource is not present: ${relativePath}`);
  if (existing.length > 1) fail("PACKAGE_RESOURCE_AMBIGUOUS", `resource has multiple package placements: ${relativePath}`);
  const selected = existing[0];
  rejectSymlinkComponents(root, selected);
  return selected;
}

function resolvePackageResource(entry, options = {}) {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
    fail("PACKAGE_RESOURCE_ENTRY_INVALID", "manifest resource entry must be an object");
  }
  const placement = normalizePlacement(entry.placement || entry.load_mode);
  const relativePath = normalizeResourcePath(
    entry.path || entry.package_relative_path || entry.packageRelativePath,
    placement,
  );
  const expectedSize = entry.size === undefined ? entry.size_bytes : entry.size;
  if (!Number.isSafeInteger(expectedSize) || expectedSize < 0) {
    fail("PACKAGE_RESOURCE_SIZE_INVALID", "manifest resource size must be a non-negative integer");
  }
  const expectedHash = normalizeHash(entry.sha256 || entry.sha_256, "manifest resource sha256");
  const root = resourcesRoot(options.resourcesPath);
  const resolvedPath = selectCandidate(root, relativePath, placement);
  const bytes = readFileSync(resolvedPath);
  const actualHash = createHash("sha256").update(bytes).digest("hex");
  if (bytes.length !== expectedSize) {
    fail("PACKAGE_RESOURCE_SIZE_MISMATCH", `resource size mismatch: ${relativePath}`);
  }
  if (actualHash !== expectedHash) {
    fail("PACKAGE_RESOURCE_HASH_MISMATCH", `resource hash mismatch: ${relativePath}`);
  }
  return Object.freeze({
    resolver: PACKAGE_LAYOUT_RESOLVER_ID,
    path: relativePath,
    resolvedPath,
    placement,
    size: bytes.length,
    sha256: `sha256:${actualHash}`,
    executable: entry.executable === true,
    role: String(entry.role || "").trim(),
    platform: String(entry.platform || "").trim(),
    arch: String(entry.arch || "").trim(),
  });
}

module.exports = {
  PACKAGE_LAYOUT_RESOLVER_ID,
  PackageLayoutError,
  normalizeResourcePath,
  resourcesRoot,
  resolvePackageResource,
};
