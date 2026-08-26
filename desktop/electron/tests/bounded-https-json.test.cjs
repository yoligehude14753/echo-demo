"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");

const {
  fetchBoundedHttpsJson,
  requestBoundedHttpsJson,
  isGithubReleasePayload,
} = require("../bounded-https-json.cjs");

function responseGet({ status = 200, headers = {}, chunks = [], end = true } = {}) {
  return (_target, _options, callback) => {
    const request = new EventEmitter();
    request.destroy = (error) => queueMicrotask(() => request.emit("error", error));
    queueMicrotask(() => {
      const response = new EventEmitter();
      response.statusCode = status;
      response.headers = {
        "content-type": "application/json",
        ...headers,
      };
      response.resume = () => {
        response.resumed = true;
      };
      response.destroy = (error) => {
        response.destroyed = true;
        queueMicrotask(() => response.emit("error", error));
      };
      callback(response);
      for (const chunk of chunks) response.emit("data", Buffer.from(chunk));
      if (end) response.emit("end");
    });
    return request;
  };
}

test("bounded HTTPS JSON accepts a valid release payload", async () => {
  const payload = {
    tag_name: "v0.3.1",
    name: "EchoDesk 0.3.1",
    html_url: "https://github.com/yoligehude14753/echo-demo/releases/tag/v0.3.1",
    assets: [
      {
        name: "EchoDesk-0.3.1-arm64.dmg",
        size: 42,
        browser_download_url:
          "https://github.com/yoligehude14753/echo-demo/releases/download/v0.3.1/EchoDesk.dmg",
      },
    ],
  };
  const body = JSON.stringify(payload);
  const actual = await fetchBoundedHttpsJson("https://api.github.com/release", {
    getImpl: responseGet({
      headers: { "content-length": String(Buffer.byteLength(body)) },
      chunks: [body],
    }),
    validate: isGithubReleasePayload,
  });
  assert.deepEqual(actual, payload);
});

test("bounded HTTPS JSON rejects redirects, invalid shapes and declared overflow", async () => {
  await assert.rejects(
    fetchBoundedHttpsJson("https://api.github.com/release", {
      getImpl: responseGet({ status: 302, headers: { location: "https://evil.test" } }),
    }),
    (error) => error.code === "HTTPS_JSON_REDIRECT_FORBIDDEN",
  );
  await assert.rejects(
    fetchBoundedHttpsJson("https://api.github.com/release", {
      getImpl: responseGet({ chunks: ["[]"] }),
      validate: isGithubReleasePayload,
    }),
    (error) => error.code === "HTTPS_JSON_RESPONSE_INVALID",
  );
  await assert.rejects(
    fetchBoundedHttpsJson("https://api.github.com/release", {
      maxBytes: 8,
      getImpl: responseGet({ headers: { "content-length": "9" } }),
    }),
    (error) => error.code === "HTTPS_JSON_RESPONSE_TOO_LARGE",
  );
});

test("bounded HTTPS JSON cancels chunked overflow and enforces a total body deadline", async () => {
  await assert.rejects(
    fetchBoundedHttpsJson("https://api.github.com/release", {
      maxBytes: 8,
      getImpl: responseGet({ chunks: ["12345", "67890"] }),
    }),
    (error) => error.code === "HTTPS_JSON_RESPONSE_TOO_LARGE",
  );

  await assert.rejects(
    fetchBoundedHttpsJson("https://api.github.com/release", {
      timeoutMs: 10,
      getImpl: responseGet({ chunks: ["{"], end: false }),
    }),
    (error) => error.code === "HTTPS_JSON_TIMEOUT",
  );
});

function responseRequest({ status = 200, body = "{}", delay = 0 } = {}) {
  return (_target, options, callback) => {
    const request = new EventEmitter();
    request.writes = [];
    request.write = (chunk) => request.writes.push(Buffer.from(chunk).toString());
    request.destroy = (error) => queueMicrotask(() => request.emit("error", error));
    request.end = () => setTimeout(() => {
      const response = new EventEmitter();
      response.statusCode = status;
      response.headers = { "content-type": "application/json", "content-length": String(Buffer.byteLength(body)) };
      response.resume = () => undefined;
      response.destroy = (error) => queueMicrotask(() => response.emit("error", error));
      callback(response);
      response.emit("data", Buffer.from(body));
      response.emit("end");
    }, delay);
    request.options = options;
    return request;
  };
}

test("bounded HTTPS request supports POST headers and JSON body", async () => {
  const requestImpl = responseRequest({ body: JSON.stringify({ ok: true }) });
  const response = await requestBoundedHttpsJson("https://backend.test/session/enroll", {
    method: "POST",
    headers: { authorization: "Bearer redacted", "content-type": "application/json" },
    body: JSON.stringify({ enrollment_id: "e1" }),
    requestImpl,
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
});

test("bounded HTTPS request classifies HTTP error, timeout, oversize, and abort", async () => {
  await assert.rejects(
    requestBoundedHttpsJson("https://backend.test/session", { requestImpl: responseRequest({ status: 503 }) }),
    (error) => error.code === "HTTPS_JSON_HTTP_ERROR" || error.code === "HTTPS_JSON_RESPONSE_INVALID",
  );
  await assert.rejects(
    requestBoundedHttpsJson("https://backend.test/session", { timeoutMs: 5, requestImpl: responseRequest({ delay: 30 }) }),
    (error) => error.code === "HTTPS_JSON_TIMEOUT",
  );
  await assert.rejects(
    requestBoundedHttpsJson("https://backend.test/session", { maxBytes: 2, requestImpl: responseRequest({ body: "{\"x\":1}" }) }),
    (error) => error.code === "HTTPS_JSON_RESPONSE_TOO_LARGE",
  );
  const controller = new AbortController();
  const pending = requestBoundedHttpsJson("https://backend.test/session", { signal: controller.signal, requestImpl: responseRequest({ delay: 30 }) });
  controller.abort(new Error("test abort"));
  await assert.rejects(pending, (error) => error.code === "HTTPS_JSON_ABORTED");
});
