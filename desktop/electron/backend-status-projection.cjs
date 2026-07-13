"use strict";

const RENDERER_BACKEND_STATES = new Set([
  "starting",
  "ready",
  "restarting",
  "degraded",
  "python-not-found",
  "backend-source-not-found",
  "bundled-backend-unavailable",
  "shutting-down",
  "external",
  "unknown",
]);

const SAFE_REASON_TEXT = Object.freeze({
  "backend-health-failed": "backend health check failed",
  "backend-process-exited": "backend process exited unexpectedly",
  "backend-spawn-failed": "backend process failed to start",
  "backend-unavailable": "backend service is unavailable",
  "external-backend-unhealthy": "external backend is unhealthy",
});

function stableReasonCode(payload) {
  const reason = String(payload?.reason || "");
  if (reason === "external backend unhealthy") return "external-backend-unhealthy";
  if (/^spawn\b/i.test(reason)) return "backend-spawn-failed";
  if (/^child exited\b/i.test(reason)) return "backend-process-exited";
  if (/healthz|startup timeout/i.test(reason)) return "backend-health-failed";
  if (reason) return "backend-unavailable";
  return null;
}

function safeInteger(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum
    ? value
    : undefined;
}

function projectBackendStatusForRenderer(payload) {
  const rawState = String(payload?.state || "unknown");
  const state = RENDERER_BACKEND_STATES.has(rawState) ? rawState : "unknown";
  const projected = { state };

  const port = safeInteger(payload?.port, 1, 65_535);
  const attempt = safeInteger(payload?.attempt, 0, 100);
  const attempts = safeInteger(payload?.attempts, 0, 100);
  const backoffMs = safeInteger(payload?.backoff_ms, 0, 24 * 60 * 60 * 1000);
  if (port !== undefined) projected.port = port;
  if (attempt !== undefined) projected.attempt = attempt;
  if (attempts !== undefined) projected.attempts = attempts;
  if (backoffMs !== undefined) projected.backoff_ms = backoffMs;
  if (payload?.mode === "public-demo" || payload?.mode === "external") {
    projected.mode = payload.mode;
  }
  if (payload?.help_url === "docs/INSTALL.md") {
    projected.help_url = payload.help_url;
  }

  const reasonCode = stableReasonCode(payload);
  if (reasonCode) {
    projected.reason_code = reasonCode;
    projected.reason = SAFE_REASON_TEXT[reasonCode];
  }
  return projected;
}

module.exports = {
  projectBackendStatusForRenderer,
};
