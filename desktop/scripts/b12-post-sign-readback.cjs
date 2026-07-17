/* eslint-disable no-console */
const {
  createHash,
} = require("node:crypto");
const {
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
} = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const SCHEMA_VERSION = 1;
const RUNNER_ID = "echodesk.b12.post-sign-readback";
const MAX_MANIFEST_BYTES = 16 * 1024 * 1024;
const HASH_PATTERN = /^[0-9a-f]{64}$/i;

function normalizeHash(value, field) {
  const hash = String(value || "").trim().replace(/^sha256:/i, "").toLowerCase();
  if (!HASH_PATTERN.test(hash)) {
    throw new Error(`[b12-readback] ${field} must be a SHA-256 digest`);
  }
  return hash;
}

function normalizeReleaseSha(value) {
  const sha = String(value || "").trim().toLowerCase();
  if (!/^[0-9a-f]{40}$/.test(sha)) {
    throw new Error("[b12-readback] expected release SHA must be a full SHA-1");
  }
  return sha;
}

function sha256File(filePath) {
  const hash = createHash("sha256");
  hash.update(readFileSync(filePath));
  return hash.digest("hex");
}

function sha256Text(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function isPeCoffBuffer(buffer) {
  if (!Buffer.isBuffer(buffer) || buffer.length < 64) return false;
  if (buffer[0] !== 0x4d || buffer[1] !== 0x5a) return false;
  const peOffset = buffer.readUInt32LE(0x3c);
  return peOffset >= 64
    && peOffset + 4 <= buffer.length
    && buffer.subarray(peOffset, peOffset + 4).equals(Buffer.from([0x50, 0x45, 0x00, 0x00]));
}

function isPeCoffFile(filePath) {
  try {
    return isPeCoffBuffer(readFileSync(filePath));
  } catch {
    return false;
  }
}

function walkRegularFiles(root, result = []) {
  let entries;
  try {
    entries = readdirSync(root, { withFileTypes: true });
  } catch (error) {
    throw new Error(`[b12-readback] cannot enumerate ${root}: ${error instanceof Error ? error.message : String(error)}`);
  }
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    if (entry.isSymbolicLink()) continue;
    const candidate = path.join(root, entry.name);
    if (entry.isFile()) result.push(candidate);
    else if (entry.isDirectory()) walkRegularFiles(candidate, result);
  }
  return result;
}

function normalizePosixRelative(root, filePath) {
  return path.relative(root, filePath).split(path.sep).join(path.posix.sep);
}

function enumeratePeCoffFiles(root) {
  const resolvedRoot = path.resolve(root);
  if (!existsSync(resolvedRoot) || !statSync(resolvedRoot).isDirectory()) {
    throw new Error(`[b12-readback] PE/COFF root does not exist: ${resolvedRoot}`);
  }
  return walkRegularFiles(resolvedRoot)
    .filter((filePath) => isPeCoffFile(filePath))
    .map((filePath) => {
      const bytes = readFileSync(filePath);
      return {
        relative_path: normalizePosixRelative(resolvedRoot, filePath),
        absolute_path: filePath,
        size_bytes: bytes.length,
        sha256: sha256File(filePath),
      };
    })
    .sort((left, right) => left.relative_path.localeCompare(right.relative_path));
}

function normalizePeArtifactPath(value, field) {
  const resolved = path.resolve(String(value || ""));
  if (!existsSync(resolved) || !statSync(resolved).isFile()) {
    throw new Error(`[b12-readback] ${field} does not exist: ${resolved}`);
  }
  if (!isPeCoffFile(resolved)) {
    throw new Error(`[b12-readback] ${field} is not a PE/COFF file: ${resolved}`);
  }
  return resolved;
}

