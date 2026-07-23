/* eslint-disable @typescript-eslint/no-var-requires */

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);
const RUNTIME_MODES = new Set(["release", "development", "diagnostic"]);
const PRINCIPAL_MODES = new Set(["local", "public"]);

class BackendEndpointError extends Error {
  constructor(code, message) {
    super(`[backend-endpoint:${code}] ${message}`);
    this.name = "BackendEndpointError";
    this.code = code;
    this.reason = code;
  }
}

function fail(code, message) {
  throw new BackendEndpointError(code, message);
}

function isLoopbackHost(raw) {
  const value = String(raw ?? "").trim().toLowerCase();
  return LOOPBACK_HOSTS.has(value);
}

function isPrivateHttpHostname(raw) {
  const hostname = String(raw ?? "")
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, "");
  if (hostname === "localhost" || hostname.endsWith(".localhost")) return true;
  if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(hostname)) {
    const parts = hostname.split(".").map((part) => Number(part));
    if (parts.some((part) => part < 0 || part > 255)) return false;
    const [a, b] = parts;
    return (
      a === 10 ||
      a === 127 ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 169 && b === 254)
    );
  }
  if (hostname === "::1") return true;
  const mapped = hostname.match(/^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i);
  if (mapped) return isPrivateHttpHostname(mapped[1]);
  const firstHextet = Number.parseInt(hostname.split(":", 1)[0], 16);
  if (!Number.isFinite(firstHextet)) return false;
  return (firstHextet & 0xfe00) === 0xfc00 || (firstHextet & 0xffc0) === 0xfe80;
}

function normalizeHttpBase(raw, options = {}) {
  const value = String(raw ?? "").trim();
  if (!value) return null;
  const withScheme = /^[a-z][a-z\d+.-]*:\/\//i.test(value)
    ? value
    : `http://${value}`;
  let parsed;
  try {
    parsed = new URL(withScheme);
  } catch {
    fail(options.code || "invalid_endpoint", `${options.label || "endpoint"} is invalid`);
  }
  if (
    (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash
  ) {
    fail(options.code || "invalid_endpoint", `${options.label || "endpoint"} must be an origin`);
  }
  if (options.requireHttps === true && parsed.protocol !== "https:") {
    fail(options.code || "invalid_endpoint", `${options.label || "endpoint"} must use HTTPS`);
  }
  if (
    parsed.protocol === "http:" &&
    options.allowPrivateHttp !== false &&
    !isPrivateHttpHostname(parsed.hostname)
  ) {
    fail(
      options.code || "invalid_endpoint",
      `${options.label || "endpoint"} HTTP is restricted to private hosts`,
    );
  }
  return parsed.origin;
}

function normalizePublicServiceBase(raw) {
  const value = normalizeHttpBase(raw, {
    code: "invalid_public_service_endpoint",
    label: "public service endpoint",
    requireHttps: true,
    allowPrivateHttp: false,
  });
  if (!value) fail("empty_public_service_endpoint", "public service endpoint is empty");
  return value;
}

function normalizeLocalDevBase(raw) {
  const value = normalizeHttpBase(raw, {
    code: "invalid_local_dev_endpoint",
    label: "local dev/diagnostic endpoint",
  });
  if (!value || !value.startsWith("http://")) {
    fail("invalid_local_dev_endpoint", "local dev/diagnostic endpoint must use private HTTP");
  }
  return value;
}

function formatHost(raw) {
  const value = String(raw ?? "").trim();
  if (!value) fail("empty_local_host", "local dev/diagnostic host is empty");
  if (value.includes(":") && !value.startsWith("[")) return `[${value}]`;
  return value;
}

function parsePort(raw, fallback) {
  const value = String(raw ?? fallback ?? "").trim();
  if (!/^\d+$/.test(value)) fail("invalid_port", `invalid port: ${value || "<empty>"}`);
  const port = Number(value);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) {
    fail("invalid_port", `port out of range: ${value}`);
  }
  return port;
}

function requireString(raw, name, code = "empty_config_value") {
  const value = String(raw ?? "").trim();
  if (!value) fail(code, `${name} must not be empty`);
  return value;
}

function isV1Config(config) {
  return (
    config.schemaVersion === undefined &&
    config.local &&
    config.public &&
    config.lanShare
  );
}

