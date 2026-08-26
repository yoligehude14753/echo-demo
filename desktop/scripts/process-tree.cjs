"use strict";

// One macOS-compatible process-tree reader for release promotion and core E2E.
// It reads only PID/PPID/comm fields; command arguments are deliberately never
// inspected because they can contain endpoints or credentials.
const { basename, resolve } = require("node:path");
const { spawnSync } = require("node:child_process");

function fail(code) {
  throw new Error(`[process-tree] ${code}`);
}

function parseProcessTable(output) {
  const rows = [];
  for (const line of String(output || "").split(/\r?\n/)) {
    const match = /^\s*(\d+)\s+(\d+)\s+(.+?)\s*$/.exec(line);
    if (!match) continue;
    const pid = Number(match[1]);
    const ppid = Number(match[2]);
    if (!Number.isSafeInteger(pid) || pid <= 0 || !Number.isSafeInteger(ppid) || ppid < 0) continue;
    rows.push({ pid, ppid, comm: match[3] });
  }
  return rows;
}

function readProcessTable({ runner = spawnSync } = {}) {
  const result = runner("/bin/ps", ["-axo", "pid=,ppid=,comm="], { encoding: "utf8" });
  if (result?.status !== 0) fail("process_table_unavailable");
  return parseProcessTable(result.stdout);
}

function descendantPids(rootPid, { listProcesses = readProcessTable } = {}) {
  if (!Number.isSafeInteger(rootPid) || rootPid <= 0) fail("invalid_root_pid");
  const rows = listProcesses();
  const children = new Map();
  for (const row of rows) {
    const values = children.get(row.ppid) || [];
    values.push(row.pid);
    children.set(row.ppid, values);
  }
  const queue = [rootPid];
  const seen = new Set();
  while (queue.length > 0) {
    const pid = queue.shift();
    if (seen.has(pid)) continue;
    seen.add(pid);
    for (const child of children.get(pid) || []) queue.push(child);
    if (seen.size > 256) fail("process_tree_too_large");
  }
  return [...seen];
}

function matchingExecutablePids(rows, allowedPids, executablePath) {
  const exact = resolve(executablePath);
  const name = basename(exact);
  const allowed = new Set(allowedPids);
  return rows
    .filter((row) => allowed.has(row.pid))
    .filter((row) => row.comm === exact || basename(row.comm) === name)
    .map((row) => row.pid);
}

function logicalProcessFamilyCount(pids, rows) {
  const members = new Set(pids);
  const parentByPid = new Map(rows.map((row) => [row.pid, row.ppid]));
  let roots = 0;
  for (const pid of members) {
    let parent = parentByPid.get(pid);
    let hasMemberAncestor = false;
    const visited = new Set();
    while (Number.isSafeInteger(parent) && parent > 0 && !visited.has(parent)) {
      if (members.has(parent)) {
        hasMemberAncestor = true;
        break;
      }
      visited.add(parent);
      parent = parentByPid.get(parent);
    }
    if (!hasMemberAncestor) roots += 1;
  }
  return roots;
}

function defaultListeningPids(pids, { runner = spawnSync } = {}) {
  if (pids.length === 0) return [];
  const result = runner(
    "/usr/sbin/lsof",
    ["-nP", "-a", "-p", pids.join(","), "-iTCP", "-sTCP:LISTEN", "-Fp"],
    { encoding: "utf8" },
  );
  if (![0, 1].includes(result?.status)) fail("listener_inspection_failed");
  return [...new Set(String(result.stdout || "")
    .split(/\r?\n/)
    .filter((line) => /^p\d+$/.test(line))
    .map((line) => Number(line.slice(1))))];
}

function inspectPackagedRuntime(rootPid, backendExecutable, {
  listProcesses = readProcessTable,
  listListeningPids = defaultListeningPids,
} = {}) {
  const rows = listProcesses();
  const tree = descendantPids(rootPid, { listProcesses: () => rows });
  const treeSet = new Set(tree);
  const main = rows.filter((row) => row.pid === rootPid && treeSet.has(row.pid));
  const backendPids = matchingExecutablePids(rows, tree, backendExecutable);
  const listenerPids = listListeningPids(backendPids).filter((pid) => backendPids.includes(pid));
  return {
    main_count: main.length,
    logical_backend_count: logicalProcessFamilyCount(backendPids, rows),
    backend_process_count: backendPids.length,
    listener_count: new Set(listenerPids).size,
    process_tree_pid_count: tree.length,
    ready_bool:
      main.length === 1 &&
      logicalProcessFamilyCount(backendPids, rows) === 1 &&
      new Set(listenerPids).size === 1,
  };
}

module.exports = {
  defaultListeningPids,
  descendantPids,
  inspectPackagedRuntime,
  logicalProcessFamilyCount,
  matchingExecutablePids,
  parseProcessTable,
  readProcessTable,
};
