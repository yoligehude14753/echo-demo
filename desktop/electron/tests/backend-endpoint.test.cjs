const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const config = require(path.resolve(__dirname, "../../backend.config.json"));
const {
  resolveBackendEndpoint,
  resolveShareBackendBase,
} = require("../backend-endpoint.cjs");

test("local mode connects over loopback while LAN share binds explicitly", () => {
  const runtime = resolveBackendEndpoint(config, {});

  assert.deepEqual(runtime, {
    mode: "local",
    port: 8769,
    localHost: "127.0.0.1",
    localBase: "http://127.0.0.1:8769",
    publicBase: "https://echodesk.yoliyoli.uk",
    backendBase: "http://127.0.0.1:8769",
    bindHost: "0.0.0.0",
    bindScope: "lan",
    spawnBackend: true,
  });
  assert.equal(
    resolveShareBackendBase(runtime, { lanAddress: "192.168.199.42" }),
    "http://192.168.199.42:8769",
  );
});

test("loopback bind override never advertises an unreachable LAN address", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHO_BACKEND_BIND_HOST: "127.0.0.1",
  });

  assert.equal(runtime.bindScope, "local");
  assert.equal(
    resolveShareBackendBase(runtime, { lanAddress: "192.168.199.42" }),
    "http://127.0.0.1:8769",
  );
});

test("an externally managed local backend never implies LAN reachability", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHO_SPAWN_BACKEND: "0",
  });

  assert.equal(runtime.spawnBackend, false);
  assert.equal(runtime.bindScope, "lan");
  assert.equal(
    resolveShareBackendBase(runtime, { lanAddress: "192.168.199.42" }),
    "http://127.0.0.1:8769",
  );
  assert.equal(
    resolveShareBackendBase(runtime, {
      lanAddress: "192.168.199.42",
      shareBaseUrl: "https://share.example.test/",
    }),
    "https://share.example.test",
  );
});

test("runtime fallback to an occupied external port disables automatic LAN URL", () => {
  const runtime = resolveBackendEndpoint(config, {});

  assert.equal(
    resolveShareBackendBase(runtime, {
      lanAddress: "192.168.199.42",
      allowLan: false,
    }),
    "http://127.0.0.1:8769",
  );
});

test("public mode and its custom host are authoritative for every URL", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHO_PUBLIC_DEMO: "1",
    ECHO_PUBLIC_BACKEND_BASE: "api.example.test/root/",
  });

  assert.equal(runtime.mode, "public");
  assert.equal(runtime.backendBase, "http://api.example.test/root");
  assert.equal(runtime.spawnBackend, false);
  assert.equal(
    resolveShareBackendBase(runtime, { lanAddress: "192.168.199.42" }),
    "http://api.example.test/root",
  );
});

test("force-local mode takes precedence over public demo mode", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHO_PUBLIC_DEMO: "1",
    ECHO_FORCE_LOCAL_BACKEND: "1",
    ECHO_BACKEND_PORT: "19001",
  });

  assert.equal(runtime.mode, "local");
  assert.equal(runtime.backendBase, "http://127.0.0.1:19001");
  assert.equal(runtime.spawnBackend, true);
});

test("invalid endpoint configuration fails closed", () => {
  assert.throws(
    () => resolveBackendEndpoint(config, { ECHO_BACKEND_PORT: "8769junk" }),
    /invalid port/,
  );
  assert.throws(
    () =>
      resolveBackendEndpoint(
        {
          ...config,
          lanShare: { enabled: true, bindHost: "127.0.0.1" },
        },
        {},
      ),
    /non-loopback bind host/,
  );
});

test("preload publishes the main-process backend host before renderer startup", () => {
  const preloadSource = readFileSync(
    path.resolve(__dirname, "../preload.cjs"),
    "utf8",
  );
  const syncChannels = [];
  let exposedName = null;
  let exposedBridge = null;
  const ipcRenderer = {
    sendSync(channel) {
      syncChannels.push(channel);
      if (channel === "echo:is-public-demo") return true;
      if (channel === "echo:backend-host-sync") return "https://api.example.test";
      throw new Error(`unexpected sync channel: ${channel}`);
    },
    invoke() {
      return Promise.resolve(null);
    },
    on() {},
    removeListener() {},
  };
  const contextBridge = {
    exposeInMainWorld(name, bridge) {
      exposedName = name;
      exposedBridge = bridge;
    },
  };

  vm.runInNewContext(preloadSource, {
    require(specifier) {
      if (specifier === "electron") return { contextBridge, ipcRenderer };
      throw new Error(`unexpected preload dependency: ${specifier}`);
    },
  });

  assert.equal(exposedName, "echo");
  assert.equal(exposedBridge.isPublicDemo, true);
  assert.equal(exposedBridge.backendHost, "https://api.example.test");
  assert.deepEqual(syncChannels, [
    "echo:is-public-demo",
    "echo:backend-host-sync",
  ]);
});
