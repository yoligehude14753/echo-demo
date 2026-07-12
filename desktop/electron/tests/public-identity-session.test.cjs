"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createCredentialVault } = require("../credential-vault.cjs");
const {
  classifyRotationResponse,
  createPublicIdentitySessionManager,
} = require("../public-identity-session.cjs");

const official = "https://echodesk.yoliyoli.uk";
const safeStorage = {
  isEncryptionAvailable: () => true,
  getSelectedStorageBackend: () => "gnome_libsecret",
  encryptString: (value) => Buffer.from(`encrypted:${value}`, "utf8"),
  decryptString: (value) => value.toString("utf8").replace(/^encrypted:/, ""),
};

function fixture(storage = safeStorage) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-session-manager-"));
  const target = path.join(root, "credential.bin");
  const vault = createCredentialVault({
    safeStorage: storage,
    target,
    backendBase: official,
    officialBackendBase: official,
    enabled: true,
  });
  return { root, target, vault };
}

function response(status, body = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return body;
    },
  };
}

function session(token = "token-" + "t".repeat(40)) {
  return {
    token,
    expires_at: "2030-01-01T00:00:00Z",
    principal: { tenant_id: "tenant", owner_id: "owner", device_id: "device" },
  };
}

test("lost enrollment response retries the same durable identity pair", async () => {
  const { root, vault } = fixture();
  try {
    const requests = [];
    const first = createPublicIdentitySessionManager({
      vault,
      newSecret: (() => {
        const values = ["e".repeat(48), "c".repeat(48)];
        return () => values.shift();
      })(),
      displayName: "test-device",
      request: async (pathname, body) => {
        assert.equal(vault.readRotationState().enrollmentConfirmed, false);
        requests.push({ pathname, body });
        throw new Error("response lost after commit");
      },
    });
    await assert.rejects(first.ensure(), /response lost/);
    const pending = vault.readRotationState();
    assert.equal(pending.enrollmentConfirmed, false);

    const second = createPublicIdentitySessionManager({
      vault,
      newSecret: () => {
        throw new Error("must not generate a replacement identity");
      },
      displayName: "test-device",
      request: async (pathname, body) => {
        requests.push({ pathname, body });
        return response(201, session());
      },
    });
    const restored = await second.ensure();
    assert.equal(restored.token.startsWith("token-"), true);
    assert.deepEqual(requests[1], requests[0]);
    assert.equal(vault.readRotationState().enrollmentConfirmed, true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("unavailable encrypted storage blocks enrollment before the network", async () => {
  const unavailable = { ...safeStorage, isEncryptionAvailable: () => false };
  const { root, vault } = fixture(unavailable);
  try {
    let requests = 0;
    const manager = createPublicIdentitySessionManager({
      vault,
      newSecret: () => "s".repeat(48),
      displayName: "test-device",
      request: async () => {
        requests += 1;
        return response(201, session());
      },
    });
    await assert.rejects(manager.ensure(), /encrypted credential storage is unavailable/);
    assert.equal(requests, 0);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("existing identity on an unavailable vault is never read or sent", async () => {
  let reads = 0;
  let requests = 0;
  const manager = createPublicIdentitySessionManager({
    vault: {
      isAvailable: () => false,
      readRotationState: () => {
        reads += 1;
        return { credential: "c".repeat(48), enrollmentConfirmed: true };
      },
      exists: () => true,
    },
    newSecret: () => "s".repeat(48),
    displayName: "test-device",
    request: async () => {
      requests += 1;
      return response(200, session());
    },
  });
  await assert.rejects(manager.ensure(), (error) => {
    assert.equal(error.code, "IDENTITY_STORE_UNAVAILABLE");
    return true;
  });
  assert.equal(reads, 0);
  assert.equal(requests, 0);
});

test("transient renew failure preserves the durable identity", async () => {
  const { root, vault } = fixture();
  try {
    vault.store("c".repeat(48), "e".repeat(48));
    const before = vault.readRotationState();
    const manager = createPublicIdentitySessionManager({
      vault,
      newSecret: () => "n".repeat(48),
      displayName: "test-device",
      request: async () => response(500),
    });
    await assert.rejects(manager.renew(), /renew failed \(500\)/);
    assert.deepEqual(vault.readRotationState(), before);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("rotation commit crash is reconciled from the pending credential", async () => {
  const { root, vault } = fixture();
  try {
    const current = "c".repeat(48);
    const enrollmentId = "e".repeat(48);
    const pending = "n".repeat(48);
    vault.store(current, enrollmentId);

    const originalCommit = vault.commitRotation;
    vault.commitRotation = () => {
      throw new Error("simulated fsync crash");
    };
    const rotating = createPublicIdentitySessionManager({
      vault,
      newSecret: () => pending,
      displayName: "test-device",
      request: async (pathname) => {
        assert.equal(pathname, "/session/credential/rotate");
        assert.equal(vault.readRotationState().pendingCredential, pending);
        return response(200, { credential_id: "credential-next" });
      },
    });
    await assert.rejects(rotating.rotate("access-" + "a".repeat(40)), /fsync crash/);
    assert.equal(vault.readRotationState().pendingCredential, pending);

    vault.commitRotation = originalCommit;
    const recovering = createPublicIdentitySessionManager({
      vault,
      newSecret: () => {
        throw new Error("must reuse pending credential");
      },
      displayName: "test-device",
      request: async (pathname, body) => {
        assert.equal(pathname, "/session/renew");
        assert.equal(body.device_credential, pending);
        return response(200, session("recovered-" + "r".repeat(40)));
      },
    });
    const restored = await recovering.renew();
    assert.equal(restored.token.startsWith("recovered-"), true);
    assert.equal(vault.read(), pending);
    assert.equal(vault.readRotationState().pendingCredential, null);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("rotation response classifier has an explicit cross-runtime outcome matrix", () => {
  for (const status of [400, 413, 415, 422]) {
    assert.equal(classifyRotationResponse(response(status)), "definitive-rejection");
  }
  for (const status of [401, 409]) {
    assert.equal(classifyRotationResponse(response(status)), "identity-lost");
  }
  for (const status of [403, 404, 408, 425, 429, 500, 503]) {
    assert.equal(classifyRotationResponse(response(status)), "ambiguous");
  }
  assert.equal(classifyRotationResponse(response(200)), "success");
});

test("only definitive rotation rejections abort pending credentials", async () => {
  for (const status of [400, 413, 415, 422]) {
    const { root, vault } = fixture();
    try {
      vault.store("c".repeat(48), "e".repeat(48));
      const pending = `d${status}`.padEnd(48, "d");
      const manager = createPublicIdentitySessionManager({
        vault,
        newSecret: () => pending,
        displayName: "test-device",
        request: async () => response(status),
      });
      await assert.rejects(manager.rotate("access-" + "a".repeat(40)));
      assert.equal(vault.readRotationState().pendingCredential, null);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

test("ambiguous and identity-lost rotation outcomes preserve pending for restart reconciliation", async () => {
  for (const status of [401, 409, 408, 425, 429, 500, 503]) {
    const { root, vault } = fixture();
    try {
      vault.store("c".repeat(48), "e".repeat(48));
      const pending = `p${status}`.padEnd(48, "p");
      const manager = createPublicIdentitySessionManager({
        vault,
        newSecret: () => pending,
        displayName: "test-device",
        request: async () => response(status),
      });
      await assert.rejects(
        manager.rotate("access-" + "a".repeat(40)),
        (error) => {
          if (status === 401 || status === 409) {
            assert.equal(error.code, "IDENTITY_LOST");
          }
          return true;
        },
      );
      assert.equal(vault.readRotationState().pendingCredential, pending);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

test("lost or malformed success response preserves one pending secret across restart", async () => {
  for (const failure of [
    () => {
      throw new Error("connection reset after commit");
    },
    () => ({
      ok: true,
      status: 200,
      async json() {
        throw new SyntaxError("truncated response body");
      },
    }),
  ]) {
    const { root, target, vault } = fixture();
    try {
      const pending = "n".repeat(48);
      vault.store("c".repeat(48), "e".repeat(48));
      const manager = createPublicIdentitySessionManager({
        vault,
        newSecret: () => pending,
        displayName: "test-device",
        request: async () => failure(),
      });
      await assert.rejects(manager.rotate("access-" + "a".repeat(40)));
      assert.equal(vault.readRotationState().pendingCredential, pending);

      const reopened = createCredentialVault({
        safeStorage,
        target,
        backendBase: official,
        officialBackendBase: official,
        enabled: true,
      });
      const recovering = createPublicIdentitySessionManager({
        vault: reopened,
        newSecret: () => {
          throw new Error("must not generate a third secret");
        },
        displayName: "test-device",
        request: async (_pathname, body) => {
          assert.equal(body.device_credential, pending);
          return response(200, session("recovered-" + "r".repeat(40)));
        },
      });
      assert.match((await recovering.renew()).token, /^recovered-/);
      assert.equal(reopened.read(), pending);
      assert.equal(reopened.readRotationState().pendingCredential, null);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

test("renew is serialized behind an in-flight credential rotation", async () => {
  const { root, vault } = fixture();
  try {
    const current = "c".repeat(48);
    const enrollmentId = "e".repeat(48);
    const pending = "n".repeat(48);
    vault.store(current, enrollmentId);

    let signalRotationStarted;
    const rotationStarted = new Promise((resolve) => {
      signalRotationStarted = resolve;
    });
    let releaseRotation;
    const rotationResponse = new Promise((resolve) => {
      releaseRotation = resolve;
    });
    const calls = [];
    const manager = createPublicIdentitySessionManager({
      vault,
      newSecret: () => pending,
      displayName: "test-device",
      request: async (pathname, body) => {
        calls.push({ pathname, body });
        if (pathname === "/session/credential/rotate") {
          signalRotationStarted();
          await rotationResponse;
          return response(200, { credential_id: "credential-next" });
        }
        assert.equal(pathname, "/session/renew");
        assert.equal(body.device_credential, pending);
        return response(200, session("renewed-" + "r".repeat(40)));
      },
    });

    const rotating = manager.rotate("access-" + "a".repeat(40));
    await rotationStarted;
    const renewing = manager.renew();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(calls.length, 1, "renew must not inspect pending state during rotation");

    releaseRotation();
    await rotating;
    const renewed = await renewing;
    assert.equal(renewed.token.startsWith("renewed-"), true);
    assert.equal(vault.read(), pending);
    assert.equal(vault.readRotationState().pendingCredential, null);
    assert.equal(calls.length, 2);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
