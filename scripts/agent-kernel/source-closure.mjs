import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(scriptDir, "../..");
const allowedDecisions = new Set(["PORT_AS_IS", "ADAPT", "REWRITE", "EXCLUDE"]);
const expectedCounts = {
  resolved_nodes: 1780,
  lexical_edges: 11988,
  unresolved_local_specifiers: 616,
  dynamic_or_require_edges: 602,
  external_package_names: 134,
};

function readJson(root, relativePath) {
  return JSON.parse(readFileSync(join(root, relativePath), "utf8"));
}

function fail(errors, message) {
  errors.push(message);
}

function sameMembers(left, right) {
  return left.length === right.length && [...left].sort().every((value, index) => value === [...right].sort()[index]);
}

function checkDecision(errors, item, label) {
  if (!item || !allowedDecisions.has(item.decision)) {
    fail(errors, `${label} has no allowed decision`);
  }
}

function checkBindings(errors, manifest, graph, ledger, identity, f04) {
  const snapshot = "sha256:b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a";
  const root = "b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a";
  const baseline = "492053c53441793c220f3b8e1dd231f1faea6e42";
  for (const [label, value, expected] of [
    ["manifest snapshot", manifest.frozen_identity.source_snapshot_id, snapshot],
    ["graph snapshot", graph.source_snapshot_id, snapshot],
    ["ledger snapshot", ledger.source_snapshot_id, snapshot],
    ["manifest root", manifest.frozen_identity.manifest_root_sha256, root],
    ["graph root", graph.manifest_root_sha256, root],
    ["ledger root", ledger.manifest_root_sha256, root],
    ["manifest Echo baseline", manifest.echo_binding.compatibility_baseline_sha, baseline],
    ["graph Echo baseline", graph.echo_baseline_sha, baseline],
    ["ledger Echo baseline", ledger.echo_baseline_sha, baseline],
    ["F04 snapshot", f04.claude.source_snapshot_id, snapshot],
    ["F04 baseline", f04.echo.compatibility_evidence_baseline_sha, baseline],
  ]) {
    if (value !== expected) fail(errors, `${label} mismatch`);
  }
  if (manifest.contract_versions.kernel_api !== 1 || ledger.contract_versions.kernel_api !== 1) {
    fail(errors, "kernel contract version is not 1");
  }
  if (manifest.runtime_requirement.electron !== "43.1.0" || manifest.runtime_requirement.modules_abi !== "148") {
    fail(errors, "runtime requirement is not the frozen F04 runtime");
  }
  if (identity.manifest_sha256 !== root || identity.source_snapshot_id !== snapshot) {
    fail(errors, "F01 identity binding mismatch");
  }
}

function checkEntrypoints(errors, manifest, graph, identity) {
  const identityByPath = new Map(identity.entrypoints.map((entry) => [entry.path, entry.sha256]));
  for (const entry of manifest.frozen_identity.entrypoints) {
    if (identityByPath.get(entry.path) !== entry.sha256) fail(errors, `entrypoint hash mismatch: ${entry.path}`);
    checkDecision(errors, entry, `entrypoint ${entry.path}`);
  }
  if (!sameMembers(graph.roots.map((root) => root.path), ["query.ts", "QueryEngine.ts", "Tool.ts"])) {
    fail(errors, "root set is not query.ts, QueryEngine.ts, Tool.ts");
  }
}

function checkF01Graph(errors, graph, f01Graph) {
  for (const [key, expected] of Object.entries(expectedCounts)) {
    if (f01Graph.analysis[key] !== expected || graph.f01_counts[key] !== expected) {
      fail(errors, `F01 count mismatch: ${key}`);
    }
  }
  const expectedDirect = f01Graph.direct_query_imports.resolved_local;
  const actualDirect = graph.direct_edges.map((edge) => edge.to);
  if (!sameMembers(actualDirect, expectedDirect)) fail(errors, "direct query edge set differs from F01 evidence");
  if (!sameMembers(graph.external_edges.map((edge) => edge.to), f01Graph.direct_query_imports.external_or_runtime)) {
    fail(errors, "external direct edge set differs from F01 evidence");
  }
  if (!sameMembers(graph.unresolved_local_edges.map((edge) => edge.to), f01Graph.direct_query_imports.unresolved_local_or_generated)) {
    fail(errors, "unresolved direct edge set differs from F01 evidence");
  }
}

