/* eslint-disable @typescript-eslint/no-var-requires */
"use strict";

const { createHash } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const {
  copyFileSync,
  mkdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} = require("node:fs");
const path = require("node:path");
const { buildSync } = require("esbuild");

const desktopRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(desktopRoot, "..");
const runtimeRoot = path.join(desktopRoot, "electron", "agent-runtime");
const outputRoot = path.join(desktopRoot, ".agent-runtime-package");
const sourceManifestPath = path.join(
  repoRoot,
  "docs",
  "0.3.3-bundled-agent-runtime",
  "evidence",
  "B12",
  "fusion-content-manifest.json",
);

const entries = [
  {
    source: "worker/worker-entry.ts",
    output: "worker.mjs",
    role: "electron_worker_entry",
    executable: true,
  },
  {
    source: "worker/bridge.ts",
    output: "worker/bridge.mjs",
    role: "worker_bridge",
    executable: false,
  },
  {
    source: "bridge/production-factory.ts",
    output: "worker/bridge/production-factory.mjs",
    role: "electron_worker_factory",
    executable: false,
  },
  {
    source: "bridge/b13-worker-factory.ts",
    output: "worker/bridge/b13-worker-factory.mjs",
    role: "b13_worker_factory",
    executable: false,
  },
  {
    source: "bridge/b13-host-kernel-deps.ts",
    output: "worker/bridge/b13-host-kernel-deps.mjs",
    role: "electron_host_deps",
    executable: false,
  },
];

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function releaseSha() {
  const configured = String(process.env.ECHODESK_RELEASE_SHA || "").trim();
  const value =
    configured ||
    execFileSync("git", ["rev-parse", "HEAD"], {
      cwd: repoRoot,
      encoding: "utf8",
    }).trim();
  if (!/^[0-9a-f]{40}$/i.test(value)) {
    throw new Error("[agent-runtime-package] release SHA must be a full commit SHA");
  }
  return value.toLowerCase();
}

function logicalContentDigest(files) {
  const canonical = files
    .map((entry) => ({
      arch: entry.arch,
      executable: entry.executable,
      path: entry.path,
      placement: entry.placement,
      platform: entry.platform,
      role: entry.role,
      sha256: entry.sha256,
      size: entry.size,
    }))
    .sort((left, right) => left.path.localeCompare(right.path));
  return sha256(Buffer.from(`${JSON.stringify(canonical)}\n`, "utf8"));
}

function packageEntry(definition) {
  const sourcePath = path.join(runtimeRoot, definition.source);
  const outputPath = path.join(outputRoot, definition.output);
  mkdirSync(path.dirname(outputPath), { recursive: true });
  buildSync({
    entryPoints: [sourcePath],
    outfile: outputPath,
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node24",
    sourcemap: false,
    legalComments: "none",
    charset: "utf8",
    logLevel: "silent",
  });
  const bytes = readFileSync(outputPath);
  return {
    path: `Resources/agent-runtime/${definition.output}`,
    size: bytes.length,
    sha256: sha256(bytes),
    executable: definition.executable,
    role: definition.role,
    platform: "darwin|win32",
    arch: "arm64|x64",
    placement: "extraResources",
    source_path: path.posix.join(
      "desktop/electron/agent-runtime",
      definition.source,
    ),
    source_sha256: sha256(readFileSync(sourcePath)),
    hash_scope: "compiled_package_bytes",
    status: "package_bound",
  };
}

function prepareAgentRuntimePackage() {
  rmSync(outputRoot, { recursive: true, force: true });
  mkdirSync(outputRoot, { recursive: true });
  const files = entries.map(packageEntry);

  const contractSource = path.join(runtimeRoot, "test", "contract", "worker-contract.json");
  const contractOutput = path.join(outputRoot, "contracts", "worker-contract.json");
  mkdirSync(path.dirname(contractOutput), { recursive: true });
  copyFileSync(contractSource, contractOutput);
  const contractBytes = readFileSync(contractOutput);
  files.push({
    path: "Resources/agent-runtime/contracts/worker-contract.json",
    size: contractBytes.length,
    sha256: sha256(contractBytes),
    executable: false,
    role: "frozen_worker_contract",
    platform: "darwin|win32",
    arch: "arm64|x64",
    placement: "extraResources",
    source_path:
      "desktop/electron/agent-runtime/test/contract/worker-contract.json",
    source_sha256: sha256(readFileSync(contractSource)),
    hash_scope: "copied_package_bytes",
    status: "package_bound",
  });

  const sourceManifest = JSON.parse(readFileSync(sourceManifestPath, "utf8"));
  const sha = releaseSha();
  const manifest = {
    schema_version: 1,
    manifest_type: "echo.b12.fusion-content",
    manifest_id: `echo-preview-runtime-${sha.slice(0, 12)}-v1`,
    release_sha: sha,
    kernel_build_identity: sourceManifest.kernel_build_identity,
    logical_content_digest: logicalContentDigest(files),
    official_claude_code_cli_packaged: false,
    runtime_discovery_policy:
      "package resources only; global CLI, HOME and PATH fallback forbidden",
    files,
  };
  writeFileSync(
    path.join(outputRoot, "fusion-content-manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8",
  );
  if (statSync(path.join(outputRoot, "fusion-content-manifest.json")).size < 1) {
    throw new Error("[agent-runtime-package] manifest was not generated");
  }
  console.log(
    `[agent-runtime-package] prepared ${files.length} resources for ${sha}`,
  );
  return outputRoot;
}

module.exports = {
  outputRoot,
  prepareAgentRuntimePackage,
};

if (require.main === module) {
  prepareAgentRuntimePackage();
}
