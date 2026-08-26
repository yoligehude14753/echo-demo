#!/usr/bin/env node
/* eslint-disable no-console */
const fs = require("node:fs");
const path = require("node:path");

const DESKTOP_ROOT = path.resolve(__dirname, "..");
const ECHO_ROOT = path.resolve(DESKTOP_ROOT, "..");

// These are the last legacy renderer sources observed in EchoDesk history. The
// current App keeps one later, scoped capture race repair; main.tsx remains the
// exact legacy blob. No source is resolved from another project.
const LEGACY_SOURCE_RECORD = Object.freeze({
  commits: ["a0fee7e", "51f55d3", "7c573e3"],
  app_blob: "ff88d78b737ac598f51ba85a945ed169ef2ca72b",
  main_blob: "fa6f11380c9b105c938f097d76a7719e56c3c966",
  preload_baseline: "3b553b0181b0d80ae424ad78cf290a686f92ecea",
  fused_wiring_commit: "25e4a4fdde3d58ca399bedc4d2bf6230a898707e",
});

const FORBIDDEN_PATHS = Object.freeze([
  "desktop/.agent-runtime-package",
  "desktop/agent-kernel",
  "desktop/electron/agent-runtime",
  "desktop/electron/backend-runtime-env.cjs",
  "desktop/electron/packaged-fused-worker-bridge.cjs",
  "desktop/scripts/prepare-agent-runtime-package.cjs",
  "desktop/scripts/b12-post-sign-readback.cjs",
  "desktop/scripts/b12-post-sign-readback.test.cjs",
  "desktop/scripts/b12-signing-scope.cjs",
  "docs/0.3.3-bundled-agent-runtime",
  "backend/app/agents/embedded_runtime.py",
  "backend/app/agents/stream_bridge.py",
  "backend/app/runtime/b13_composition.py",
  "backend/app/runtime/b13_host_ipc.py",
  "backend/app/runtime/b13_model_tool_provider.py",
  "backend/tests/integration/test_echo_task_stream_bridge.py",
  "backend/tests/unit/agent_runtime",
  "backend/tests/unit/test_agent_bridge_recovery.py",
  "backend/tests/unit/test_b10_vertical_contract.py",
]);

const SCANNED_FILES = Object.freeze([
  "desktop/package.json",
  "desktop/src/main.tsx",
  "desktop/src/App.tsx",
  "desktop/electron/main.cjs",
  "desktop/electron/preload.cjs",
  "desktop/electron/backend-lifecycle-controller.cjs",
  "desktop/backend-endpoint.cjs",
  "desktop/scripts/verify-bundled-backend.cjs",
  "desktop/scripts/desktop-release-signing.cjs",
  "desktop/scripts/mac-bundle-sign.cjs",
  "desktop/scripts/canonical-mac-release.cjs",
  "backend/app/agents/agentos.py",
  "backend/app/agents/service.py",
  "backend/app/api/agents.py",
]);

const FORBIDDEN_TOKENS = Object.freeze([
  ".agent-runtime-package",
  "agent-runtime",
  "packaged-fused",
  "fused worker",
  "ECHODESK_RUNTIME_FD",
  "ECHODESK_RUNTIME_NONCE",
  "EmbeddedRuntime",
  "b13_",
]);

function read(relativePath) {
  return fs.readFileSync(path.join(ECHO_ROOT, relativePath), "utf8");
}

function containsRegularFile(targetPath) {
  if (!fs.existsSync(targetPath)) return false;
  if (path.basename(targetPath) === "__pycache__" || targetPath.endsWith(".pyc")) return false;
  const entry = fs.lstatSync(targetPath);
  if (entry.isFile()) return true;
  if (!entry.isDirectory()) return false;
  return fs.readdirSync(targetPath).some((name) =>
    containsRegularFile(path.join(targetPath, name)),
  );
}

