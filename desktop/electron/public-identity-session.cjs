"use strict";

class PublicIdentitySessionError extends Error {
  constructor(message, code, { status = null, cause } = {}) {
    super(message, { cause });
    this.name = "PublicIdentitySessionError";
    this.code = code;
    this.status = status;
  }
}

function identityLost(message = "device identity is no longer valid") {
  return new PublicIdentitySessionError(message, "IDENTITY_LOST", { status: 401 });
}

function responseError(operation, response) {
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

module.exports = {
  classifyRotationResponse,
  PublicIdentitySessionError,
  createPublicIdentitySessionManager,
};
