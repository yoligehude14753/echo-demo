"use strict";

const CLIENT_VERSION_HEADER = "X-EchoDesk-Client-Version";
const MINIMUM_CLIENT_VERSION_HEADER = "X-EchoDesk-Minimum-Client-Version";
const MAX_WORKSPACE_RESPONSE_BYTES = 1024 * 1024;
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

class WorkspaceBackendTransportError extends Error {
  constructor(message, code, { status = null, minimumVersion = null, cause } = {}) {
    super(message, { cause });
    this.name = "WorkspaceBackendTransportError";
    this.code = code;
    this.status = status;
    this.minimumVersion = minimumVersion;
  }
}

function normalizedPublicOrigin(raw, label = "backend") {
  let parsed;
  try {
    parsed = new URL(String(raw || ""));
  } catch (cause) {
    throw new WorkspaceBackendTransportError(
      `${label} origin is invalid`,
      "WORKSPACE_BACKEND_ORIGIN_INVALID",
      { cause },
    );
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash
  ) {
    throw new WorkspaceBackendTransportError(
      `${label} must be a credential-free HTTPS origin`,
      "WORKSPACE_BACKEND_ORIGIN_INVALID",
    );
  }
  return parsed.origin;
}

function minimumVersionFrom(response) {
  const value = response?.headers
    ?.get?.(MINIMUM_CLIENT_VERSION_HEADER)
    ?.trim();
  return value && value.length <= 64 ? value : null;
}

function upgradeRequired(response) {
  const minimumVersion = minimumVersionFrom(response);
  return new WorkspaceBackendTransportError(
    `CLIENT_UPGRADE_REQUIRED minimum=${minimumVersion || "unknown"}`,
    "CLIENT_UPGRADE_REQUIRED",
    { status: 426, minimumVersion },
  );
}

function redirectForbidden(response) {
  return new WorkspaceBackendTransportError(
    "workspace backend redirects are forbidden",
    "WORKSPACE_BACKEND_REDIRECT_FORBIDDEN",
    { status: response.status },
  );
}

function isRedirectResponse(response) {
  return (
    response?.type === "opaqueredirect" ||
    REDIRECT_STATUSES.has(response.status)
  );
}

function throwIfAborted(signal) {
  if (!signal.aborted) return;
  if (signal.reason instanceof Error) throw signal.reason;
  throw new DOMException("workspace request cancelled", "AbortError");
}

function awaitWithAbort(operation, signal) {
  throwIfAborted(signal);
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", abort);
      callback(value);
    };
    const abort = () => {
      try {
        throwIfAborted(signal);
      } catch (error) {
        finish(reject, error);
      }
    };
    signal.addEventListener("abort", abort, { once: true });
    let pending;
    try {
      pending = operation();
    } catch (error) {
      finish(reject, error);
      return;
    }
    Promise.resolve(pending).then(
      (value) => finish(resolve, value),
      (error) => finish(reject, error),
    );
  });
}

async function discardResponseBody(response) {
  try {
    await response.body?.cancel();
  } catch {
    // 调用方已经有权威 HTTP / abort 结果；清理异常 body 只能尽力而为，
    // 不能覆盖真正的请求结果。
  }
}

async function assertResponseOrigin(response, expectedOrigin) {
  if (!response.url) return;
  let responseOrigin = null;
  try {
    responseOrigin = new URL(response.url).origin;
  } catch {
    // An invalid response URL cannot prove that the bearer stayed on the
    // transport-bound origin, so it is rejected like any other mismatch.
  }
  if (responseOrigin === expectedOrigin) return;
  await discardResponseBody(response);
  throw new WorkspaceBackendTransportError(
    "workspace backend response origin changed unexpectedly",
    "WORKSPACE_BACKEND_ORIGIN_MISMATCH",
    { status: response.status },
  );
}

function responseBodyError(response, message, code) {
  return new WorkspaceBackendTransportError(message, code, {
    status: response.status,
  });
}

function declaredResponseLength(response) {
  const raw = response.headers?.get?.("Content-Length")?.trim();
  if (!raw) return null;
  if (!/^\d+$/.test(raw)) {
    throw responseBodyError(
      response,
      "workspace backend returned an invalid Content-Length",
      "WORKSPACE_BACKEND_RESPONSE_INVALID",
    );
  }
  const length = Number(raw);
  if (!Number.isSafeInteger(length)) {
    throw responseBodyError(
      response,
      "workspace backend response exceeds the safe byte range",
      "WORKSPACE_BACKEND_RESPONSE_TOO_LARGE",
    );
  }
  return length;
}

