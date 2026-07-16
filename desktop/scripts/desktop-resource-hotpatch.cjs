#!/usr/bin/env node
"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync, spawn } = require("node:child_process");
const {
  createStoreZip,
  extractStoreZip,
} = require("./lib/store-zip.cjs");

const SCHEMA = "echodesk.desktop-resource-hotpatch/v1";
const DEFAULT_INCLUDES = ["app.asar", "agent-runtime"];
const ALLOWED_ROOTS = new Set(["app.asar", "agent-runtime", "backend"]);
const SHA_PATTERN = /^[0-9a-f]{40}$/;
const VERSION_PATTERN = /^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$/;

function sha256File(filePath) {
  const hash = crypto.createHash("sha256");
  const fd = fs.openSync(filePath, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (;;) {
      const bytesRead = fs.readSync(fd, buffer, 0, buffer.length, null);
      if (bytesRead === 0) break;
      hash.update(buffer.subarray(0, bytesRead));
    }
  } finally {
    fs.closeSync(fd);
  }
  return hash.digest("hex");
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function manifestDigest(manifest) {
  return crypto.createHash("sha256").update(stableJson(manifest)).digest("hex");
}

function normalizeResourcePath(input) {
  if (
    typeof input !== "string"
    || input.length === 0
    || input.includes("\\")
    || input.startsWith("/")
    || /^[A-Za-z]:/.test(input)
  ) {
    throw new Error(`unsafe resource path: ${input}`);
  }
  const normalized = path.posix.normalize(input);
  if (normalized !== input || normalized === ".." || normalized.startsWith("../")) {
    throw new Error(`unsafe resource path: ${input}`);
  }
  const root = normalized.split("/")[0];
  if (!ALLOWED_ROOTS.has(root)) {
    throw new Error(`resource path is outside the allowlist: ${input}`);
  }
  if (root === "app.asar" && normalized !== "app.asar") {
    throw new Error(`app.asar must be replaced as one file: ${input}`);
  }
  return normalized;
}

function assertRegularFile(filePath) {
  const stat = fs.lstatSync(filePath);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error(`resource must be a regular file: ${filePath}`);
  }
  return stat;
}

function walkFiles(root, relativePrefix = "") {
  const current = relativePrefix ? path.join(root, ...relativePrefix.split("/")) : root;
  if (!fs.existsSync(current)) return [];
  const stat = fs.lstatSync(current);
  if (stat.isSymbolicLink()) throw new Error(`symbolic links are forbidden: ${current}`);
  if (stat.isFile()) return [relativePrefix];
  if (!stat.isDirectory()) throw new Error(`unsupported resource type: ${current}`);
  const result = [];
  for (const name of fs.readdirSync(current).sort()) {
    const child = relativePrefix ? `${relativePrefix}/${name}` : name;
    result.push(...walkFiles(root, child));
  }
  return result;
}

function filesForIncludes(resourcesRoot, includes) {
  const result = new Set();
  for (const include of includes) {
    const normalized = normalizeResourcePath(include);
    for (const file of walkFiles(resourcesRoot, normalized)) result.add(normalizeResourcePath(file));
  }
  return result;
}

function validateSourceSha(value, label) {
  if (!SHA_PATTERN.test(value || "")) throw new Error(`${label} must be a full lowercase commit SHA`);
  return value;
}

function validateVersion(value) {
  if (!VERSION_PATTERN.test(value || "")) throw new Error("from-version is invalid");
  return value;
}

