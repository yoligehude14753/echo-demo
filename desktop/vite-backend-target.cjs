"use strict";

function normalizeTarget(value, source) {
  const raw = typeof value === "string" ? value.trim() : "";
  if (!raw) throw new Error(`${source} must not be empty`);
  let parsed;
  try {
    parsed = new URL(raw);
  } catch (error) {
    throw new Error(`${source} must be an absolute HTTP(S) URL`, { cause: error });
  }
  if (!new Set(["http:", "https:"]).has(parsed.protocol)) {
    throw new Error(`${source} must use http or https`);
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error(`${source} must not contain credentials, query, or fragment`);
  }
  return parsed.toString().replace(/\/$/, "");
}

function resolveViteBackendTarget(env = process.env, defaultTarget = "http://127.0.0.1:8769") {
  if (Object.prototype.hasOwnProperty.call(env, "VITE_API_TARGET")) {
    return normalizeTarget(env.VITE_API_TARGET, "VITE_API_TARGET");
  }
  if (Object.prototype.hasOwnProperty.call(env, "ECHO_LOCAL_DEV_DIAGNOSTIC_BASE")) {
    return normalizeTarget(
      env.ECHO_LOCAL_DEV_DIAGNOSTIC_BASE,
      "ECHO_LOCAL_DEV_DIAGNOSTIC_BASE",
    );
  }
  if (Object.prototype.hasOwnProperty.call(env, "ECHO_BACKEND_PORT")) {
    const rawPort = typeof env.ECHO_BACKEND_PORT === "string" ? env.ECHO_BACKEND_PORT.trim() : "";
    if (!/^\d+$/.test(rawPort)) throw new Error("ECHO_BACKEND_PORT must be an integer");
    const port = Number(rawPort);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error("ECHO_BACKEND_PORT must be between 1 and 65535");
    }
    const host = typeof env.ECHO_BACKEND_HOST === "string" && env.ECHO_BACKEND_HOST.trim()
      ? env.ECHO_BACKEND_HOST.trim()
      : "127.0.0.1";
    return normalizeTarget(`http://${host}:${port}`, "ECHO_BACKEND_PORT");
  }
  const isolationRequested =
    env.ECHO_RUNTIME_MODE === "diagnostic" ||
    env.ECHO_RUNTIME_MODE === "development" ||
    Boolean(env.ECHO_BACKEND_CWD) ||
    Boolean(env.ECHO_PYTHON) ||
    Boolean(env.ECHO_USER_DIR);
  if (isolationRequested) {
    throw new Error(
      "isolated backend target is required; set VITE_API_TARGET, " +
        "ECHO_LOCAL_DEV_DIAGNOSTIC_BASE, or ECHO_BACKEND_PORT",
    );
  }
  return normalizeTarget(defaultTarget, "default backend target");
}

function websocketTarget(target) {
  return normalizeTarget(target, "backend target").replace(/^http:/, "ws:").replace(/^https:/, "wss:");
}

module.exports = { resolveViteBackendTarget, websocketTarget };