function responseTooLarge(response, receivedBytes) {
  return responseBodyError(
    response,
    `workspace backend response exceeds ${MAX_WORKSPACE_RESPONSE_BYTES} bytes` +
      (receivedBytes === null ? "" : ` (received ${receivedBytes})`),
    "WORKSPACE_BACKEND_RESPONSE_TOO_LARGE",
  );
}

async function bufferedResponse(response, signal) {
  throwIfAborted(signal);
  let declaredBytes;
  try {
    declaredBytes = declaredResponseLength(response);
  } catch (error) {
    await discardResponseBody(response);
    throw error;
  }
  if (declaredBytes !== null && declaredBytes > MAX_WORKSPACE_RESPONSE_BYTES) {
    const error = responseTooLarge(response, declaredBytes);
    await discardResponseBody(response);
    throw error;
  }

  const chunks = [];
  let receivedBytes = 0;
  const reader = response.body?.getReader?.();
  if (reader) {
    let completed = false;
    try {
      while (true) {
        const { done, value } = await awaitWithAbort(() => reader.read(), signal);
        if (done) {
          completed = true;
          break;
        }
        const chunk =
          value instanceof Uint8Array ? value : new Uint8Array(value || 0);
        receivedBytes += chunk.byteLength;
        if (receivedBytes > MAX_WORKSPACE_RESPONSE_BYTES) {
          const error = responseTooLarge(response, receivedBytes);
          void reader.cancel(error).catch(() => undefined);
          throw error;
        }
        chunks.push(chunk);
      }
    } catch (error) {
      if (!completed) void reader.cancel(error).catch(() => undefined);
      throw error;
    } finally {
      if (completed) reader.releaseLock();
    }
  }

  const body = new Uint8Array(receivedBytes);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  throwIfAborted(signal);
  const nullBodyStatus =
    response.status === 204 || response.status === 205 || response.status === 304;
  return new Response(nullBodyStatus ? null : body, {
    status: response.status,
    statusText: response.statusText,
    headers: new Headers(response.headers),
  });
}

async function readWorkspaceJsonResponse(response) {
  if (!response?.ok) {
    await discardResponseBody(response || {}).catch(() => undefined);
    throw new WorkspaceBackendTransportError(
      `workspace backend HTTP ${Number(response?.status) || 0}`,
      "WORKSPACE_BACKEND_HTTP_ERROR",
      { status: Number(response?.status) || null },
    );
  }
  let text;
  try {
    text = await response.text();
  } catch {
    throw new WorkspaceBackendTransportError(
      "workspace backend response could not be read",
      "WORKSPACE_BACKEND_RESPONSE_INVALID",
      { status: response.status },
    );
  }
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    // JSON.parse can include a fragment of the server body in SyntaxError.message.
    // Replace it with a stable error before it crosses main/renderer IPC.
    throw new WorkspaceBackendTransportError(
      "workspace backend returned invalid JSON",
      "WORKSPACE_BACKEND_RESPONSE_INVALID",
      { status: response.status },
    );
  }
}

