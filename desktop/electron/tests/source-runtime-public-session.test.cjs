"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  electronLaunchArgs,
  prepareSourceElectronRuntime,
  publicProxyTarget,
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

test("source public Electron keeps ephemeral identity and proxy boundaries explicit", () => {
  const main = fs.readFileSync(path.join(desktopRoot, "electron/main.cjs"), "utf8");
  const publicSession = fs.readFileSync(
    path.join(desktopRoot, "electron/public-identity-session.cjs"),
    "utf8",
  );
  const runtime = fs.readFileSync(path.join(desktopRoot, "src/runtime.ts"), "utf8");
  const session = fs.readFileSync(path.join(desktopRoot, "src/session.ts"), "utf8");
  assert.match(main, /app\.setName\("EchoDesk"\)/);
  assert.match(main, /createEphemeralPublicSessionManager/);
  assert.match(main, /probePublicBackendContract\(BACKEND_HOST\)/);
  assert.match(main, /ensurePublicSessionInMain\(\)/);
  assert.doesNotMatch(main, /safeStorage|createCredentialVault|credentialVault\(/);
  assert.doesNotMatch(main, /public-device-credential\.bin/);
  assert.doesNotMatch(publicSession, /createEphemeralPublicSessionManager\([\s\S]*?(?:node:fs|writeFile|mkdirSync|safeStorage|vault\.)/);
  assert.match(
    main,
    /ipcMain\.handle\("credential:ensure-session"[\s\S]*?forceBootstrap: true/,
  );
  assert.match(runtime, /usesElectronViteProxy/);
  assert.match(session, /window\.echo\?\.backendHost/);
  assert.match(session, /actualOrigin === window\.location\.origin/);
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
  const args = electronLaunchArgs({
    ECHODESK_ELECTRON_USER_DATA_DIR: "/tmp/echodesk-source-runtime/user-data",
  });
  assert.match(args[0], /electron[\\/]main\.cjs$/);
  assert.equal(args[1], "--user-data-dir=/tmp/echodesk-source-runtime/user-data");
  assert.throws(
    () => electronLaunchArgs({ ECHODESK_ELECTRON_USER_DATA_DIR: "relative/path" }),
    /must be absolute/,
  );
});