function readbackWindowsPeScope({
  innerRoot,
  outerArtifacts = [],
  expectedReleaseSha,
} = {}) {
  const failures = [];
  let normalizedReleaseSha = null;
  const inner = enumeratePeCoffFiles(innerRoot).map((record) => ({
    ...record,
    scope: "inner",
    authenticode_status: "delegated_to_verify-windows-authenticode.ps1",
    thumbprint: "protected-config:windows-certificate-thumbprint",
    publisher: "protected-config:windows-publisher",
    digest_algorithm: "sha256",
    timestamp_status: "delegated_to_rfc3161_verifier",
  }));
  if (inner.length === 0) failures.push({ code: "EMPTY_INNER_PE_SCOPE", root: path.resolve(innerRoot) });

  const outer = [...new Set(outerArtifacts.map((artifact) => normalizePeArtifactPath(artifact, "outer artifact")))]
    .map((artifactPath) => ({
      scope: "outer",
      relative_path: path.basename(artifactPath),
      absolute_path: artifactPath,
      size_bytes: statSync(artifactPath).size,
      sha256: sha256File(artifactPath),
      authenticode_status: "delegated_to_verify-windows-authenticode.ps1",
      thumbprint: "protected-config:windows-certificate-thumbprint",
      publisher: "protected-config:windows-publisher",
      digest_algorithm: "sha256",
      timestamp_status: "delegated_to_rfc3161_verifier",
    }));
  if (outerArtifacts.length === 0) failures.push({ code: "EMPTY_OUTER_PE_SCOPE" });
  if (outer.length !== outerArtifacts.length) {
    failures.push({ code: "DUPLICATE_OUTER_PE_SCOPE", expected: outerArtifacts.length, actual: outer.length });
  }
  if (expectedReleaseSha !== undefined) {
    try {
      normalizedReleaseSha = normalizeReleaseSha(expectedReleaseSha);
    } catch (error) {
      failures.push({ code: "INVALID_RELEASE_SHA", message: error instanceof Error ? error.message : String(error) });
    }
  }

  return {
    schema_version: SCHEMA_VERSION,
    runner: RUNNER_ID,
    status: failures.length === 0 ? "PASS" : "FAIL",
    verdict: failures.length === 0 ? "windows_pe_scope_readback_pass" : "release_blocked_signing",
    release_sha: normalizedReleaseSha,
    enumeration: {
      detector: "DOS MZ header plus PE\\0\\0 signature at e_lfanew",
      recursive: true,
      extension_is_non_authoritative: true,
    },
    inner_root: path.resolve(innerRoot),
    inner_pe_files: inner,
    outer_pe_files: outer,
    all_pe_files: [...inner, ...outer],
    failures,
  };
}

function comparePeCoffTrees(sourceRoot, candidateRoot) {
  const source = enumeratePeCoffFiles(sourceRoot);
  const candidate = enumeratePeCoffFiles(candidateRoot);
  const sourceByPath = new Map(source.map((record) => [record.relative_path, record]));
  const candidateByPath = new Map(candidate.map((record) => [record.relative_path, record]));
  const failures = [];
  for (const [relativePath, expected] of sourceByPath) {
    const actual = candidateByPath.get(relativePath);
    if (!actual) {
      failures.push({ code: "MISSING_PORTABLE_PE", path: relativePath });
      continue;
    }
    if (actual.size_bytes !== expected.size_bytes || actual.sha256 !== expected.sha256) {
      failures.push({
        code: "PORTABLE_PE_BYTES_MISMATCH",
        path: relativePath,
        expected_size_bytes: expected.size_bytes,
        actual_size_bytes: actual.size_bytes,
        expected_sha256: expected.sha256,
        actual_sha256: actual.sha256,
      });
    }
  }
  for (const relativePath of candidateByPath.keys()) {
    if (!sourceByPath.has(relativePath)) failures.push({ code: "EXTRA_PORTABLE_PE", path: relativePath });
  }
  return {
    status: failures.length === 0 ? "PASS" : "FAIL",
    verdict: failures.length === 0 ? "portable_verified_inner_bytes" : "release_blocked_signing",
    source_root: path.resolve(sourceRoot),
    candidate_root: path.resolve(candidateRoot),
    source_count: source.length,
    candidate_count: candidate.length,
    failures,
  };
}

function canonicalJson(value) {
  if (Array.isArray(value)) return value.map((item) => canonicalJson(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalJson(value[key])]),
    );
  }
  return value;
}

function computeCanonicalManifestDigest(manifest) {
  const withoutDigestValue = JSON.parse(JSON.stringify(manifest));
  if (withoutDigestValue.manifest_digest && typeof withoutDigestValue.manifest_digest === "object") {
    delete withoutDigestValue.manifest_digest.value;
  }
  return sha256Text(JSON.stringify(canonicalJson(withoutDigestValue)));
}

