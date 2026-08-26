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

function resolveViteBackendTarget(env = process.env) {
  if (!Object.prototype.hasOwnProperty.call(env, "ECHODESK_BASE_URL")) {
    throw new Error("ECHODESK_BASE_URL must be injected by the backend lifecycle owner");
  }
  return normalizeTarget(env.ECHODESK_BASE_URL, "ECHODESK_BASE_URL");
}

function websocketTarget(target) {
  return normalizeTarget(target, "backend target").replace(/^http:/, "ws:").replace(/^https:/, "wss:");
}

module.exports = { resolveViteBackendTarget, websocketTarget };