function makeManifest({ baseResources, nextResources, includes, fromSource, fromVersion, toSource }) {
  const selected = includes.length > 0 ? includes : DEFAULT_INCLUDES;
  if (new Set(selected).size !== selected.length) throw new Error("duplicate include");
  for (const include of selected) {
    const normalized = normalizeResourcePath(include);
    if (!ALLOWED_ROOTS.has(normalized)) {
      throw new Error(`include must name an allowlisted root: ${include}`);
    }
    if (!fs.existsSync(path.join(nextResources, normalized))) {
      throw new Error(`selected next resource is missing: ${include}`);
    }
  }
  const before = filesForIncludes(baseResources, selected);
  const after = filesForIncludes(nextResources, selected);
  const union = [...new Set([...before, ...after])].sort();
  if (union.length === 0) throw new Error("patch has no selected files");
  const files = union.map((relativePath) => {
    const basePath = path.join(baseResources, ...relativePath.split("/"));
    const nextPath = path.join(nextResources, ...relativePath.split("/"));
    const baseExists = fs.existsSync(basePath);
    const nextExists = fs.existsSync(nextPath);
    const fromSha256 = baseExists ? sha256File(basePath) : null;
    if (baseExists) assertRegularFile(basePath);
    if (!nextExists) {
      if (!baseExists) throw new Error(`invalid delete entry: ${relativePath}`);
      return { path: relativePath, operation: "delete", from_sha256: fromSha256 };
    }
    const stat = assertRegularFile(nextPath);
    const sha256 = sha256File(nextPath);
    return {
      path: relativePath,
      operation: "put",
      from_sha256: fromSha256,
      sha256,
      size: stat.size,
    };
  }).filter((entry) => entry.operation === "delete" || entry.from_sha256 !== entry.sha256);
  if (files.length === 0) throw new Error("patch has no changed files");
  const validatedFromSource = validateSourceSha(fromSource, "from-source");
  const validatedToSource = validateSourceSha(toSource, "to-source");
  if (validatedFromSource === validatedToSource) throw new Error("from-source and to-source must differ");
  return {
    schema: SCHEMA,
    from: {
      source_sha: validatedFromSource,
      version: validateVersion(fromVersion),
    },
    to: {
      source_sha: validatedToSource,
    },
    includes: [...selected].sort(),
    files,
  };
}

function writePatch({ manifest, nextResources, outputDirectory, zipPath }) {
  if (fs.existsSync(outputDirectory)) throw new Error(`output already exists: ${outputDirectory}`);
  fs.mkdirSync(path.join(outputDirectory, "payload"), { recursive: true });
  for (const file of manifest.files) {
    if (file.operation !== "put") continue;
    const source = path.join(nextResources, ...file.path.split("/"));
    const target = path.join(outputDirectory, "payload", ...file.path.split("/"));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(source, target, fs.constants.COPYFILE_EXCL);
  }
  const manifestPath = path.join(outputDirectory, "manifest.json");
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, { flag: "wx" });
  fs.writeFileSync(path.join(outputDirectory, "manifest.sha256"), `${manifestDigest(manifest)}  manifest.json\n`, { flag: "wx" });
  if (zipPath) {
    const entries = walkFiles(outputDirectory).map((name) => ({
      name,
      source: path.join(outputDirectory, ...name.split("/")),
    }));
    createStoreZip(entries, zipPath);
  }
}

function parseArgs(argv) {
  const command = argv[0];
  const values = new Map();
  const flags = new Set();
  for (let index = 1; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) throw new Error(`unexpected argument: ${token}`);
    if (["--dry-run", "--keep-backup", "--no-restart"].includes(token)) {
      flags.add(token.slice(2));
      continue;
    }
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) throw new Error(`missing value for ${token}`);
    const key = token.slice(2);
    if (key === "include") {
      const current = values.get(key) || [];
      current.push(value);
      values.set(key, current);
    } else if (values.has(key)) {
      throw new Error(`duplicate argument: ${token}`);
    } else {
      values.set(key, value);
    }
    index += 1;
  }
  return { command, values, flags };
}

function required(values, name) {
  const value = values.get(name);
  if (!value || Array.isArray(value)) throw new Error(`--${name} is required`);
  return path.resolve(value);
}

function requiredText(values, name) {
  const value = values.get(name);
  if (!value || Array.isArray(value)) throw new Error(`--${name} is required`);
  return value;
}

