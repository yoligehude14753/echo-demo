"use strict";

const https = require("node:https");

const DEFAULT_MAX_JSON_BYTES = 1024 * 1024;
const DEFAULT_JSON_TIMEOUT_MS = 8_000;

class BoundedHttpsJsonError extends Error {
  constructor(message, code, { status = null, cause } = {}) {
    super(message, { cause });
    this.name = "BoundedHttpsJsonError";
    this.code = code;
    this.status = status;
  }
}

function boundedJsonError(code, { status = null, cause } = {}) {
  const messages = {
    HTTPS_JSON_URL_INVALID: "HTTPS JSON URL is invalid",
    HTTPS_JSON_REDIRECT_FORBIDDEN: "HTTPS JSON redirects are forbidden",
    HTTPS_JSON_HTTP_ERROR: "HTTPS JSON request failed",
    HTTPS_JSON_CONTENT_TYPE_INVALID: "HTTPS JSON response type is invalid",
    HTTPS_JSON_RESPONSE_TOO_LARGE: "HTTPS JSON response exceeds the byte limit",
    HTTPS_JSON_RESPONSE_INVALID: "HTTPS JSON response is invalid",
    HTTPS_JSON_TIMEOUT: "HTTPS JSON request timed out",
    HTTPS_JSON_NETWORK_ERROR: "HTTPS JSON request failed",
  };
  return new BoundedHttpsJsonError(messages[code] || "HTTPS JSON request failed", code, {
    status,
    cause,
  });
}

function safeContentLength(raw, maxBytes) {
  if (raw === undefined || raw === null || raw === "") return null;
  const value = Array.isArray(raw) ? raw[0] : String(raw);
  if (!/^\d+$/.test(value)) {
    throw boundedJsonError("HTTPS_JSON_RESPONSE_INVALID");
  }
  const bytes = Number(value);
  if (!Number.isSafeInteger(bytes) || bytes > maxBytes) {
    throw boundedJsonError("HTTPS_JSON_RESPONSE_TOO_LARGE");
  }
  return bytes;
}

function fetchBoundedHttpsJson(
  rawUrl,
  {
    headers = {},
    maxBytes = DEFAULT_MAX_JSON_BYTES,
    timeoutMs = DEFAULT_JSON_TIMEOUT_MS,
    validate = () => true,
    getImpl = https.get,
    setTimer = setTimeout,
    clearTimer = clearTimeout,
  } = {},
) {
  let target;
  try {
    target = new URL(String(rawUrl || ""));
  } catch (cause) {
    return Promise.reject(
      boundedJsonError("HTTPS_JSON_URL_INVALID", { cause }),
    );
  }
  if (
    target.protocol !== "https:" ||
    target.username ||
    target.password ||
    !Number.isSafeInteger(maxBytes) ||
    maxBytes < 1 ||
    !Number.isSafeInteger(timeoutMs) ||
    timeoutMs < 1 ||
    typeof validate !== "function" ||
    typeof getImpl !== "function"
  ) {
    return Promise.reject(boundedJsonError("HTTPS_JSON_URL_INVALID"));
  }

  return new Promise((resolve, reject) => {
    let settled = false;
    let timer = null;
    let request = null;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      if (timer !== null) clearTimer(timer);
      callback(value);
    };
    const fail = (error) => finish(reject, error);

    try {
      request = getImpl(target, { headers }, (response) => {
        const status = Number(response.statusCode || 0);
        if (status >= 300 && status < 400) {
          response.resume?.();
          fail(
            boundedJsonError("HTTPS_JSON_REDIRECT_FORBIDDEN", { status }),
          );
          return;
        }
        if (status < 200 || status >= 300) {
          response.resume?.();
          fail(boundedJsonError("HTTPS_JSON_HTTP_ERROR", { status }));
          return;
        }
        const contentType = String(response.headers?.["content-type"] || "")
          .split(";", 1)[0]
          .trim()
          .toLowerCase();
        if (contentType !== "application/json") {
          response.resume?.();
          fail(
            boundedJsonError("HTTPS_JSON_CONTENT_TYPE_INVALID", { status }),
          );
          return;
        }
        try {
          safeContentLength(response.headers?.["content-length"], maxBytes);
        } catch (error) {
          response.resume?.();
          fail(error);
          return;
        }

        const chunks = [];
        let receivedBytes = 0;
        response.on("data", (rawChunk) => {
          if (settled) return;
          const chunk = Buffer.isBuffer(rawChunk)
            ? rawChunk
            : Buffer.from(rawChunk);
          receivedBytes += chunk.byteLength;
          if (receivedBytes > maxBytes) {
            const error = boundedJsonError("HTTPS_JSON_RESPONSE_TOO_LARGE", {
              status,
            });
            fail(error);
            response.destroy?.(error);
            return;
          }
          chunks.push(chunk);
        });
        response.on("error", (cause) => {
          fail(boundedJsonError("HTTPS_JSON_NETWORK_ERROR", { status, cause }));
        });
        response.on("end", () => {
          if (settled) return;
          let payload;
          try {
            payload = JSON.parse(Buffer.concat(chunks, receivedBytes).toString("utf8"));
          } catch (cause) {
            fail(
              boundedJsonError("HTTPS_JSON_RESPONSE_INVALID", { status, cause }),
            );
            return;
          }
          let valid = false;
          try {
            valid = validate(payload) === true;
          } catch {
            valid = false;
          }
          if (!valid) {
            fail(boundedJsonError("HTTPS_JSON_RESPONSE_INVALID", { status }));
            return;
          }
          finish(resolve, payload);
        });
      });
      request.on("error", (cause) => {
        fail(boundedJsonError("HTTPS_JSON_NETWORK_ERROR", { cause }));
      });
      timer = setTimer(() => {
        const error = boundedJsonError("HTTPS_JSON_TIMEOUT");
        fail(error);
        request?.destroy?.(error);
      }, timeoutMs);
      timer?.unref?.();
    } catch (cause) {
      fail(boundedJsonError("HTTPS_JSON_NETWORK_ERROR", { cause }));
    }
  });
}

function validGithubUrl(rawUrl) {
  try {
    const target = new URL(String(rawUrl || ""));
    return (
      target.protocol === "https:" &&
      target.hostname === "github.com" &&
      !target.username &&
      !target.password
    );
  } catch {
    return false;
  }
}

function isGithubReleasePayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }
  if (
    typeof payload.tag_name !== "string" ||
    !payload.tag_name.trim() ||
    payload.tag_name.length > 128 ||
    (payload.name !== null &&
      payload.name !== undefined &&
      (typeof payload.name !== "string" || payload.name.length > 300)) ||
    !validGithubUrl(payload.html_url) ||
    !Array.isArray(payload.assets) ||
    payload.assets.length > 256
  ) {
    return false;
  }
  return payload.assets.every(
    (asset) =>
      asset &&
      typeof asset === "object" &&
      typeof asset.name === "string" &&
      asset.name.length > 0 &&
      asset.name.length <= 300 &&
      Number.isSafeInteger(asset.size) &&
      asset.size >= 0 &&
      validGithubUrl(asset.browser_download_url),
  );
}

module.exports = {
  BoundedHttpsJsonError,
  DEFAULT_MAX_JSON_BYTES,
  fetchBoundedHttpsJson,
  isGithubReleasePayload,
};
