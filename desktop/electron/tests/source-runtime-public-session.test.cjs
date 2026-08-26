"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  electronLaunchArgs,
  prepareSourceElectronRuntime,
  publicProxyTarget,
  resolveSourceBackendEndpoint,
} = require("../../scripts/start-electron-dev.cjs");

const desktopRoot = path.resolve(__dirname, "../..");

test("public source supervisor selects the configured HTTPS backend for Vite proxying", () => {
  assert.equal(
    publicProxyTarget({
      ECHO_PRINCIPAL_MODE: "public",
      ECHO_PUBLIC_BACKEND_BASE: "https://public.example.test",
    }),
    "https://public.example.test",
  );
  assert.equal(
    publicProxyTarget({
      ECHO_PRINCIPAL_MODE: "public",
      VITE_API_TARGET: "https://explicit.example.test",
      ECHO_PUBLIC_BACKEND_BASE: "https://ignored.example.test",
    }),
    "https://explicit.example.test",
  );
});

test("local source supervisor allocates one endpoint before Vite starts", async () => {
  const calls = [];
  const endpoint = await resolveSourceBackendEndpoint({}, async () => {
    calls.push("allocate");
    return "http://127.0.0.1:19345";
  });
  assert.equal(endpoint, "http://127.0.0.1:19345");
  assert.deepEqual(calls, ["allocate"]);
});

test("development certificate exception is limited to the injected Vite origin", () => {
  const main = fs.readFileSync(path.join(desktopRoot, "electron/main.cjs"), "utf8");
  assert.match(main, /app\.on\("certificate-error"/);
  assert.match(main, /candidate\.origin !== vite\.origin/);
  assert.match(main, /ERR_CERT_AUTHORITY_INVALID/);
  assert.match(main, /callback\(true\)/);
});

test("source backend injects yoli_llm without changing packaged backend environment", () => {
  const main = fs.readFileSync(path.join(desktopRoot, "electron/main.cjs"), "utf8");
  assert.match(
    main,
    /const sourceBackendPythonPath = bundledBackend\s*\? null\s*:\s*\[[\s\S]*?"_platforms", "llm", "src"[\s\S]*?inheritedBackendEnv\.PYTHONPATH[\s\S]*?join\(path\.delimiter\)/,
  );
  assert.match(main, /sourceBackendPythonPath \? \{ PYTHONPATH: sourceBackendPythonPath \} : \{\}/);
});

test("source public Electron keeps durable identity and proxy boundaries explicit", () => {
  const main = fs.readFileSync(path.join(desktopRoot, "electron/main.cjs"), "utf8");
  const publicSession = fs.readFileSync(
    path.join(desktopRoot, "electron/public-identity-session.cjs"),
    "utf8",
  );
  const runtime = fs.readFileSync(path.join(desktopRoot, "src/runtime.ts"), "utf8");
  const session = fs.readFileSync(path.join(desktopRoot, "src/session.ts"), "utf8");
  assert.match(main, /app\.setName\(DESKTOP_PRODUCT_NAME\)/);
  assert.match(main, /createPublicIdentitySessionManager/);
  assert.match(main, /probePublicBackendContract\(BACKEND_HOST\)/);
  assert.match(main, /ensurePublicSessionInMain\(\)/);
  assert.match(main, /safeStorage/);
  assert.match(main, /createCredentialVault/);
  assert.match(main, /allowEnrollment: false/);
  assert.match(main, /credential:enroll-session/);
  assert.match(main, /public-device-identity\.bin/);
  assert.doesNotMatch(publicSession, /createEphemeralPublicSessionManager\([\s\S]*?(?:node:fs|writeFile|mkdirSync|safeStorage|vault\.)/);
  assert.match(main, /ipcMain\.handle\("credential:ensure-session"[\s\S]*?forceBootstrap: true/);
  assert.match(runtime, /usesElectronViteProxy/);
  assert.match(session, /window\.echo\?\.backendHost/);
  assert.match(session, /actualOrigin === window\.location\.origin/);
  assert.match(
    session,
    /usesViteProxyOrigin[\s\S]*?actualOrigin !== leaseOrigin && !usesViteProxyOrigin/,
  );
  assert.match(
    session,
    /usesViteProxyResponseOrigin[\s\S]*?responseOrigin !== leaseOrigin && !usesViteProxyResponseOrigin/,
  );
});

test("source Electron supervisor prepares the branded secure-storage runtime", () => {
  const calls = [];
  prepareSourceElectronRuntime({
    execFile(...args) {
      calls.push(args);
    },
    env: { ...process.env },
  });
  if (process.platform === "darwin") {
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], process.execPath);
    assert.match(calls[0][1][0], /electron\/scripts\/brand-dev-electron\.cjs$/);
    assert.equal(calls[0][2].stdio, "inherit");
  } else {
    assert.deepEqual(calls, []);
  }
});

test("source Electron accepts only an explicit absolute isolated user-data path", () => {
  const userDataDir = path.join(
    path.parse(desktopRoot).root,
    "tmp",
    "echodesk-source-runtime",
    "user-data",
  );
  const args = electronLaunchArgs({
    ECHODESK_ELECTRON_USER_DATA_DIR: userDataDir,
  });
  assert.match(args[0], /electron[\\/]main\.cjs$/);
  assert.equal(args[1], `--user-data-dir=${userDataDir}`);
  assert.throws(
    () => electronLaunchArgs({ ECHODESK_ELECTRON_USER_DATA_DIR: "relative/path" }),
    /must be absolute/,
  );
});