function canonicalRelativePath(value, field = "path") {
  const raw = String(value || "");
  if (!raw || raw.includes("\0") || raw.includes("\\") || path.posix.isAbsolute(raw)) {
    throw new Error(`[b12-readback] ${field} must be a relative POSIX path`);
  }
  const normalized = path.posix.normalize(raw);
  if (normalized === "." || normalized === ".." || normalized.startsWith("../")) {
    throw new Error(`[b12-readback] ${field} escapes the package root`);
  }
  return normalized;
}

function manifestFiles(manifest) {
  const files = manifest.files || manifest.content_entries || manifest.entries || manifest.content;
  if (!Array.isArray(files) || files.length === 0) {
    throw new Error("[b12-readback] fusion manifest must contain a non-empty files array");
  }
  const seen = new Set();
  return files.map((entry, index) => {
    if (!entry || typeof entry !== "object") {
      throw new Error(`[b12-readback] manifest file ${index} must be an object`);
    }
    const relativePath = canonicalRelativePath(
      entry.path || entry.package_relative_path || entry.packageRelativePath,
      `manifest.files[${index}].path`,
    );
    if (seen.has(relativePath)) {
      throw new Error(`[b12-readback] manifest contains duplicate path ${relativePath}`);
    }
    seen.add(relativePath);
    const rawSize = entry.size === undefined ? entry.size_bytes : entry.size;
    const rawSha256 = entry.sha256 === undefined ? entry.sha_256 : entry.sha256;
    const pending = rawSize === null || rawSha256 === null;
    if (pending && (rawSize !== null || rawSha256 !== null)) {
      throw new Error(`[b12-readback] manifest.files[${index}] must set both size and sha256 when pending`);
    }
    if (!pending && (!Number.isSafeInteger(rawSize) || rawSize < 0)) {
      throw new Error(`[b12-readback] manifest.files[${index}].size/size_bytes must be a non-negative integer`);
    }
    const sha256 = pending
      ? null
      : normalizeHash(rawSha256, `manifest.files[${index}].sha256`);
    if (typeof entry.executable !== "boolean") {
      throw new Error(`[b12-readback] manifest.files[${index}].executable must be boolean`);
    }
    if (!String(entry.role || "").trim()) {
      throw new Error(`[b12-readback] manifest.files[${index}].role is required`);
    }
    if (!String(entry.platform || "").trim() || !String(entry.arch || "").trim()) {
      throw new Error(`[b12-readback] manifest.files[${index}] requires platform and arch`);
    }
    if (!String(entry.placement || entry.load_mode || "").trim()) {
      throw new Error(`[b12-readback] manifest.files[${index}].placement is required`);
    }
    return {
      path: relativePath,
      size: rawSize,
      sha256,
      executable: entry.executable,
      role: String(entry.role).trim(),
      platform: String(entry.platform).trim(),
      arch: String(entry.arch).trim(),
      placement: String(entry.placement || entry.load_mode).trim(),
      pending,
      status: String(entry.status || "").trim() || null,
      hashScope: String(entry.hash_scope || "").trim() || null,
    };
  });
}

function computeLogicalContentDigest(files) {
  const canonical = files
    .filter((entry) => !entry.pending && entry.sha256)
    .map((entry) => ({
      arch: entry.arch,
      executable: entry.executable,
      path: entry.path,
      placement: entry.placement,
      platform: entry.platform,
      role: entry.role,
      sha256: normalizeHash(entry.sha256, "logical content entry sha256"),
      size: entry.size,
    }))
    .sort((left, right) => left.path.localeCompare(right.path));
  return sha256Text(`${JSON.stringify(canonical)}\n`);
}

function validateManifest(manifest) {
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
    throw new Error("[b12-readback] fusion manifest must be a JSON object");
  }
  if (manifest.schema_version !== SCHEMA_VERSION) {
    throw new Error(`[b12-readback] unsupported fusion manifest schema ${String(manifest.schema_version)}`);
  }
  const releaseSha = String(manifest.release_sha || manifest.releaseSha || "").trim();
  if (!/^[0-9a-f]{40}$/i.test(releaseSha)) {
    throw new Error("[b12-readback] fusion manifest release_sha must be a full SHA-1");
  }
  const files = manifestFiles(manifest);
  const computedLogicalDigest = computeLogicalContentDigest(files);
  const declaredLogicalDigest = manifest.logical_content_digest || manifest.content_digest;
  if (declaredLogicalDigest !== undefined) {
    const normalizedDeclared = normalizeHash(declaredLogicalDigest, "logical_content_digest");
    if (normalizedDeclared !== computedLogicalDigest) {
      throw new Error("[b12-readback] fusion manifest logical content digest is internally inconsistent");
    }
  }
  return {
    releaseSha: releaseSha.toLowerCase(),
    files,
    computedLogicalDigest,
    declaredLogicalDigest: declaredLogicalDigest === undefined
      ? null
      : normalizeHash(declaredLogicalDigest, "logical_content_digest"),
  };
}

