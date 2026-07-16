const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const config = require(path.resolve(__dirname, "../../backend.config.json"));
const legacyConfig = {
  local: { host: "127.0.0.1", port: 8769 },
  lanShare: { enabled: true, bindHost: "0.0.0.0" },
  public: { baseUrl: "https://legacy.example.test" },
};
const {
  resolveBackendEndpoint,
  resolveShareBackendBase,
} = require("../backend-endpoint.cjs");

test("local mode connects over loopback while LAN share binds explicitly", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHO_RUNTIME_MODE: "development",
  });

  assert.deepEqual({
    mode: "local",
    principalMode: "local",
    runtimeMode: "development",
    role: "local_dev_diagnostic",
    source: "development-default-local",
    schemaVersion: 2,
    port: 8769,
    localHost: "127.0.0.1",
    localBase: "http://127.0.0.1:8769",
    publicBase: "https://echodesk.yoliyoli.uk",
    publicServiceEndpoint: "https://echodesk.yoliyoli.uk",
    pairedHubSyncGatewayEndpoint: null,
    localDevDiagnosticEndpoint: "http://127.0.0.1:8769",
    backendBase: "http://127.0.0.1:8769",
    bindHost: "0.0.0.0",
    bindScope: "lan",
    spawnBackend: true,
  }, runtime);
  assert.equal(
    resolveShareBackendBase(runtime, { lanAddress: "192.168.199.42" }),
    "http://192.168.199.42:8769",
  );
});

test("loopback bind override never advertises an unreachable LAN address", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHO_RUNTIME_MODE: "development",
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
    ECHO_RUNTIME_MODE: "development",
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
  const runtime = resolveBackendEndpoint(config, {
    ECHO_RUNTIME_MODE: "development",
  });

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
    ECHO_RUNTIME_MODE: "diagnostic",
    ECHO_PRINCIPAL_MODE: "public",
    ECHO_PUBLIC_BACKEND_BASE: "https://api.example.test/",
  });

  assert.equal(runtime.mode, "public");
  assert.equal(runtime.role, "public_service");
  assert.equal(runtime.backendBase, "https://api.example.test");
  assert.equal(runtime.spawnBackend, false);
  assert.equal(
    resolveShareBackendBase(runtime, { lanAddress: "192.168.199.42" }),
    "https://api.example.test",
  );
});

test("packaged release defaults to the bundled local backend and ignores stale public env overrides", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHO_PUBLIC_DEMO: "1",
    ECHO_BACKEND_PORT: "19001",
    ECHO_PUBLIC_BACKEND_BASE: "https://stale.example.test",
  });

  assert.equal(runtime.runtimeMode, "release");
  assert.equal(runtime.mode, "local");
  assert.equal(runtime.role, "local_dev_diagnostic");
  assert.equal(runtime.source, "release-default-local");
  assert.equal(runtime.backendBase, "http://127.0.0.1:8769");
  assert.equal(runtime.spawnBackend, true);
  assert.equal(runtime.localDevDiagnosticEndpoint, "http://127.0.0.1:8769");
});

test("packaged release keeps remote service as an explicit opt-in", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHO_PRINCIPAL_MODE: "public",
  });

  assert.equal(runtime.runtimeMode, "release");
  assert.equal(runtime.mode, "public");
  assert.equal(runtime.role, "public_service");
  assert.equal(runtime.source, "explicit-principal-mode");
  assert.equal(runtime.backendBase, "https://echodesk.yoliyoli.uk");
  assert.equal(runtime.spawnBackend, false);
  assert.equal(runtime.localDevDiagnosticEndpoint, null);
});

test("legacy v1 packaged config defaults to its bundled local backend", () => {
  const runtime = resolveBackendEndpoint(legacyConfig, {});

  assert.equal(runtime.schemaVersion, 1);
  assert.equal(runtime.runtimeMode, "release");
  assert.equal(runtime.role, "local_dev_diagnostic");
  assert.equal(runtime.source, "release-default-local");
  assert.equal(runtime.backendBase, "http://127.0.0.1:8769");
  assert.equal(runtime.spawnBackend, true);
  assert.equal(runtime.localDevDiagnosticEndpoint, "http://127.0.0.1:8769");
});