function scanLegacySourceClosure(root = ECHO_ROOT) {
  const resolvedRoot = path.resolve(root);
  const failures = [];
  const packageJson = JSON.parse(fs.readFileSync(path.join(resolvedRoot, "desktop/package.json"), "utf8"));
  const build = packageJson.build || {};
  const extraResources = Array.isArray(build.extraResources) ? build.extraResources : [];
  const extraResourceText = JSON.stringify(extraResources);

  for (const relativePath of FORBIDDEN_PATHS) {
    if (containsRegularFile(path.join(resolvedRoot, relativePath))) {
      failures.push({ code: "FORBIDDEN_CORE_ASSET", path: relativePath });
    }
  }
  if (extraResourceText.includes("agent-runtime") || extraResourceText.includes(".agent-runtime-package")) {
    failures.push({ code: "FUSED_EXTRA_RESOURCE", field: "build.extraResources" });
  }
  if (packageJson.main !== "electron/main.cjs") {
    failures.push({ code: "NON_CANONICAL_ELECTRON_ENTRY", value: packageJson.main });
  }
  if (packageJson.scripts?.["release:canonical:mac"] !== "node scripts/canonical-mac-release.cjs") {
    failures.push({ code: "CANONICAL_RELEASE_SCRIPT_DRIFT" });
  }

  for (const relativePath of SCANNED_FILES) {
    const filePath = path.join(resolvedRoot, relativePath);
    if (!fs.existsSync(filePath)) {
      failures.push({ code: "MISSING_SOURCE_CLOSURE_FILE", path: relativePath });
      continue;
    }
    const source = fs.readFileSync(filePath, "utf8");
    for (const token of FORBIDDEN_TOKENS) {
      if (source.includes(token)) failures.push({ code: "FORBIDDEN_CORE_TOKEN", path: relativePath, token });
    }
  }

  const rendererEntry = readFromRoot(resolvedRoot, "desktop/src/main.tsx");
  const legacyApp = readFromRoot(resolvedRoot, "desktop/src/App.tsx");
  const preload = readFromRoot(resolvedRoot, "desktop/electron/preload.cjs");
  const main = readFromRoot(resolvedRoot, "desktop/electron/main.cjs");
  const requiredMarkers = [
    ["legacy renderer App import", rendererEntry, 'import App from "@/App";'],
    ["legacy renderer mount", rendererEntry, "<App />"],
    ["legacy capture UI", legacyApp, "useEchoCapture"],
    ["legacy meeting UI", legacyApp, "MeetingList"],
    ["legacy history preload", preload, "loadLocalLegacyHistory"],
    ["injected backend endpoint", main, "ECHODESK_BASE_URL"],
    ["canonical backend lifecycle", main, "createBackendLifecycleController"],
  ];
  for (const [name, source, marker] of requiredMarkers) {
    if (!source.includes(marker)) failures.push({ code: "LEGACY_MARKER_MISSING", name, marker });
  }
  for (const [name, source] of [
    ["renderer entry", rendererEntry],
    ["legacy App", legacyApp],
    ["preload", preload],
  ]) {
    if (/CoreShell|core-shell|agent-kernel|packaged-fused/i.test(source)) {
      failures.push({ code: "LEGACY_RENDERER_CORE_REFERENCE", name });
    }
  }

  return {
    status: failures.length === 0 ? "PASS" : "FAIL",
    failure_count: failures.length,
    failures,
    legacy_source_record: LEGACY_SOURCE_RECORD,
    preserved_contracts: [
      "legacy renderer App/main/preload",
      "public model gateway",
      "window lifecycle",
      "ECHODESK_BASE_URL endpoint injection",
      "canonical release entrypoint",
      "legacy capture/meeting/LLM and HTTP AgentOS compatibility",
    ],
  };
}

function readFromRoot(root, relativePath) {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

function assertLegacySourceClosure(root = ECHO_ROOT) {
  const result = scanLegacySourceClosure(root);
  if (result.failure_count !== 0) {
    throw new Error(`[legacy-source-closure] failure_count=${result.failure_count}\n${JSON.stringify(result.failures, null, 2)}`);
  }
  return result;
}

if (require.main === module) console.log(JSON.stringify(assertLegacySourceClosure(), null, 2));

module.exports = { FORBIDDEN_PATHS, LEGACY_SOURCE_RECORD, assertLegacySourceClosure, scanLegacySourceClosure };