function loadPatch(patchInput) {
  let root = path.resolve(patchInput);
  let cleanup = null;
  if (fs.lstatSync(root).isFile()) {
    if (path.extname(root).toLowerCase() !== ".zip") throw new Error("patch file must be a .zip");
    cleanup = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-hotpatch-"));
    extractStoreZip(root, cleanup);
    root = cleanup;
  }
  const manifestPath = path.join(root, "manifest.json");
  const digestPath = path.join(root, "manifest.sha256");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const expectedDigest = fs.readFileSync(digestPath, "utf8").trim().split(/\s+/)[0];
  if (!/^[0-9a-f]{64}$/.test(expectedDigest) || expectedDigest !== manifestDigest(manifest)) {
    throw new Error("patch manifest digest mismatch");
  }
  validateManifest(manifest, root);
  return { root, manifest, cleanup };
}

function validateManifest(manifest, patchRoot) {
  if (!manifest || manifest.schema !== SCHEMA) throw new Error("unsupported patch manifest schema");
  validateSourceSha(manifest.from?.source_sha, "manifest from source");
  validateVersion(manifest.from?.version);
  validateSourceSha(manifest.to?.source_sha, "manifest to source");
  if (manifest.from.source_sha === manifest.to.source_sha) throw new Error("manifest source transition is empty");
  if (
    !Array.isArray(manifest.includes)
    || manifest.includes.length === 0
    || new Set(manifest.includes).size !== manifest.includes.length
  ) {
    throw new Error("invalid manifest includes");
  }
  const includes = new Set(manifest.includes.map((include) => {
    const normalized = normalizeResourcePath(include);
    if (!ALLOWED_ROOTS.has(normalized)) throw new Error(`invalid manifest include: ${include}`);
    return normalized;
  }));
  if (!Array.isArray(manifest.files) || manifest.files.length === 0) throw new Error("empty patch manifest");
  const seen = new Set();
  for (const file of manifest.files) {
    const relativePath = normalizeResourcePath(file.path);
    if (!includes.has(relativePath.split("/")[0])) {
      throw new Error(`manifest path is not declared by includes: ${relativePath}`);
    }
    if (seen.has(relativePath)) throw new Error(`duplicate manifest path: ${relativePath}`);
    seen.add(relativePath);
    if (!["put", "delete"].includes(file.operation)) throw new Error(`invalid operation: ${relativePath}`);
    if (file.from_sha256 !== null && !/^[0-9a-f]{64}$/.test(file.from_sha256 || "")) {
      throw new Error(`invalid from hash: ${relativePath}`);
    }
    if (file.operation === "put") {
      if (!/^[0-9a-f]{64}$/.test(file.sha256 || "") || !Number.isSafeInteger(file.size) || file.size < 0) {
        throw new Error(`invalid payload metadata: ${relativePath}`);
      }
      const payload = path.join(patchRoot, "payload", ...relativePath.split("/"));
      const stat = assertRegularFile(payload);
      if (stat.size !== file.size || sha256File(payload) !== file.sha256) {
        throw new Error(`payload hash mismatch: ${relativePath}`);
      }
    }
  }
}

function resolveResources(values, platform) {
  if (values.has("resources")) return required(values, "resources");
  const app = required(values, "app");
  return platform === "darwin"
    ? path.join(app, "Contents", "Resources")
    : path.join(app, "resources");
}

function verifyInstalledBase(resources, manifest) {
  for (const file of manifest.files) {
    const target = path.join(resources, ...file.path.split("/"));
    const exists = fs.existsSync(target);
    if (file.from_sha256 === null) {
      if (exists) throw new Error(`base expected file to be absent: ${file.path}`);
      continue;
    }
    if (!exists || sha256File(target) !== file.from_sha256) {
      throw new Error(`installed base hash mismatch: ${file.path}`);
    }
  }
}

