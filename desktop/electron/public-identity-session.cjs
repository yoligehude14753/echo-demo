"use strict";

const MAX_IDENTITY_RESPONSE_BYTES = 64 * 1024;

function throwIfRequestAborted(signal) {
  if (!signal?.aborted) return;
  if (signal.reason instanceof Error) throw signal.reason;
  throw new DOMException("identity request cancelled", "AbortError");
}

function awaitWithRequestAbort(operation, signal) {
  throwIfRequestAborted(signal);
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
        throwIfRequestAborted(signal);
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

async function cancelResponseBody(response, reason) {
  try {
    await response.body?.cancel?.(reason);
  } catch {
    // The authoritative redirect/origin/body error must not be hidden by
    // best-effort response cleanup.
  }
}

async function boundedIdentityResponse(response, signal, maxResponseBytes) {
  const rawLength = response.headers?.get?.("Content-Length")?.trim();
  if (rawLength) {
    if (!/^\d+$/.test(rawLength) || Number(rawLength) > maxResponseBytes) {
      await cancelResponseBody(response);
      const error = new Error("identity response body exceeds the safe limit");
      error.code = "IDENTITY_RESPONSE_TOO_LARGE";
      throw error;
    }
  }

  const chunks = [];
  let received = 0;
  const reader = response.body?.getReader?.();
  if (reader) {
    let completed = false;
    try {
      while (true) {
        const { done, value } = await awaitWithRequestAbort(
          () => reader.read(),
          signal,
        );
        if (done) {
          completed = true;
          break;
        }
        const chunk = value instanceof Uint8Array ? value : new Uint8Array(value || 0);
        received += chunk.byteLength;
        if (received > maxResponseBytes) {
          const error = new Error("identity response body exceeds the safe limit");
          error.code = "IDENTITY_RESPONSE_TOO_LARGE";
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

  const bytes = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  throwIfRequestAborted(signal);
  const text = new TextDecoder().decode(bytes);
  let parsed = {};
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch (cause) {
      if (response.ok) {
        const error = new Error("identity backend returned invalid JSON", { cause });
        error.code = "IDENTITY_RESPONSE_INVALID";
        throw error;
      }
    }
  }
  throwIfRequestAborted(signal);
  return {
    ok: response.ok,
    status: response.status,
    headers: response.headers,
    url: response.url,
    async json() {
      return parsed;
    },
  };
}

async function backendBoundJsonFetch({
  backendOrigin,
  pathname,
  method = "GET",
  headers = {},
  body = undefined,
  signal = undefined,
  timeoutMs = 8_000,
  maxResponseBytes = MAX_IDENTITY_RESPONSE_BYTES,
  fetchImpl = globalThis.fetch,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
}) {
  const parsedBackend = new URL(backendOrigin);
  if (
    parsedBackend.protocol !== "https:" ||
    parsedBackend.username ||
    parsedBackend.password ||
    parsedBackend.pathname !== "/" ||
    parsedBackend.search ||
    parsedBackend.hash
  ) {
    const error = new Error("identity backend must be a credential-free HTTPS origin");
    error.code = "IDENTITY_BACKEND_ORIGIN_INVALID";
    throw error;
  }
  const expectedOrigin = parsedBackend.origin;
  const target = new URL(pathname, `${expectedOrigin}/`);
  if (target.origin !== expectedOrigin) {
    const error = new Error("backend-bound request cannot change origin");
    error.code = "IDENTITY_BACKEND_ORIGIN_MISMATCH";
    throw error;
  }
  if (typeof fetchImpl !== "function") {
    throw new TypeError("backend-bound request requires fetch");
  }
  if (!Number.isSafeInteger(maxResponseBytes) || maxResponseBytes <= 0) {
    throw new TypeError("identity response byte limit must be positive");
  }

  const controller = new AbortController();
  const forwardAbort = () =>
    controller.abort(
      signal?.reason || new DOMException("identity request cancelled", "AbortError"),
    );
  if (signal?.aborted) forwardAbort();
  else signal?.addEventListener("abort", forwardAbort, { once: true });
  const timer = setTimer(
    () =>
      controller.abort(
        new DOMException("identity request timed out", "TimeoutError"),
      ),
    timeoutMs,
  );
  try {
    const response = await fetchImpl(target, {
      method,
      headers,
      body,
      signal: controller.signal,
      redirect: "error",
    });
    if (response.status >= 300 && response.status < 400) {
      await cancelResponseBody(response);
      const error = new Error("backend-bound redirects are forbidden");
      error.code = "IDENTITY_BACKEND_REDIRECT_FORBIDDEN";
      error.status = response.status;
      throw error;
    }
    if (response.url && new URL(response.url).origin !== expectedOrigin) {
      await cancelResponseBody(response);
      const error = new Error("backend response origin changed unexpectedly");
      error.code = "IDENTITY_BACKEND_ORIGIN_MISMATCH";
      throw error;
    }
    return await boundedIdentityResponse(
      response,
      controller.signal,
      maxResponseBytes,
    );
  } finally {
    clearTimer(timer);
    signal?.removeEventListener("abort", forwardAbort);
  }
}

class PublicIdentitySessionError extends Error {
  constructor(
    message,
    code,
    { status = null, minimumVersion = null, cause } = {},
  ) {
    super(message, { cause });
    this.name = "PublicIdentitySessionError";
    this.code = code;
    this.status = status;
    this.minimumVersion = minimumVersion;
  }
}

function identityLost(message = "device identity is no longer valid") {
  return new PublicIdentitySessionError(message, "IDENTITY_LOST", { status: 401 });
}

function responseError(operation, response) {
  const minimumVersion =
    response.headers?.get?.("X-EchoDesk-Minimum-Client-Version")?.trim() || null;
  if (response.status === 426) {
    return new PublicIdentitySessionError(
      `CLIENT_UPGRADE_REQUIRED minimum=${minimumVersion || "unknown"}; ${operation} failed (426)`,
      "CLIENT_UPGRADE_REQUIRED",
      { status: response.status, minimumVersion },
    );
  }
  const code =
    response.status === 401 || response.status === 409
      ? "IDENTITY_LOST"
      : "IDENTITY_REQUEST_FAILED";
  return new PublicIdentitySessionError(
    `${operation} failed (${response.status})`,
    code,
    { status: response.status },
  );
}

const DEFINITIVE_ROTATION_REJECTION_STATUSES = new Set([400, 413, 415, 422]);
const IDENTITY_ROTATION_REJECTION_STATUSES = new Set([401, 409]);

function classifyRotationResponse(response) {
  if (response?.ok === true) return "success";
  const status = Number(response?.status);
  if (IDENTITY_ROTATION_REJECTION_STATUSES.has(status)) return "identity-lost";
  if (DEFINITIVE_ROTATION_REJECTION_STATUSES.has(status)) return "definitive-rejection";
  return "ambiguous";
}

async function sessionDto(response, operation) {
  if (!response.ok) throw responseError(operation, response);
  const body = await response.json();
  if (typeof body?.token !== "string" || body.token.length === 0) {
    throw new PublicIdentitySessionError(
      `${operation} returned no access token`,
      "IDENTITY_RESPONSE_INVALID",
    );
  }
  return {
    token: body.token,
    expires_at: body.expires_at ?? null,
    principal: body.principal ?? undefined,
    credential_expires_at: body.credential_expires_at ?? null,
  };
}

function requireStoredState(vault) {
  if (typeof vault.isAvailable === "function" && !vault.isAvailable()) {
    throw new PublicIdentitySessionError(
      "encrypted credential storage is unavailable",
      "IDENTITY_STORE_UNAVAILABLE",
    );
  }
  const state = vault.readRotationState();
  if (state) return state;
  if (vault.exists()) {
    throw new PublicIdentitySessionError(
      "stored device identity is unreadable for this backend",
      "IDENTITY_STORE_INVALID",
    );
  }
  return null;
}

function requirePersisted(result) {
  if (!result) {
    throw new PublicIdentitySessionError(
      "encrypted credential storage is unavailable",
      "IDENTITY_STORE_UNAVAILABLE",
    );
  }
}

function createPublicIdentitySessionManager({
  vault,
  request,
  newSecret,
  displayName,
}) {
  if (!vault || typeof request !== "function" || typeof newSecret !== "function") {
    throw new TypeError("public identity session manager dependencies are required");
  }
  let operationTail = Promise.resolve();

  function serialize(operation) {
    const running = operationTail.then(operation, operation);
    operationTail = running.then(
      () => undefined,
      () => undefined,
    );
    return running;
  }

  async function enroll(state) {
    const response = await request("/session/enroll", {
      enrollment_id: state.enrollmentId,
      device_secret: state.credential,
      display_name: String(displayName || "EchoDesk").slice(0, 120),
    });
    const session = await sessionDto(response, "enroll");
    requirePersisted(vault.confirmEnrollment(state.credential, state.enrollmentId));
    return session;
  }

  async function renewCredential(credential) {
    const response = await request("/session/renew", {
      device_credential: credential,
    });
    if (response.status === 401) return { kind: "invalid", session: null };
    if (!response.ok) throw responseError("renew", response);
    return { kind: "valid", session: await sessionDto(response, "renew") };
  }

  async function reconcileRotation(state) {
    const pendingCredential = state.pendingCredential;
    if (!pendingCredential) throw new TypeError("pending credential is required");

    let pendingResult = null;
    let pendingError = null;
    try {
      pendingResult = await renewCredential(pendingCredential);
    } catch (error) {
      pendingError = error;
    }
    if (pendingResult?.kind === "valid") {
      requirePersisted(vault.commitRotation(pendingCredential));
      return pendingResult.session;
    }

    let currentResult;
    try {
      currentResult = await renewCredential(state.credential);
    } catch (currentError) {
      throw pendingError || currentError;
    }
    if (currentResult.kind === "valid") {
      requirePersisted(vault.abortRotation(pendingCredential));
      return currentResult.session;
    }
    if (pendingResult?.kind === "invalid") return null;
    throw pendingError || identityLost("credential rotation could not be reconciled");
  }

  async function renew() {
    const state = requireStoredState(vault);
    if (!state) return null;
    if (!state.enrollmentConfirmed) return enroll(state);
    if (state.pendingCredential) return reconcileRotation(state);
    const result = await renewCredential(state.credential);
    return result.session;
  }

  async function ensure() {
    const existing = requireStoredState(vault);
    if (existing) return renew();

    const enrollmentId = newSecret();
    const credential = newSecret();
    requirePersisted(vault.beginEnrollment(credential, enrollmentId));
    const pending = requireStoredState(vault);
    if (!pending) {
      throw new PublicIdentitySessionError(
        "pending enrollment was not persisted",
        "IDENTITY_STORE_INVALID",
      );
    }
    return enroll(pending);
  }

  async function rotate(sessionToken) {
    const state = requireStoredState(vault);
    if (!state || !state.enrollmentConfirmed) {
      throw identityLost("no confirmed device identity is available for rotation");
    }
    if (state.pendingCredential) {
      throw new PublicIdentitySessionError(
        "a credential rotation is awaiting reconciliation",
        "ROTATION_PENDING",
      );
    }

    const pendingCredential = vault.beginRotation(newSecret());
    let response;
    try {
      response = await request(
        "/session/credential/rotate",
        {
          current_device_credential: state.credential,
          new_device_credential: pendingCredential,
        },
        { token: sessionToken },
      );
    } catch (error) {
      // The server may have committed before the response was lost.  Keep the
      // encrypted pending credential so startup reconciliation can prove which
      // credential is authoritative.
      throw error;
    }
    const outcome = classifyRotationResponse(response);
    if (outcome !== "success") {
      if (outcome === "definitive-rejection") {
        requirePersisted(vault.abortRotation(pendingCredential));
      }
      throw responseError("credential rotation", response);
    }

    const body = await response.json();
    requirePersisted(vault.commitRotation(pendingCredential));
    return {
      credential_id: body.credential_id ?? null,
      credential_expires_at: body.credential_expires_at ?? null,
    };
  }

  return {
    ensure: () => serialize(ensure),
    renew: () => serialize(renew),
    rotate: (sessionToken) => serialize(() => rotate(sessionToken)),
  };
}

/**
 * Public desktop sessions deliberately have no durable client identity. The
 * enrollment material and bearer are closure-local, so a renderer reload or
 * Electron restart always performs a fresh public bootstrap.
 *
 * The durable manager above remains for future explicitly provisioned secrets.
 */
function createEphemeralPublicSessionManager({ request, newSecret, displayName }) {
  if (typeof request !== "function" || typeof newSecret !== "function") {
    throw new TypeError("ephemeral public session dependencies are required");
  }

  let identity = null;
  let operationTail = Promise.resolve();

  function serialize(operation) {
    const running = operationTail.then(operation, operation);
    operationTail = running.then(
      () => undefined,
      () => undefined,
    );
    return running;
  }

  async function bootstrap() {
    const nextIdentity = {
      enrollmentId: newSecret(),
      credential: newSecret(),
    };
    const response = await request("/session/enroll", {
      enrollment_id: nextIdentity.enrollmentId,
      device_secret: nextIdentity.credential,
      display_name: String(displayName || "EchoDesk").slice(0, 120),
    });
    const session = await sessionDto(response, "enroll");
    identity = nextIdentity;
    return session;
  }

  async function renew() {
    if (!identity) return bootstrap();
    const response = await request("/session/renew", {
      device_credential: identity.credential,
    });
    if (response.status === 401 || response.status === 409) {
      identity = null;
      return bootstrap();
    }
    if (!response.ok) throw responseError("renew", response);
    return sessionDto(response, "renew");
  }

  return {
    ensure: () => serialize(renew),
    renew: () => serialize(renew),
    reset: () => serialize(() => {
      identity = null;
    }),
  };
}

module.exports = {
  backendBoundJsonFetch,
  classifyRotationResponse,
  MAX_IDENTITY_RESPONSE_BYTES,
  PublicIdentitySessionError,
  createEphemeralPublicSessionManager,
  createPublicIdentitySessionManager,
};