const REQUIRED_PACKAGED_RUNTIME_RESOURCES = [
  { role: "electron_worker_entry", path: "Resources/agent-runtime/worker.mjs" },
  { role: "worker_bridge", path: "Resources/agent-runtime/worker/bridge.mjs" },
  { role: "electron_worker_factory", path: "Resources/agent-runtime/worker/bridge/production-factory.mjs" },
  { role: "electron_host_deps", path: "Resources/agent-runtime/worker/bridge/b13-host-kernel-deps.mjs" },
];

function validatePackagedRuntimeResources(files, roots) {
  const failures = [];
  for (const required of REQUIRED_PACKAGED_RUNTIME_RESOURCES) {
    const entry = files.find((candidate) => candidate.path === required.path)
      || files.find((candidate) => candidate.role === required.role);
    if (!entry) {
      failures.push({ code: "MISSING_PACKAGED_RUNTIME_RESOURCE", role: required.role, path: required.path });
      continue;
    }
    if (entry.pending) {
      failures.push({ code: "PENDING_PACKAGED_RUNTIME_RESOURCE", role: required.role, path: entry.path });
      continue;
    }
    const located = locateEntry(roots, entry.path);
    if (located.ambiguous) failures.push({ code: "AMBIGUOUS_PACKAGED_RUNTIME_RESOURCE", role: required.role, path: entry.path, matches: located.matches });
    else if (!located.path) failures.push({ code: "MISSING_PACKAGED_RUNTIME_READBACK", role: required.role, path: entry.path });
  }
  return failures;
}

function walkForFilename(root, filename, result = []) {
  if (result.length > 1) return result;
  let entries;
  try {
    entries = readdirSync(root, { withFileTypes: true });
  } catch {
    return result;
  }
  for (const entry of entries) {
    if (entry.isSymbolicLink()) continue;
    const candidate = path.join(root, entry.name);
    if (entry.isFile() && entry.name === filename) result.push(candidate);
    else if (entry.isDirectory()) walkForFilename(candidate, filename, result);
    if (result.length > 1) return result;
  }
  return result;
}

function discoverManifest(root, explicitManifestPath) {
  if (explicitManifestPath) {
    const resolved = path.resolve(explicitManifestPath);
    if (!existsSync(resolved) || !statSync(resolved).isFile()) {
      throw new Error(`[b12-readback] manifest does not exist: ${resolved}`);
    }
    return resolved;
  }
  const exactCandidates = [
    "fusion-content-manifest.json",
    path.join("resources", "fusion-content-manifest.json"),
    path.join("resources", "agent-runtime", "fusion-content-manifest.json"),
    path.join("Resources", "fusion-content-manifest.json"),
    path.join("Contents", "Resources", "fusion-content-manifest.json"),
    path.join("Contents", "Resources", "agent-runtime", "fusion-content-manifest.json"),
  ].map((relativePath) => path.join(root, relativePath));
  const exact = exactCandidates.filter((candidate) => existsSync(candidate) && statSync(candidate).isFile());
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) throw new Error("[b12-readback] multiple fusion manifests found; pass --manifest explicitly");
  const discovered = walkForFilename(root, "fusion-content-manifest.json");
  if (discovered.length !== 1) {
    throw new Error(`[b12-readback] expected exactly one fusion-content-manifest.json, found ${discovered.length}`);
  }
  return discovered[0];
}

function candidateEntryPaths(root, relativePath) {
  const candidates = [
    relativePath,
    path.join("Contents", relativePath),
    path.join("Resources", relativePath),
    path.join("Contents", "Resources", relativePath),
    path.join("resources", relativePath),
    path.join("resources", "app.asar.unpacked", relativePath),
  ];
  return [...new Set(candidates.map((candidate) => path.resolve(root, candidate)))];
}

