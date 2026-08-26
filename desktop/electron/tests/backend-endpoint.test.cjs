const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const config = require(path.resolve(__dirname, "../../backend.config.json"));
const LOCAL_ENDPOINT = "http://127.0.0.1:19345";
const {
  resolveBackendEndpoint,
  resolveShareBackendBase,
} = require("../backend-endpoint.cjs");

function localEnv(overrides = {}) {
  return {
    ECHO_RUNTIME_MODE: "development",
    ECHODESK_BASE_URL: LOCAL_ENDPOINT,
    ...overrides,
  };
}

test("local mode connects over loopback while LAN share binds explicitly", () => {
  const runtime = resolveBackendEndpoint(config, localEnv());

  assert.deepEqual({
    mode: "local",
    principalMode: "local",
    runtimeMode: "development",
    role: "local_dev_diagnostic",
    source: "injected-runtime-endpoint",
    schemaVersion: 3,
    port: 19345,
    localHost: "127.0.0.1",
    localBase: LOCAL_ENDPOINT,
    publicBase: "https://echo.yoliyoli.uk",
    publicServiceEndpoint: "https://echo.yoliyoli.uk",
    pairedHubSyncGatewayEndpoint: "https://echo.yoliyoli.uk",
    localDevDiagnosticEndpoint: LOCAL_ENDPOINT,
    backendBase: LOCAL_ENDPOINT,
    bindHost: "0.0.0.0",
    bindScope: "lan",
    spawnBackend: true,
  }, runtime);
  assert.equal(
    resolveShareBackendBase(runtime, { lanAddress: "192.168.199.42" }),
    "http://192.168.199.42:19345",
  );
});

test("loopback bind override never advertises an unreachable LAN address", () => {
  const runtime = resolveBackendEndpoint(config, localEnv({
    ECHO_BACKEND_BIND_HOST: "127.0.0.1",
  }));

  assert.equal(runtime.bindScope, "local");
  assert.equal(
    resolveShareBackendBase(runtime, { lanAddress: "192.168.199.42" }),
    LOCAL_ENDPOINT,
  );
});

test("an externally managed local backend never implies LAN reachability", () => {
  const runtime = resolveBackendEndpoint(config, localEnv({
    ECHO_SPAWN_BACKEND: "0",
  }));

  assert.equal(runtime.spawnBackend, false);
  assert.equal(runtime.bindScope, "lan");
  assert.equal(
    resolveShareBackendBase(runtime, { lanAddress: "192.168.199.42" }),
    LOCAL_ENDPOINT,
  );
  assert.equal(
    resolveShareBackendBase(runtime, {
      lanAddress: "192.168.199.42",
      shareBaseUrl: "https://share.example.test/",
    }),
    "https://share.example.test",
  );
});

test("injected local endpoint respects an explicit LAN safety restriction", () => {
  const runtime = resolveBackendEndpoint(config, localEnv());

  assert.equal(
    resolveShareBackendBase(runtime, {
      lanAddress: "192.168.199.42",
      allowLan: false,
    }),
    LOCAL_ENDPOINT,
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

test("packaged release uses the injected local endpoint without a public fallback", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHODESK_BASE_URL: LOCAL_ENDPOINT,
    ECHO_PUBLIC_BACKEND_BASE: "https://stale.example.test",
  });

  assert.equal(runtime.runtimeMode, "release");
  assert.equal(runtime.mode, "local");
  assert.equal(runtime.principalMode, "local");
  assert.equal(runtime.role, "local_dev_diagnostic");
  assert.equal(runtime.source, "injected-runtime-endpoint");
  assert.equal(runtime.backendBase, LOCAL_ENDPOINT);
  assert.equal(runtime.spawnBackend, true);
  assert.equal(runtime.localBase, LOCAL_ENDPOINT);
  assert.equal(runtime.localDevDiagnosticEndpoint, LOCAL_ENDPOINT);
  assert.equal(runtime.localHost, "127.0.0.1");
  assert.equal(runtime.port, 19345);
  assert.equal(runtime.bindHost, "127.0.0.1");
});