function createWorkspaceBackendTransport({
  backendBase,
  vault,
  ensureSession,
  renewSession,
  clientVersion,
  fetchImpl = globalThis.fetch,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
}) {
  if (
    !vault ||
    typeof ensureSession !== "function" ||
    typeof renewSession !== "function" ||
    typeof fetchImpl !== "function"
  ) {
    throw new TypeError("workspace backend transport dependencies are required");
  }
  const backendOrigin = normalizedPublicOrigin(backendBase, "workspace backend");
  const vaultOrigin = normalizedPublicOrigin(
    vault.backendOrigin,
    "workspace credential vault",
  );
  if (vaultOrigin !== backendOrigin) {
    throw new WorkspaceBackendTransportError(
      "workspace backend and credential vault origins differ",
      "WORKSPACE_BACKEND_VAULT_MISMATCH",
    );
  }
  const declaredVersion = String(clientVersion || "").trim();
  if (!declaredVersion) {
    throw new TypeError("workspace client version is required");
  }
  let terminalUpgradeError = null;

  function assertExpectedOrigin(rawExpectedOrigin) {
    const expectedOrigin = normalizedPublicOrigin(
      rawExpectedOrigin,
      "renderer expected backend",
    );
    if (expectedOrigin !== backendOrigin || expectedOrigin !== vaultOrigin) {
      throw new WorkspaceBackendTransportError(
        "renderer backend origin no longer matches the workspace identity vault",
        "WORKSPACE_BACKEND_ORIGIN_MISMATCH",
      );
    }
    return expectedOrigin;
  }

  function tokenForSession(session, expectedOrigin) {
    if (!session || typeof session !== "object") {
      throw new WorkspaceBackendTransportError(
        "workspace session is unavailable",
        "WORKSPACE_SESSION_MISSING",
      );
    }
    const sessionOrigin = normalizedPublicOrigin(
      session.backend_origin,
      "workspace session backend",
    );
    if (sessionOrigin !== expectedOrigin) {
      throw new WorkspaceBackendTransportError(
        "workspace session belongs to a different backend origin",
        "WORKSPACE_SESSION_ORIGIN_MISMATCH",
      );
    }
    const token = typeof session?.token === "string" ? session.token.trim() : "";
    if (!token) {
      throw new WorkspaceBackendTransportError(
        "workspace session returned no access token",
        "WORKSPACE_SESSION_MISSING",
      );
    }
    return token;
  }

  function requestInit(init, token, signal) {
    const headers = new Headers(init?.headers);
    headers.set(CLIENT_VERSION_HEADER, declaredVersion);
    headers.set("Authorization", `Bearer ${token}`);
    // 如果 fetch 能通过 307/308 重放本机文件 body，上面的精确 origin 检查就会失效。
    // 工作区传输永不跟随重定向，即使调用方传入了其它 redirect 策略。
    return { ...init, headers, signal, redirect: "error" };
  }

  async function request({
    expectedOrigin: rawExpectedOrigin,
    pathname,
    init = {},
    timeoutMs = 30_000,
  }) {
    const expectedOrigin = assertExpectedOrigin(rawExpectedOrigin);
    if (terminalUpgradeError) throw terminalUpgradeError;
    if (typeof pathname !== "string" || !pathname.startsWith("/")) {
      throw new WorkspaceBackendTransportError(
        "workspace backend path must be absolute",
        "WORKSPACE_BACKEND_PATH_INVALID",
      );
    }
    const target = new URL(pathname, expectedOrigin);
    if (target.origin !== expectedOrigin) {
      throw new WorkspaceBackendTransportError(
        "workspace backend path escaped the expected origin",
        "WORKSPACE_BACKEND_PATH_INVALID",
      );
    }

    const controller = new AbortController();
    const externalSignal = init.signal;
    const abortFromCaller = () => controller.abort(externalSignal?.reason);
    if (externalSignal?.aborted) abortFromCaller();
    else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
    const timer = setTimer(
      () => controller.abort(new DOMException("workspace request timed out", "TimeoutError")),
      Math.max(1, timeoutMs),
    );

    try {
      const session = await awaitWithAbort(ensureSession, controller.signal);
      const token = tokenForSession(session, expectedOrigin);
      let response = await fetchImpl(
        target,
        requestInit(init, token, controller.signal),
      );
      if (isRedirectResponse(response)) {
        const error = redirectForbidden(response);
        await discardResponseBody(response);
        throw error;
      }
      await assertResponseOrigin(response, expectedOrigin);
      if (response.status === 426) {
        terminalUpgradeError = upgradeRequired(response);
        await discardResponseBody(response);
        throw terminalUpgradeError;
      }
      if (response.status !== 401) {
        return await bufferedResponse(response, controller.signal);
      }

      await discardResponseBody(response);
      throwIfAborted(controller.signal);
      const renewed = await awaitWithAbort(renewSession, controller.signal);
      const renewedToken = tokenForSession(renewed, expectedOrigin);
      response = await fetchImpl(
        target,
        requestInit(init, renewedToken, controller.signal),
      );
      if (isRedirectResponse(response)) {
        const error = redirectForbidden(response);
        await discardResponseBody(response);
        throw error;
      }
      await assertResponseOrigin(response, expectedOrigin);
      if (response.status === 426) {
        terminalUpgradeError = upgradeRequired(response);
        await discardResponseBody(response);
        throw terminalUpgradeError;
      }
      return await bufferedResponse(response, controller.signal);
    } finally {
      clearTimer(timer);
      externalSignal?.removeEventListener("abort", abortFromCaller);
    }
  }

  return {
    backendOrigin,
    assertExpectedOrigin,
    request,
  };
}

module.exports = {
  CLIENT_VERSION_HEADER,
  MAX_WORKSPACE_RESPONSE_BYTES,
  MINIMUM_CLIENT_VERSION_HEADER,
  WorkspaceBackendTransportError,
  createWorkspaceBackendTransport,
  normalizedPublicOrigin,
  readWorkspaceJsonResponse,
};