function runChecked(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: "inherit", ...options });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${command} exited with ${result.status}`);
}

function stopApplication(platform, values, skip) {
  if (skip) return;
  if (platform === "darwin") {
    spawnSync("/usr/bin/pkill", ["-x", "EchoDesk"], { stdio: "ignore" });
    return;
  }
  const result = spawnSync("taskkill.exe", ["/IM", "EchoDesk.exe", "/T", "/F"], { stdio: "ignore" });
  if (result.error) throw result.error;
  // taskkill uses 128 when no matching process exists; that already satisfies
  // the precondition that no EchoDesk process is holding resources open.
  if (result.status !== 0 && result.status !== 128) {
    throw new Error(`taskkill.exe exited with ${result.status}`);
  }
}

function restartApplication(platform, values) {
  if (platform === "darwin") {
    runChecked("/usr/bin/open", ["-a", required(values, "app")]);
    return;
  }
  const executable = values.has("executable")
    ? required(values, "executable")
    : path.join(required(values, "app"), "EchoDesk.exe");
  const child = spawn(executable, [], { detached: true, stdio: "ignore" });
  child.unref();
}

function swapResources({
  resources,
  patchRoot,
  manifest,
  platform,
  appPath,
  keepBackup,
  commandRunner = runChecked,
}) {
  const parent = path.dirname(resources);
  const token = manifest.to.source_sha.slice(0, 12);
  const stage = path.join(parent, `.echodesk-resources-stage-${token}-${process.pid}`);
  const backup = path.join(parent, `.echodesk-resources-backup-${token}-${process.pid}`);
  if (fs.existsSync(stage) || fs.existsSync(backup)) throw new Error("transaction paths already exist");
  fs.cpSync(resources, stage, {
    recursive: true,
    dereference: false,
    errorOnExist: true,
    force: false,
  });
  let originalMoved = false;
  let stageActivated = false;
  try {
    for (const file of manifest.files) {
      const target = path.join(stage, ...file.path.split("/"));
      if (file.operation === "delete") {
        fs.rmSync(target, { force: true });
        continue;
      }
      const payload = path.join(patchRoot, "payload", ...file.path.split("/"));
      fs.mkdirSync(path.dirname(target), { recursive: true });
      const tempTarget = `${target}.hotpatch-${process.pid}`;
      fs.copyFileSync(payload, tempTarget, fs.constants.COPYFILE_EXCL);
      fs.renameSync(tempTarget, target);
    }
    for (const file of manifest.files) {
      const target = path.join(stage, ...file.path.split("/"));
      if (file.operation === "delete") {
        if (fs.existsSync(target)) throw new Error(`staged delete failed: ${file.path}`);
      } else if (sha256File(target) !== file.sha256) {
        throw new Error(`staged payload hash mismatch: ${file.path}`);
      }
    }
    fs.renameSync(resources, backup);
    originalMoved = true;
    fs.renameSync(stage, resources);
    stageActivated = true;
    try {
      if (platform === "darwin") {
        commandRunner("/usr/bin/codesign", ["--force", "--deep", "--sign", "-", appPath]);
        commandRunner("/usr/bin/codesign", ["--verify", "--deep", "--strict", "--verbose=2", appPath]);
      }
    } catch (error) {
      fs.renameSync(resources, stage);
      stageActivated = false;
      fs.renameSync(backup, resources);
      originalMoved = false;
      if (platform === "darwin") {
        commandRunner("/usr/bin/codesign", ["--force", "--deep", "--sign", "-", appPath]);
        commandRunner("/usr/bin/codesign", ["--verify", "--deep", "--strict", "--verbose=2", appPath]);
      }
      throw error;
    }
    // The new tree is committed after signing/verification. Backup cleanup is
    // deliberately outside the rollback region: a cleanup error must never
    // restore a partially deleted backup over a valid patched installation.
    stageActivated = false;
    originalMoved = false;
    if (!keepBackup) {
      try {
        fs.rmSync(backup, { recursive: true, force: true });
      } catch (error) {
        console.warn(`[desktop-resource-hotpatch] patched successfully; backup cleanup failed: ${error.message}`);
        return { backup };
      }
    }
    return { backup: keepBackup ? backup : null };
  } catch (error) {
    if (stageActivated && fs.existsSync(resources)) {
      fs.renameSync(resources, stage);
      stageActivated = false;
    }
    if (originalMoved && fs.existsSync(backup) && !fs.existsSync(resources)) {
      fs.renameSync(backup, resources);
      originalMoved = false;
    }
    fs.rmSync(stage, { recursive: true, force: true });
    throw error;
  }
}

function createCommand(values) {
  const baseResources = required(values, "base-resources");
  const nextResources = required(values, "next-resources");
  const outputDirectory = required(values, "output");
  const zipPath = values.has("zip") ? required(values, "zip") : null;
  const manifest = makeManifest({
    baseResources,
    nextResources,
    includes: values.get("include") || [],
    fromSource: requiredText(values, "from-source"),
    fromVersion: requiredText(values, "from-version"),
    toSource: requiredText(values, "to-source"),
  });
  writePatch({ manifest, nextResources, outputDirectory, zipPath });
  console.log(JSON.stringify({
    status: "created",
    output: outputDirectory,
    zip: zipPath,
    manifest_sha256: manifestDigest(manifest),
    changed_files: manifest.files.length,
  }));
}

function applyCommand(values, flags) {
  const platform = values.get("platform") || process.platform;
  if (!["darwin", "win32"].includes(platform)) throw new Error("apply supports only darwin or win32");
  const resources = resolveResources(values, platform);
  const patch = loadPatch(required(values, "patch"));
  try {
    verifyInstalledBase(resources, patch.manifest);
    if (flags.has("dry-run")) {
      console.log(JSON.stringify({
        status: "dry-run-ok",
        resources,
        from: patch.manifest.from,
        to: patch.manifest.to,
        changed_files: patch.manifest.files.length,
      }));
      return;
    }
    if (!values.has("app")) throw new Error("--app is required unless --dry-run is used");
    stopApplication(platform, values, false);
    const transaction = swapResources({
      resources,
      patchRoot: patch.root,
      manifest: patch.manifest,
      platform,
      appPath: required(values, "app"),
      keepBackup: flags.has("keep-backup"),
    });
    if (!flags.has("no-restart")) restartApplication(platform, values);
    console.log(JSON.stringify({
      status: "applied",
      resources,
      to: patch.manifest.to,
      backup: transaction.backup,
    }));
  } finally {
    if (patch.cleanup) fs.rmSync(patch.cleanup, { recursive: true, force: true });
  }
}

function usage() {
  return `EchoDesk desktop resource hot-patch