function locateEntry(roots, relativePath) {
  const packageRoots = Array.isArray(roots) ? roots : [roots];
  const matches = packageRoots
    .flatMap((root) => candidateEntryPaths(root, relativePath))
    .filter((candidate, index, candidates) => candidates.indexOf(candidate) === index)
    .filter((candidate) => {
      if (!existsSync(candidate)) return false;
      const info = lstatSync(candidate);
      return info.isFile();
    });
  if (matches.length === 0) return { path: null, ambiguous: false };
  if (matches.length > 1) return { path: null, ambiguous: true, matches };
  return { path: matches[0], ambiguous: false };
}

function command(commandName, args, cwd) {
  const result = spawnSync(commandName, args, {
    cwd,
    encoding: "utf8",
    stdio: "pipe",
    windowsHide: true,
  });
  if (result.error || result.status !== 0) {
    const detail = [result.error?.message, result.stdout, result.stderr].filter(Boolean).join(" ").trim();
    throw new Error(`[b12-readback] ${commandName} failed${detail ? `: ${detail}` : ""}`);
  }
}

function extractZipArtifact(artifact, destination) {
  if (process.platform === "darwin") {
    command("ditto", ["-x", "-k", artifact, destination]);
    return;
  }
  command("unzip", ["-q", "-o", artifact, "-d", destination]);
}

function prepareLayout({ artifactPath, layoutRoot }) {
  if (layoutRoot) {
    const root = path.resolve(layoutRoot);
    if (!existsSync(root) || !statSync(root).isDirectory()) {
      throw new Error(`[b12-readback] layout root does not exist: ${root}`);
    }
    return { root, artifactKind: "layout", cleanup() {} };
  }
  if (!artifactPath) throw new Error("[b12-readback] provide --artifact or --layout-root");
  const artifact = path.resolve(artifactPath);
  if (!existsSync(artifact)) throw new Error(`[b12-readback] artifact does not exist: ${artifact}`);
  if (statSync(artifact).isDirectory()) {
    return { root: artifact, artifactKind: artifact.toLowerCase().endsWith(".app") ? "app" : "installed", cleanup() {} };
  }
  const extension = path.extname(artifact).toLowerCase();
  if (extension === ".exe") {
    throw new Error("[b12-readback] NSIS/installer readback requires --layout-root; B12 never executes an installer");
  }
  const temporaryRoot = mkdtempSync(path.join(os.tmpdir(), "echodesk-b12-readback-"));
  if (extension === ".zip") {
    try {
      extractZipArtifact(artifact, temporaryRoot);
    } catch (error) {
      rmSync(temporaryRoot, { recursive: true, force: true });
      throw error;
    }
    return { root: temporaryRoot, artifactKind: "zip", cleanup() { rmSync(temporaryRoot, { recursive: true, force: true }); } };
  }
  if (extension === ".dmg") {
    if (process.platform !== "darwin") {
      rmSync(temporaryRoot, { recursive: true, force: true });
      throw new Error("[b12-readback] DMG readback requires macOS or an already-mounted --layout-root");
    }
    try {
      mkdirSync(temporaryRoot, { recursive: true });
      command("hdiutil", ["attach", artifact, "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", temporaryRoot]);
    } catch (error) {
      rmSync(temporaryRoot, { recursive: true, force: true });
      throw error;
    }
    return {
      root: temporaryRoot,
      artifactKind: "dmg",
      cleanup() {
        try { command("hdiutil", ["detach", temporaryRoot, "-force"]); } finally { rmSync(temporaryRoot, { recursive: true, force: true }); }
      },
    };
  }
  rmSync(temporaryRoot, { recursive: true, force: true });
  throw new Error(`[b12-readback] unsupported artifact type ${extension || "(none)"}; provide --layout-root`);
}

function packageRootsForManifest(root, manifestPath) {
  const roots = [root];
  let current = path.dirname(manifestPath);
  const resolvedRoot = path.resolve(root);
  while (current !== resolvedRoot && current.startsWith(`${resolvedRoot}${path.sep}`)) {
    roots.push(current);
    current = path.dirname(current);
  }
  return roots;
}