test("legacy v1 local config is available only in explicit development", () => {
  const runtime = resolveBackendEndpoint(legacyConfig, {
    ECHO_RUNTIME_MODE: "development",
    ECHO_PRINCIPAL_MODE: "local",
  });

  assert.equal(runtime.schemaVersion, 1);
  assert.equal(runtime.role, "local_dev_diagnostic");
  assert.equal(runtime.backendBase, "http://127.0.0.1:8769");
  assert.equal(runtime.localDevDiagnosticEndpoint, "http://127.0.0.1:8769");
});

test("unknown config versions and enabled Hub roles fail closed", () => {
  assert.throws(
    () => resolveBackendEndpoint({ ...config, schemaVersion: 3 }, {}),
    (error) => error?.code === "unknown_schema_version",
  );
  assert.throws(
    () =>
      resolveBackendEndpoint(
        {
          ...config,
          roles: {
            ...config.roles,
            pairedHubSyncGateway: { enabled: true, baseUrl: "https://hub.example.test" },
          },
        },
        {},
      ),
    (error) => error?.code === "invalid_paired_hub_role",
  );
});

test("explicit local mode remains the packaged bundled-worker contract", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHO_PRINCIPAL_MODE: "local",
  });

  assert.equal(runtime.mode, "local");
  assert.equal(runtime.role, "local_dev_diagnostic");
  assert.equal(runtime.source, "explicit-principal-mode");
  assert.equal(runtime.spawnBackend, true);
});

test("paired Hub gateway is never selected as a public service fallback", () => {
  assert.throws(
    () =>
      resolveBackendEndpoint(config, {
        ECHO_RUNTIME_MODE: "diagnostic",
        ECHO_PAIRED_HUB_SYNC_GATEWAY_BASE: "https://hub.example.test",
      }),
    (error) => error?.code === "hub_sync_gateway_not_supported",
  );
});

test("invalid endpoint configuration fails closed", () => {
  assert.throws(
    () =>
      resolveBackendEndpoint(config, {
        ECHO_RUNTIME_MODE: "development",
        ECHO_BACKEND_PORT: "8769junk",
      }),
    (error) => error?.code === "invalid_port",
  );
  assert.throws(
    () =>
      resolveBackendEndpoint(config, {
        ECHO_RUNTIME_MODE: "diagnostic",
        ECHO_PRINCIPAL_MODE: "public",
        ECHO_PUBLIC_BACKEND_BASE: "http://public.example.test",
      }),
    (error) => error?.code === "invalid_public_service_endpoint",
  );
  assert.throws(
    () =>
      resolveBackendEndpoint(config, {
        ECHO_RUNTIME_MODE: "diagnostic",
        ECHO_PRINCIPAL_MODE: "public",
        ECHO_PUBLIC_BACKEND_BASE: "https://api.example.test/root",
      }),
    (error) => error?.code === "invalid_public_service_endpoint",
  );
  assert.throws(
    () =>
      resolveBackendEndpoint(
        {
          ...config,
          lanShare: { enabled: true, bindHost: "127.0.0.1" },
        },
        { ECHO_RUNTIME_MODE: "development" },
      ),
    (error) => error?.code === "invalid_lan_bind_host",
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
      if (channel === "echo:backend-routing-sync") {
        return {
          runtimeMode: "release",
          principalMode: "public",
          role: "public_service",
          source: "release-config",
          schemaVersion: 2,
          backendBase: "https://api.example.test",
          publicServiceEndpoint: "https://api.example.test",
          pairedHubSyncGatewayEndpoint: null,
          localDevDiagnosticEndpoint: null,
        };
      }
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
  assert.equal(exposedBridge.backendRouting.role, "public_service");
  assert.equal(exposedBridge.backendRouting.pairedHubSyncGatewayEndpoint, null);
  assert.equal(exposedBridge.backendRouting.localDevDiagnosticEndpoint, null);
  assert.equal(exposedBridge.setBackendHost, undefined);
  assert.equal(exposedBridge.setBackendRouting, undefined);
  assert.deepEqual(syncChannels, [
    "echo:is-public-demo",
    "echo:backend-host-sync",
    "echo:backend-routing-sync",
  ]);
});