Create:
  node scripts/desktop-resource-hotpatch.cjs create \\
    --base-resources <old Resources> --next-resources <new Resources> \\
    --from-source <40-char SHA> --from-version <version> --to-source <40-char SHA> \\
    --output <patch directory> [--zip <patch.zip>] \\
    [--include app.asar] [--include agent-runtime] [--include backend]

Apply:
  node scripts/desktop-resource-hotpatch.cjs apply --patch <directory|zip> \\
    --app <EchoDesk.app|install directory> [--executable <EchoDesk.exe>] \\
    [--platform darwin|win32] [--dry-run] [--keep-backup] [--no-restart]

The apply command only permits app.asar, agent-runtime/** and backend/**.
It never replaces the Electron executable, helpers, DLLs or the installer.`;
}

function main(argv = process.argv.slice(2)) {
  const { command, values, flags } = parseArgs(argv);
  if (command === "create") return createCommand(values);
  if (command === "apply") return applyCommand(values, flags);
  console.error(usage());
  throw new Error("command must be create or apply");
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`[desktop-resource-hotpatch] ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = {
  ALLOWED_ROOTS,
  SCHEMA,
  makeManifest,
  manifestDigest,
  normalizeResourcePath,
  sha256File,
  stableJson,
  swapResources,
  validateManifest,
  verifyInstalledBase,
  writePatch,
};