function readbackLayout({ root, manifestPath, expectedReleaseSha, requireFallbackScan = true, requirePackagedRuntime = false }) {
  const resolvedManifestPath = discoverManifest(root, manifestPath);
  if (statSync(resolvedManifestPath).size > MAX_MANIFEST_BYTES) {
    throw new Error("[b12-readback] fusion manifest exceeds the size limit");
  }
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(resolvedManifestPath, "utf8"));
  } catch (error) {
    throw new Error(`[b12-readback] invalid fusion manifest JSON: ${error instanceof Error ? error.message : String(error)}`);
  }
  const normalized = validateManifest(manifest);
  const failures = [];
  if (expectedReleaseSha !== undefined && normalized.releaseSha !== normalizeReleaseSha(expectedReleaseSha)) {
    failures.push({ code: "RELEASE_SHA_MISMATCH", expected: normalizeReleaseSha(expectedReleaseSha), actual: normalized.releaseSha });
  }
  const checkedFiles = [];
  const pendingFiles = [];
  const packageRoots = packageRootsForManifest(root, resolvedManifestPath);
  if (requirePackagedRuntime) {
    failures.push(...validatePackagedRuntimeResources(normalized.files, packageRoots));
  }
  for (const expected of normalized.files) {
    if (expected.pending) {
      pendingFiles.push({
        path: expected.path,
        role: expected.role,
        status: expected.status,
        hash_scope: expected.hashScope,
        reason: "unsigned package bytes are not available in this manifest yet",
      });
      continue;
    }
    const located = locateEntry(packageRoots, expected.path);
    if (located.ambiguous) {
      failures.push({ code: "AMBIGUOUS_RESOURCE_PATH", path: expected.path, matches: located.matches });
      continue;
    }
    if (!located.path) {
      failures.push({ code: "MISSING_RESOURCE", path: expected.path });
      continue;
    }
    const actualSize = statSync(located.path).size;
    const actualHash = sha256File(located.path);
    const record = {
      path: expected.path,
      resolved_path: path.relative(root, located.path) || ".",
      expected_size: expected.size,
      actual_size: actualSize,
      expected_sha256: `sha256:${expected.sha256}`,
      actual_sha256: `sha256:${actualHash}`,
      role: expected.role,
      placement: expected.placement,
      executable: expected.executable,
      status: "pass",
    };
    if (actualSize !== expected.size) {
      record.status = "fail";
      failures.push({ code: "RESOURCE_SIZE_MISMATCH", path: expected.path, expected: expected.size, actual: actualSize });
    }
    if (actualHash !== expected.sha256) {
      record.status = "fail";
      failures.push({ code: "RESOURCE_HASH_MISMATCH", path: expected.path, expected: expected.sha256, actual: actualHash });
    }
    checkedFiles.push(record);
  }

  const fallbackScan = manifest.forbidden_fallback_scan || manifest.forbiddenFallbackScan;
  if (requireFallbackScan && (!fallbackScan || fallbackScan.status !== "pass" || (fallbackScan.findings || []).length > 0)) {
    failures.push({
      code: "FORBIDDEN_FALLBACK_SCAN_NOT_PASS",
      status: fallbackScan?.status || "missing",
      findings: fallbackScan?.findings || [],
    });
  }

  const actualManifestDigest = sha256File(resolvedManifestPath);
  const declaredArtifactDigest = manifest.manifest_sha256 || (
    typeof manifest.manifest_digest === "string"
      ? manifest.manifest_digest
      : manifest.manifest_digest?.value
  );
  const manifestDigestPending = declaredArtifactDigest === "PENDING_CANONICAL_DIGEST";
  const expectedManifestDigest = manifest.manifest_sha256
    ? actualManifestDigest
    : computeCanonicalManifestDigest(manifest);
  if (declaredArtifactDigest !== undefined && !manifestDigestPending && normalizeHash(declaredArtifactDigest, "manifest_digest") !== expectedManifestDigest) {
    failures.push({ code: "MANIFEST_HASH_MISMATCH", expected: declaredArtifactDigest, actual: `sha256:${expectedManifestDigest}` });
  }

  return {
    schema_version: SCHEMA_VERSION,
    runner: RUNNER_ID,
    status: failures.length === 0 ? "PASS" : "FAIL",
    verdict: failures.length === 0 ? "post_sign_readback_pass" : "release_blocked_signing",
    release_sha: normalized.releaseSha,
    manifest: {
      path: path.relative(root, resolvedManifestPath) || ".",
      sha256: `sha256:${actualManifestDigest}`,
      canonical_sha256: `sha256:${computeCanonicalManifestDigest(manifest)}`,
      logical_content_digest: `sha256:${normalized.computedLogicalDigest}`,
      declared_logical_content_digest: normalized.declaredLogicalDigest ? `sha256:${normalized.declaredLogicalDigest}` : null,
    },
    checked_files: checkedFiles,
    pending_files: pendingFiles,
    manifest_digest_status: manifestDigestPending ? "pending" : "observed",
    failures,
    signature_validation: {
      executed: false,
      signed_state_asserted: false,
      notarization_asserted: false,
      authenticode_asserted: false,
      note: "B12 checks logical content only; B14/B15 own real signature and installed-state validation",
    },
  };
}