function canonicalRoles(config) {
  if (config.schemaVersion === 2) {
    if (!config.roles || typeof config.roles !== "object") {
      fail("invalid_schema", "schemaVersion 2 requires roles");
    }
    const roles = config.roles;
    const publicService = roles.publicService;
    const localDevDiagnostic = roles.localDevDiagnostic;
    const hub = roles.pairedHubSyncGateway;
    if (!publicService || typeof publicService !== "object") {
      fail("invalid_schema", "roles.publicService is required");
    }
    if (!localDevDiagnostic || typeof localDevDiagnostic !== "object") {
      fail("invalid_schema", "roles.localDevDiagnostic is required");
    }
    if (
      !hub ||
      typeof hub !== "object" ||
      hub.enabled !== false ||
      hub.baseUrl !== null
    ) {
      fail(
        "invalid_paired_hub_role",
        "paired Hub sync gateway must remain disabled with a null endpoint",
      );
    }
    return {
      schemaVersion: 2,
      publicBase: publicService.baseUrl,
      localHost: localDevDiagnostic.host,
      localPort: localDevDiagnostic.port,
      hubBase: null,
      legacy: false,
    };
  }
  if (config.schemaVersion === 1 || isV1Config(config)) {
    return {
      schemaVersion: 1,
      publicBase: config.public?.baseUrl,
      localHost: config.local?.host,
      localPort: config.local?.port,
      hubBase: null,
      legacy: true,
    };
  }
  fail("unknown_schema_version", "backend.config.json schemaVersion is unsupported");
}

function runtimeModeFor(env, options = {}) {
  const explicit = String(env.ECHO_RUNTIME_MODE ?? "").trim().toLowerCase();
  const mode = explicit || (options.isDevelopment === true ? "development" : "release");
  if (!RUNTIME_MODES.has(mode)) {
    fail("unknown_runtime_mode", `unsupported runtime mode: ${mode}`);
  }
  return mode;
}

function principalModeFor(runtimeMode, env) {
  const explicit = String(env.ECHO_PRINCIPAL_MODE ?? "").trim().toLowerCase();
  if (explicit === "paired_hub_sync_gateway") {
    fail("hub_sync_gateway_not_supported", "paired Hub sync gateway is not a business endpoint");
  }
  if (explicit && !PRINCIPAL_MODES.has(explicit)) {
    fail("invalid_principal_mode", `unsupported principal mode: ${explicit}`);
  }
  const legacyLocal = env.ECHO_FORCE_LOCAL_BACKEND === "1";
  const legacyPublic = env.ECHO_PUBLIC_DEMO === "1";
  if (runtimeMode !== "release" && legacyLocal && legacyPublic) {
    fail("conflicting_principal_mode", "legacy local and public switches conflict");
  }
  if (runtimeMode === "release") {
    // A release build is a public client unless an operator explicitly selects
    // the offline/local runtime.  Never make a public transport failure select
    // a bundled backend: that would change the principal, capture, STT and
    // worker authority without user intent.
    if (explicit) return { mode: explicit, source: "explicit-principal-mode" };
    return { mode: "public", source: "release-default-public" };
  }
  if (explicit) return { mode: explicit, source: "explicit-principal-mode" };
  if (legacyLocal) return { mode: "local", source: "legacy-force-local" };
  if (legacyPublic) return { mode: "public", source: "legacy-public-demo" };
  return {
    mode: "local",
    source: runtimeMode === "diagnostic" ? "diagnostic-default-local" : "development-default-local",
  };
}

function localEndpointFor(roles, runtimeMode, env) {
  if (runtimeMode !== "release" && env.ECHO_LOCAL_DEV_DIAGNOSTIC_BASE) {
    return normalizeLocalDevBase(env.ECHO_LOCAL_DEV_DIAGNOSTIC_BASE);
  }
  const localHost = requireString(roles.localHost, "localDevDiagnostic.host", "invalid_local_dev_endpoint");
  if (!isPrivateHttpHostname(localHost)) {
    fail("invalid_local_dev_endpoint", "local dev/diagnostic host must be private");
  }
  const port = parsePort(
    runtimeMode === "release" ? undefined : env.ECHO_BACKEND_PORT,
    roles.localPort,
  );
  return `http://${formatHost(localHost)}:${port}`;
}