function checkClosureDecisions(errors, manifest, graph, ledger) {
  const modules = new Map(manifest.module_decisions.map((module) => [module.path, module]));
  for (const module of manifest.module_decisions) checkDecision(errors, module, `module ${module.path}`);
  for (const edge of [...graph.direct_edges, ...graph.external_edges, ...graph.unresolved_local_edges, ...graph.dynamic_or_require_edges, ...graph.external_dependency_groups, ...graph.coverage_gaps, ...graph.forbidden_surfaces]) {
    checkDecision(errors, edge, `graph item ${edge.from ?? edge.root ?? edge.name ?? edge.group ?? edge.to}`);
  }
  for (const edge of graph.direct_edges) {
    if (!modules.has(edge.to)) fail(errors, `direct edge target missing module decision: ${edge.to}`);
    if (modules.get(edge.to)?.decision !== edge.decision) fail(errors, `edge/module decision mismatch: ${edge.to}`);
  }
  for (const item of ledger.decisions) checkDecision(errors, item, `ledger ${item.id}`);
  const explicit = new Set(manifest.policy.forbidden_surface);
  for (const surface of ledger.explicit_exclusions) {
    if (!explicit.has(surface)) fail(errors, `explicit exclusion absent from manifest policy: ${surface}`);
  }
  const forbiddenNames = ["cli parser", "terminal ui", "agentos", "proxy daemon", "global auth", "global config", "history", "update", "telemetry"];
  const forbiddenItems = [...graph.forbidden_surfaces, ...ledger.decisions, ...manifest.module_decisions];
  for (const item of forbiddenItems) {
    const text = JSON.stringify(item).toLowerCase();
    if (forbiddenNames.some((name) => text.includes(name)) && item.decision === "PORT_AS_IS") {
      fail(errors, `forbidden surface marked PORT_AS_IS: ${text}`);
    }
  }
  if (graph.dynamic_or_require_edges.some((edge) => edge.decision === "PORT_AS_IS")) {
    fail(errors, "dynamic edge cannot be PORT_AS_IS");
  }
  if (graph.external_edges.some((edge) => edge.decision === "PORT_AS_IS")) {
    fail(errors, "external edge cannot be PORT_AS_IS");
  }
}

export function runGate(root = repoRoot) {
  const errors = [];
  const manifest = readJson(root, "desktop/agent-kernel/source/source-closure-manifest.json");
  const graph = readJson(root, "desktop/agent-kernel/source/source-import-graph.json");
  const ledger = readJson(root, "desktop/agent-kernel/source/source-decision-ledger.json");
  const identity = readJson(root, "docs/0.3.3-bundled-agent-runtime/evidence/F01/claude-source-identity.json");
  const f01Graph = readJson(root, "docs/0.3.3-bundled-agent-runtime/evidence/F01/query-production-import-graph.json");
  const f04 = readJson(root, "docs/0.3.3-bundled-agent-runtime/evidence/F04/FUSION_COMPATIBILITY_BASELINE_V1.json");
  checkBindings(errors, manifest, graph, ledger, identity, f04);
  checkEntrypoints(errors, manifest, graph, identity);
  checkF01Graph(errors, graph, f01Graph);
  checkClosureDecisions(errors, manifest, graph, ledger);
  return {
    status: errors.length === 0 ? "PASS" : "FAIL",
    artifact: "B01-source-closure-static-gate",
    checks: [
      "frozen snapshot and manifest root binding",
      "Echo baseline/runtime/contract binding",
      "F01 direct and aggregate graph consistency",
      "per-module and per-edge decision completeness",
      "forbidden surface fail-closed policy"
    ],
    errors
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const result = runGate();
  console.log(JSON.stringify(result, null, 2));
  if (result.status !== "PASS") process.exitCode = 1;
}
