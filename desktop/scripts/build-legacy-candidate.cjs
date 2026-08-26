#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const {
  installedMacTeamId,
  resolveMacIdentity,
} = require("./desktop-release-signing.cjs");
const {
  signAdhocMacBundle,
  signDeveloperIdMacBundle,
} = require("./mac-bundle-sign.cjs");

const desktopRoot = path.resolve(__dirname, "..");
const releaseRoot = path.join(desktopRoot, "release", "legacy-candidate");
const configPath = path.join(desktopRoot, "legacy-candidate-builder.json");
const forbidden = [
  "electron/agent-runtime",
  "agent-kernel",
  ".agent-runtime-package",
  ".codex-tmp",
  "fused",
  "B13",
  "CoreShell",
];

function fail(message) {
  throw new Error(`[legacy-candidate] ${message}`);
}

function run(command, args, env = process.env) {
  const result = spawnSync(command, args, { cwd: desktopRoot, stdio: "inherit", env });
  if (result.error || result.status !== 0) fail(`${command} failed`);
}

function staticCheck() {
  const pkg = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
  if (pkg.build?.productName !== "EchoDesk" || pkg.build?.appId !== "com.echodesk.app") {
    fail("canonical package identity or app:dist semantics changed");
  }
  if (JSON.stringify(pkg.build?.extraResources || []).includes("agent-runtime")) {
    fail("canonical extraResources contains forbidden runtime");
  }
  const source = fs.readFileSync(path.join(desktopRoot, "electron", "main.cjs"), "utf8");
  if (source.indexOf("app.setPath(\"userData\"") > source.indexOf("app.requestSingleInstanceLock()")) {
    fail("userData isolation is after single-instance lock");
  }
  console.log("[legacy-candidate] static configuration check: PASS");
}

function verifyCandidateTree() {
  if (!fs.existsSync(releaseRoot)) fail(`missing output ${releaseRoot}`);
  const entries = [];
  const visit = (current) => {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const full = path.join(current, entry.name);
      const relative = path.relative(releaseRoot, full);
      entries.push(relative);
      if (entry.isDirectory()) visit(full);
    }
  };
  visit(releaseRoot);
  const hits = entries.filter((entry) => forbidden.some((token) => entry.includes(token)));
  if (hits.length) fail(`forbidden candidate resources: ${hits.join(", ")}`);
  console.log(`[legacy-candidate] forbidden resource scan: PASS (${entries.length} entries)`);
}

function capture(command, args) {
  const result = spawnSync(command, args, {
    cwd: desktopRoot,
    encoding: "utf8",
  });
  if (result.error || result.status !== 0) return null;
  return `${result.stdout || ""}\n${result.stderr || ""}`;
}

function candidateSigningHash() {
  const configured = String(process.env.CSC_NAME || "").trim();
  if (configured) return configured;
  const canonicalApp = "/Applications/EchoDesk.app";
  if (!fs.existsSync(canonicalApp)) return null;
  const metadata = capture("codesign", ["--display", "--verbose=4", canonicalApp]);
  const identities = capture("security", ["find-identity", "-v", "-p", "codesigning"]);
  if (!metadata || !identities) return null;
  try {
    return resolveMacIdentity(identities, installedMacTeamId(metadata)).hash;
  } catch {
    return null;
  }
}

function signCandidate() {
  const appPath = path.join(releaseRoot, "mac-arm64", "EchoDesk Legacy Candidate.app");
  const signingHash = candidateSigningHash();
  if (signingHash) {
    signDeveloperIdMacBundle(appPath, signingHash, { timestamp: true });
    console.log("[legacy-candidate] stable Developer ID signing: PASS");
  } else {
    signAdhocMacBundle(appPath);
    console.log("[legacy-candidate] stable Developer ID unavailable; development ad-hoc signing used");
  }
}

staticCheck();
fs.rmSync(releaseRoot, { recursive: true, force: true });
run("npm", ["run", "backend:build:mac"]);
const candidateEnv = {
  ...process.env,
  ECHODESK_BASE_URL: process.env.ECHODESK_BASE_URL || "http://127.0.0.1:23145",
  ECHO_USER_DIR: process.env.ECHO_USER_DIR || path.join(releaseRoot, "runtime-user"),
  CSC_IDENTITY_AUTO_DISCOVERY: "false",
};
run("npm", ["run", "build"], candidateEnv);
run("npx", ["electron-builder", "--config", configPath, "--mac", "dir", "--arm64", "--publish", "never"], candidateEnv);
signCandidate();
verifyCandidateTree();
console.log(`[legacy-candidate] output=${releaseRoot}`);