function readback({ artifactPath, layoutRoot, manifestPath, expectedReleaseSha, requireFallbackScan = true, requirePackagedRuntime = false } = {}) {
  let prepared;
  try {
    prepared = prepareLayout({ artifactPath, layoutRoot });
    const result = readbackLayout({ root: prepared.root, manifestPath, expectedReleaseSha, requireFallbackScan, requirePackagedRuntime });
    if (artifactPath && existsSync(artifactPath) && statSync(artifactPath).isFile()) {
      result.artifact = {
        path: path.resolve(artifactPath),
        kind: prepared.artifactKind,
        sha256: `sha256:${sha256File(path.resolve(artifactPath))}`,
      };
    } else {
      result.artifact = { kind: prepared.artifactKind, path: prepared.root };
    }
    return result;
  } catch (error) {
    return {
      schema_version: SCHEMA_VERSION,
      runner: RUNNER_ID,
      status: "FAIL",
      verdict: "release_blocked_signing",
      failures: [{ code: "READBACK_RUNNER_ERROR", message: error instanceof Error ? error.message : String(error) }],
      signature_validation: {
        executed: false,
        signed_state_asserted: false,
        notarization_asserted: false,
        authenticode_asserted: false,
        note: "B12 checks logical content only; B14/B15 own real signature and installed-state validation",
      },
    };
  } finally {
    prepared?.cleanup();
  }
}

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--json") {
      args.json = true;
      continue;
    }
    if (token === "--allow-missing-fallback-scan") {
      args.requireFallbackScan = false;
      continue;
    }
    if (token === "--require-packaged-runtime") {
      args.requirePackagedRuntime = true;
      continue;
    }
    if (!token.startsWith("--")) throw new Error(`[b12-readback] unexpected argument ${token}`);
    const key = token.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`[b12-readback] ${token} requires a value`);
    args[key] = value;
    index += 1;
  }
  return args;
}

function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  if (args.windowsInnerRoot) {
    const result = readbackWindowsPeScope({
      innerRoot: args.windowsInnerRoot,
      outerArtifacts: args.windowsOuterArtifact ? [args.windowsOuterArtifact] : [],
      expectedReleaseSha: args.releaseSha,
    });
    if (args.windowsCompareRoot) {
      result.portable_byte_comparison = comparePeCoffTrees(
        args.windowsInnerRoot,
        args.windowsCompareRoot,
      );
      if (result.portable_byte_comparison.status !== "PASS") {
        result.status = "FAIL";
        result.verdict = "release_blocked_signing";
      }
    }
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    if (result.status !== "PASS") process.exitCode = 2;
    return;
  }
  const result = readback({
    artifactPath: args.artifact,
    layoutRoot: args.layoutRoot,
    manifestPath: args.manifest,
    expectedReleaseSha: args.releaseSha,
    requireFallbackScan: args.requireFallbackScan !== false,
    requirePackagedRuntime: args.requirePackagedRuntime === true,
  });
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  if (result.status !== "PASS") process.exitCode = 2;
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 2;
  }
}

module.exports = {
  RUNNER_ID,
  SCHEMA_VERSION,
  canonicalRelativePath,
  comparePeCoffTrees,
  computeLogicalContentDigest,
  computeCanonicalManifestDigest,
  enumeratePeCoffFiles,
  isPeCoffBuffer,
  isPeCoffFile,
  normalizeReleaseSha,
  readbackWindowsPeScope,
  normalizeHash,
  readback,
  readbackLayout,
  REQUIRED_PACKAGED_RUNTIME_RESOURCES,
  validatePackagedRuntimeResources,
  validateManifest,
};
