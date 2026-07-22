"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { publicProxyTarget } = require("../../scripts/start-electron-dev.cjs");

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

test("source public Electron keeps secure identity and proxy boundaries explicit", () => {
  const main = fs.readFileSync(path.join(desktopRoot, "electron/main.cjs"), "utf8");
  const runtime = fs.readFileSync(path.join(desktopRoot, "src/runtime.ts"), "utf8");
  const session = fs.readFileSync(path.join(desktopRoot, "src/session.ts"), "utf8");
  assert.match(main, /app\.setName\("EchoDesk"\)/);
  assert.match(main, /safeStorage/);
  assert.doesNotMatch(main, /setUsePlainTextEncryption\(true\)/);
  assert.match(runtime, /usesElectronViteProxy/);
  assert.match(session, /window\.echo\?\.backendHost/);
  assert.match(session, /actualOrigin === window\.location\.origin/);
});
