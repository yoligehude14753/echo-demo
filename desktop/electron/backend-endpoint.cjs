/* eslint-disable @typescript-eslint/no-var-requires */

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

function normalizeHttpBase(raw) {
  const value = String(raw ?? "").trim().replace(/\/+$/, "");
  if (!value) return null;
  return /^https?:\/\//i.test(value) ? value : `http://${value}`;
}

function isLoopbackHost(raw) {
  const value = String(raw ?? "").trim().toLowerCase();
  return LOOPBACK_HOSTS.has(value);
}

function formatHost(raw) {
  const value = String(raw ?? "").trim();
  if (!value) throw new Error("[backend-endpoint] host must not be empty");
  if (value.includes(":") && !value.startsWith("[")) return `[${value}]`;
  return value;
}

function parsePort(raw, fallback) {
  const value = String(raw ?? fallback ?? "").trim();
  if (!/^\d+$/.test(value)) {
    throw new Error(`[backend-endpoint] invalid port: ${value || "<empty>"}`);
  }
  const port = Number(value);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`[backend-endpoint] port out of range: ${value}`);
  }
  return port;
}

function requireString(raw, name) {
  const value = String(raw ?? "").trim();
  if (!value) throw new Error(`[backend-endpoint] ${name} must not be empty`);
  return value;
}

function resolveBackendEndpoint(config, env = process.env) {
  if (!config || typeof config !== "object") {
    throw new Error("[backend-endpoint] backend.config.json must be an object");
  }
  const localHost = requireString(config.local?.host, "local.host");
  const port = parsePort(env.ECHO_BACKEND_PORT, config.local?.port);
  const publicBase = normalizeHttpBase(
    env.ECHO_PUBLIC_BACKEND_BASE || config.public?.baseUrl,
  );
  if (!publicBase) {
    throw new Error("[backend-endpoint] public.baseUrl must not be empty");
  }

  const lanShareEnabled = config.lanShare?.enabled === true;
  const lanBindHost = requireString(
    config.lanShare?.bindHost,
    "lanShare.bindHost",
  );
  if (lanShareEnabled && isLoopbackHost(lanBindHost)) {
    throw new Error(
      "[backend-endpoint] enabled LAN sharing requires a non-loopback bind host",
    );
  }
  const defaultBindHost = lanShareEnabled ? lanBindHost : localHost;
  const bindHost = requireString(
    env.ECHO_BACKEND_BIND_HOST || defaultBindHost,
    "effective bind host",
  );

  const forceLocal = env.ECHO_FORCE_LOCAL_BACKEND === "1";
  const publicDemo = env.ECHO_PUBLIC_DEMO === "1" && !forceLocal;
  const localBase = `http://${formatHost(localHost)}:${port}`;
  const bindScope = isLoopbackHost(bindHost) ? "local" : "lan";

  return Object.freeze({
    mode: publicDemo ? "public" : "local",
    port,
    localHost,
    localBase,
    publicBase,
    backendBase: publicDemo ? publicBase : localBase,
    bindHost,
    bindScope,
    spawnBackend: !publicDemo && env.ECHO_SPAWN_BACKEND !== "0",
  });
}

function resolveShareBackendBase(runtime, options = {}) {
  const configured = normalizeHttpBase(options.shareBaseUrl);
  if (configured) return configured;
  if (runtime.mode === "public") return runtime.publicBase;
  if (
    runtime.bindScope !== "lan" ||
    runtime.spawnBackend !== true ||
    options.allowLan === false
  ) {
    return runtime.localBase;
  }

  const lanAddress = requireString(options.lanAddress, "LAN address");
  if (isLoopbackHost(lanAddress)) return runtime.localBase;
  return `http://${formatHost(lanAddress)}:${runtime.port}`;
}

module.exports = {
  isLoopbackHost,
  normalizeHttpBase,
  parsePort,
  resolveBackendEndpoint,
  resolveShareBackendBase,
};
