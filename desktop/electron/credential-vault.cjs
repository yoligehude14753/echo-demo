"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ENVELOPE_SCHEMA = 2;
const MIN_CREDENTIAL_LENGTH = 20;
const MAX_CREDENTIAL_LENGTH = 512;
const MAX_ENCRYPTED_BYTES = 16 * 1024;

function normalizedHttpsOrigin(raw) {
  const parsed = new URL(String(raw || ""));
  if (parsed.protocol !== "https:" || parsed.username || parsed.password) {
    throw new Error("public credential backend must be a credential-free HTTPS origin");
  }
  return parsed.origin;
}

function isCredential(value) {
  return (
    typeof value === "string" &&
    value.length >= MIN_CREDENTIAL_LENGTH &&
    value.length <= MAX_CREDENTIAL_LENGTH
  );
}

function fsyncDirectory(directory) {
  let fd;
  try {
    fd = fs.openSync(directory, "r");
    fs.fsyncSync(fd);
  } catch {
    // Windows and some sandboxed filesystems do not allow directory fsync.
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function atomicWrite(target, data) {
  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
  const temporary = `${target}.tmp-${process.pid}-${Date.now()}`;
  let fd;
  try {
    fd = fs.openSync(temporary, "wx", 0o600);
    fs.writeFileSync(fd, data);
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    fs.renameSync(temporary, target);
    fs.chmodSync(target, 0o600);
    fsyncDirectory(path.dirname(target));
  } catch (error) {
    if (fd !== undefined) fs.closeSync(fd);
    try {
      fs.unlinkSync(temporary);
    } catch {
      // Best-effort cleanup; the next write uses a unique temporary name.
    }
    throw error;
  }
}

function createCredentialVault({
  safeStorage,
  target,
  backendBase,
  officialBackendBase,
  enabled,
  platform = process.platform,
  logger = () => {},
}) {
  const backendOrigin = normalizedHttpsOrigin(backendBase);
  const officialOrigin = normalizedHttpsOrigin(officialBackendBase);

  function selectedLinuxBackendIsSecure() {
    if (platform !== "linux") return true;
    if (typeof safeStorage.getSelectedStorageBackend !== "function") {
      logger("Linux safeStorage backend is unknown; refusing credential access");
      return false;
    }
    try {
      const backend = safeStorage.getSelectedStorageBackend();
      const secure =
        backend === "gnome_libsecret" ||
        (typeof backend === "string" && /^kwallet\d*$/.test(backend));
      if (!secure) {
        logger(`Linux safeStorage backend ${String(backend || "unknown")} is not secure`);
      }
      return secure;
    } catch (error) {
      logger(
        `Linux safeStorage backend unavailable: ${error?.message ?? String(error)}`,
      );
      return false;
    }
  }

  function available() {
    if (enabled !== true) return false;
    try {
      return (
        safeStorage.isEncryptionAvailable() === true &&
        selectedLinuxBackendIsSecure()
      );
    } catch (error) {
      logger(`safeStorage unavailable: ${error?.message ?? String(error)}`);
      return false;
    }
  }

  function clear() {
    try {
      fs.unlinkSync(target);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }

  function writeState({
    credential,
    enrollmentId = null,
    pendingCredential = null,
    enrollmentConfirmed = true,
  }) {
    if (!available()) return false;
    if (!isCredential(credential)) throw new Error("invalid public device credential");
    if (enrollmentId !== null && !isCredential(enrollmentId)) {
      throw new Error("invalid public enrollment id");
    }
    if (pendingCredential !== null && !isCredential(pendingCredential)) {
      throw new Error("invalid pending public device credential");
    }
    if (typeof enrollmentConfirmed !== "boolean") {
      throw new Error("invalid enrollment confirmation state");
    }
    const envelope = JSON.stringify({
      schema: ENVELOPE_SCHEMA,
      backendOrigin,
      credential,
      enrollmentId,
      pendingCredential,
      enrollmentConfirmed,
    });
    atomicWrite(target, safeStorage.encryptString(envelope));
    return true;
  }

  function store(credential, enrollmentId = null) {
    return writeState({ credential, enrollmentId, enrollmentConfirmed: true });
  }

  function parseEnvelope(plaintext) {
    try {
      const envelope = JSON.parse(plaintext);
      if (
        (envelope?.schema !== 1 && envelope?.schema !== ENVELOPE_SCHEMA) ||
        envelope?.backendOrigin !== backendOrigin ||
        !isCredential(envelope?.credential)
      ) {
        return null;
      }
      const pendingCredential = isCredential(envelope.pendingCredential)
        ? envelope.pendingCredential
        : null;
      return {
        credential: envelope.credential,
        enrollmentId: isCredential(envelope.enrollmentId) ? envelope.enrollmentId : null,
        pendingCredential,
        enrollmentConfirmed:
          envelope.schema === 1 ? true : envelope.enrollmentConfirmed === true,
      };
    } catch {
      return null;
    }
  }

  function readState() {
    if (!available()) return null;
    try {
      const encrypted = fs.readFileSync(target);
      if (encrypted.length === 0 || encrypted.length > MAX_ENCRYPTED_BYTES) return null;
      const plaintext = safeStorage.decryptString(encrypted);
      const identity = parseEnvelope(plaintext);
      if (identity) return identity;

      // One-time migration is deliberately limited to the official service.
      if (backendOrigin === officialOrigin && isCredential(plaintext)) {
        store(plaintext);
        return {
          credential: plaintext,
          enrollmentId: null,
          pendingCredential: null,
          enrollmentConfirmed: true,
        };
      }
      logger("origin-bound credential rejected legacy or mismatched envelope");
      return null;
    } catch (error) {
      if (error?.code !== "ENOENT") {
        logger(`encrypted credential unavailable: ${error?.message ?? error}`);
      }
      return null;
    }
  }

  function readIdentity() {
    const state = readState();
    if (!state) return null;
    return {
      credential: state.credential,
      enrollmentId: state.enrollmentId,
    };
  }

  function beginEnrollment(credential, enrollmentId) {
    if (!available()) return false;
    const state = readState();
    if (state) {
      if (state.credential === credential && state.enrollmentId === enrollmentId) {
        return true;
      }
      throw new Error("a different public enrollment identity already exists");
    }
    if (exists()) throw new Error("stored public enrollment identity is unreadable");
    return writeState({
      credential,
      enrollmentId,
      pendingCredential: null,
      enrollmentConfirmed: false,
    });
  }

  function confirmEnrollment(credential, enrollmentId) {
    const state = readState();
    if (
      !state ||
      state.credential !== credential ||
      state.enrollmentId !== enrollmentId
    ) {
      throw new Error("pending enrollment identity mismatch");
    }
    if (state.enrollmentConfirmed) return true;
    return writeState({ ...state, enrollmentConfirmed: true });
  }

  function beginRotation(nextCredential) {
    if (!isCredential(nextCredential)) {
      throw new Error("invalid pending public device credential");
    }
    const state = readState();
    if (!state || !state.enrollmentConfirmed) {
      throw new Error("no confirmed device credential available for rotation");
    }
    if (state.pendingCredential) return state.pendingCredential;
    if (!writeState({ ...state, pendingCredential: nextCredential })) {
      throw new Error("encrypted credential storage is unavailable");
    }
    return nextCredential;
  }

  function commitRotation(expectedPendingCredential) {
    const state = readState();
    if (!state || state.pendingCredential !== expectedPendingCredential) {
      throw new Error("pending credential mismatch");
    }
    return writeState({
      credential: state.pendingCredential,
      enrollmentId: state.enrollmentId,
      pendingCredential: null,
      enrollmentConfirmed: true,
    });
  }

  function abortRotation(expectedPendingCredential) {
    const state = readState();
    if (!state || state.pendingCredential !== expectedPendingCredential) {
      throw new Error("pending credential mismatch");
    }
    return writeState({ ...state, pendingCredential: null });
  }

  function exists() {
    try {
      return fs.statSync(target).isFile();
    } catch {
      return false;
    }
  }

  return {
    abortRotation,
    backendOrigin,
    beginEnrollment,
    beginRotation,
    clear,
    commitRotation,
    confirmEnrollment,
    exists,
    isAvailable: available,
    readIdentity,
    readRotationState: readState,
    read: () => readIdentity()?.credential ?? null,
    store,
  };
}

module.exports = {
  ENVELOPE_SCHEMA,
  atomicWrite,
  createCredentialVault,
  normalizedHttpsOrigin,
};