test("packaged release keeps remote service as an explicit opt-in", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHO_PRINCIPAL_MODE: "public",
  });

  assert.equal(runtime.runtimeMode, "release");
  assert.equal(runtime.mode, "public");
  assert.equal(runtime.role, "public_service");
  assert.equal(runtime.source, "explicit-principal-mode");
  assert.equal(runtime.backendBase, "https://echo.yoliyoli.uk");
  assert.equal(runtime.spawnBackend, false);
  assert.equal(runtime.localDevDiagnosticEndpoint, null);
});

test("unknown config versions and invalid Hub roles fail closed", () => {
  assert.throws(
    () => resolveBackendEndpoint({ ...config, schemaVersion: 4 }, {}),
    (error) => error?.code === "unknown_schema_version",
  );
  assert.throws(
    () =>
      resolveBackendEndpoint(
        {
          ...config,
          roles: {
            ...config.roles,
            pairedHubSyncGateway: { enabled: true, baseUrl: "http://hub.example.test" },
          },
        },
        {},
      ),
    (error) => error?.code === "invalid_paired_hub_role",
  );
});

test("explicit packaged Hub gateway is exposed without becoming a backend fallback", () => {
  const runtime = resolveBackendEndpoint(config, { ECHODESK_BASE_URL: LOCAL_ENDPOINT });
  assert.equal(runtime.backendBase, LOCAL_ENDPOINT);
  assert.equal(
    runtime.pairedHubSyncGatewayEndpoint,
    config.roles.pairedHubSyncGateway.baseUrl,
  );
});

test("disabled packaged Hub gateway remains unavailable to the renderer", () => {
  const runtime = resolveBackendEndpoint(
    {
      ...config,
      roles: {
        ...config.roles,
        pairedHubSyncGateway: { enabled: false, baseUrl: null },
      },
    },
    { ECHODESK_BASE_URL: LOCAL_ENDPOINT },
  );
  assert.equal(runtime.pairedHubSyncGatewayEndpoint, null);
  assert.equal(runtime.backendBase, LOCAL_ENDPOINT);
});

test("explicit local mode remains the packaged bundled-worker contract", () => {
  const runtime = resolveBackendEndpoint(config, {
    ECHO_PRINCIPAL_MODE: "local",
    ECHODESK_BASE_URL: LOCAL_ENDPOINT,
  });

  assert.equal(runtime.mode, "local");
  assert.equal(runtime.role, "local_dev_diagnostic");
  assert.equal(runtime.source, "injected-runtime-endpoint");
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

test("missing or invalid injected endpoint fails closed", () => {
  assert.throws(
    () =>
      resolveBackendEndpoint(config, {
        ECHO_RUNTIME_MODE: "development",
      }),
    (error) => error?.code === "runtime_endpoint_missing",
  );
  assert.throws(
    () => resolveBackendEndpoint(config, localEnv({ ECHODESK_BASE_URL: "https://public.example.test" })),
    (error) => error?.code === "invalid_local_dev_endpoint",
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
        localEnv(),
      ),
    (error) => error?.code === "invalid_lan_bind_host",
  );
});

test("preload publishes the main-process backend host before renderer startup", () => {
  const mainSource = readFileSync(
    path.resolve(__dirname, "../main.cjs"),
    "utf8",
  );
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
          schemaVersion: 3,
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
  assert.match(
    mainSource,
    /pairedHubSyncGatewayEndpoint: BACKEND_ENDPOINT\.pairedHubSyncGatewayEndpoint/,
  );
  assert.equal(exposedBridge.setBackendHost, undefined);
  assert.equal(exposedBridge.setBackendRouting, undefined);
  assert.deepEqual(syncChannels, [
    "echo:is-public-demo",
    "echo:backend-host-sync",
    "echo:backend-routing-sync",
  ]);
});