function resolveBackendEndpoint(config, env = process.env, options = {}) {
  if (!config || typeof config !== "object") {
    fail("invalid_config", "backend.config.json must be an object");
  }
  const runtimeMode = runtimeModeFor(env, options);
  const roles = canonicalRoles(config);
  const principal = principalModeFor(runtimeMode, env);
  if (env.ECHO_PAIRED_HUB_SYNC_GATEWAY_BASE) {
    fail("hub_sync_gateway_not_supported", "paired Hub sync gateway is not implemented");
  }

  const publicBase = normalizePublicServiceBase(
    runtimeMode === "release" ? roles.publicBase : env.ECHO_PUBLIC_BACKEND_BASE || roles.publicBase,
  );
  const isPublic = principal.mode === "public";
  // Do not even derive a loopback endpoint for a public release.  Keeping it
  // absent makes accidental fallback impossible for backend/STT/LLM/worker
  // traffic, all of which are routed through backendBase.
  const localBase = isPublic ? null : localEndpointFor(roles, runtimeMode, env);
  const parsedLocal = localBase ? new URL(localBase) : null;
  const localHost = parsedLocal
    ? parsedLocal.hostname.replace(/^\[|\]$/g, "")
    : null;
  const port = parsedLocal ? Number(parsedLocal.port || 80) : null;
  const lanShareEnabled = !isPublic && runtimeMode !== "release" && config.lanShare?.enabled === true;
  const configuredLanBindHost = !isPublic
    ? requireString(config.lanShare?.bindHost, "lanShare.bindHost")
    : null;
  if (lanShareEnabled && isLoopbackHost(configuredLanBindHost)) {
    fail("invalid_lan_bind_host", "enabled LAN sharing requires a non-loopback bind host");
  }
  const defaultBindHost = !isPublic
    ? (lanShareEnabled ? configuredLanBindHost : localHost)
    : null;
  const bindHost = !isPublic
    ? requireString(
      runtimeMode === "release"
        ? localHost
        : env.ECHO_BACKEND_BIND_HOST || defaultBindHost,
      "effective bind host",
    )
    : null;
  const bindScope = bindHost ? (isLoopbackHost(bindHost) ? "local" : "lan") : null;
  const endpointSource = isPublic
    ? runtimeMode === "release"
      ? principal.source
      : env.ECHO_PUBLIC_BACKEND_BASE
        ? "explicit-public-endpoint"
        : principal.source
    : runtimeMode === "release"
      ? principal.source
      : env.ECHO_LOCAL_DEV_DIAGNOSTIC_BASE || env.ECHO_BACKEND_PORT
      ? "explicit-local-endpoint"
      : principal.source;

  return Object.freeze({
    mode: isPublic ? "public" : "local",
    principalMode: principal.mode,
    runtimeMode,
    role: isPublic ? "public_service" : "local_dev_diagnostic",
    source: endpointSource,
    schemaVersion: roles.schemaVersion,
    port,
    localHost,
    localBase,
    publicBase,
    publicServiceEndpoint: publicBase,
    pairedHubSyncGatewayEndpoint: null,
    localDevDiagnosticEndpoint: isPublic ? null : localBase,
    backendBase: isPublic ? publicBase : localBase,
    bindHost,
    bindScope,
    spawnBackend: !isPublic && env.ECHO_SPAWN_BACKEND !== "0",
  });
}

function resolveShareBackendBase(runtime, options = {}) {
  if (runtime.role === "public_service") return runtime.publicServiceEndpoint;
  const configured = normalizeHttpBase(options.shareBaseUrl, {
    code: "invalid_share_endpoint",
    label: "share endpoint",
  });
  if (configured) return configured;
  if (
    runtime.bindScope !== "lan" ||
    runtime.spawnBackend !== true ||
    options.allowLan === false
  ) {
    return runtime.localDevDiagnosticEndpoint;
  }

  const lanAddress = requireString(options.lanAddress, "LAN address");
  if (isLoopbackHost(lanAddress)) return runtime.localDevDiagnosticEndpoint;
  return `http://${formatHost(lanAddress)}:${runtime.port}`;
}

module.exports = {
  BackendEndpointError,
  isLoopbackHost,
  normalizeHttpBase,
  normalizeLocalDevBase,
  normalizePublicServiceBase,
  parsePort,
  resolveBackendEndpoint,
  resolveShareBackendBase,
};
