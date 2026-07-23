"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createCredentialVault } = require("../credential-vault.cjs");
const {
  backendBoundJsonFetch,
  classifyRotationResponse,
  createEphemeralPublicSessionManager,
  createPublicIdentitySessionManager,
  MAX_IDENTITY_RESPONSE_BYTES,
} = require("../public-identity-session.cjs");

const official = "https://echo.yoliyoli.uk";
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

function response(status, body = {}, responseHeaders = {}) {
  const normalizedHeaders = Object.fromEntries(
    Object.entries(responseHeaders).map(([name, value]) => [
      name.toLowerCase(),
      String(value),
    ]),
  );
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get(name) {
        return normalizedHeaders[String(name).toLowerCase()] ?? null;
      },
    },
    async json() {
      return body;
    },
  };
}

test("426 preserves the required client version and never confirms enrollment", async () => {
  const { root, vault } = fixture();
  try {
    const manager = createPublicIdentitySessionManager({
      vault,
      newSecret: (() => {
        const values = ["e".repeat(48), "c".repeat(48)];
        return () => values.shift();
      })(),
      displayName: "old-client",
      request: async () =>
        response(
          426,
          { error: { code: "client_upgrade_required" } },
          { "X-EchoDesk-Minimum-Client-Version": "0.4.0" },
        ),
    });

    await assert.rejects(manager.ensure(), (error) => {
      assert.equal(error.code, "CLIENT_UPGRADE_REQUIRED");
      assert.equal(error.status, 426);
      assert.equal(error.minimumVersion, "0.4.0");
      assert.match(error.message, /CLIENT_UPGRADE_REQUIRED minimum=0\.4\.0/);
      return true;
    });
    assert.equal(vault.readRotationState().enrollmentConfirmed, false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Electron identity requests always declare the packaged app version", () => {
  const main = fs.readFileSync(path.resolve(__dirname, "../main.cjs"), "utf8");
  assert.match(
    main,
    /"X-EchoDesk-Client-Version": app\.getVersion\(\)/,
  );
  assert.match(main, /backendBoundJsonFetch\(\{/);
  assert.doesNotMatch(main, /fetch\(new URL\(pathname/);
  assert.match(main, /displayName: "EchoDesk Desktop"/);
  assert.doesNotMatch(main, /displayName: os\.hostname\(\)/);
});

test("backend-bound bootstrap fetch refuses a 307 without contacting its second origin", async () => {
  const secondOrigin = "https://redirected.example";
  const calls = [];
  await assert.rejects(
    backendBoundJsonFetch({
      backendOrigin: official,
      pathname: "/bootstrap",
      fetchImpl: async (url, init) => {
        calls.push({ url: url.toString(), redirect: init.redirect });
        return new Response(null, {
          status: 307,
          headers: { Location: `${secondOrigin}/bootstrap` },
        });
      },
    }),
    (error) => {
      assert.equal(error.code, "IDENTITY_BACKEND_REDIRECT_FORBIDDEN");
      assert.equal(error.status, 307);
      return true;
    },
  );
  assert.deepEqual(calls, [
    { url: `${official}/bootstrap`, redirect: "error" },
  ]);
});

test("backend-bound identity requests reject non-origin URLs before fetch", async () => {
  for (const backendOrigin of [
    "http://echo.yoliyoli.uk",
    "https://user@echo.yoliyoli.uk",
    "https://echo.yoliyoli.uk/private",
    "https://echo.yoliyoli.uk/?query=1",
    "https://echo.yoliyoli.uk/#fragment",
  ]) {
    let fetched = 0;
    await assert.rejects(
      backendBoundJsonFetch({
        backendOrigin,
        pathname: "/bootstrap",
        fetchImpl: async () => {
          fetched += 1;
          return new Response("{}", { status: 200 });
        },
      }),
      (error) => error.code === "IDENTITY_BACKEND_ORIGIN_INVALID",
    );
    assert.equal(fetched, 0);
  }
});

test("backend-bound identity requests reject a successful response URL from another origin", async () => {
  await assert.rejects(
    backendBoundJsonFetch({
      backendOrigin: official,
      pathname: "/bootstrap",
      fetchImpl: async () => {
        const result = new Response("{}", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
        Object.defineProperty(result, "url", {
          value: "https://redirected.example/bootstrap",
        });
        return result;
      },
    }),
    (error) => error.code === "IDENTITY_BACKEND_ORIGIN_MISMATCH",
  );
});

test("identity response cap rejects declared and chunked oversize bodies", async () => {
  let declaredCancelled = 0;
  await assert.rejects(
    backendBoundJsonFetch({
      backendOrigin: official,
      pathname: "/bootstrap",
      fetchImpl: async () =>
        new Response(
          new ReadableStream({
            cancel() {
              declaredCancelled += 1;
            },
          }),
          {
            status: 200,
            headers: {
              "Content-Length": String(MAX_IDENTITY_RESPONSE_BYTES + 1),
            },
          },
        ),
    }),
    (error) => error.code === "IDENTITY_RESPONSE_TOO_LARGE",
  );
  assert.equal(declaredCancelled, 1);

  const chunkSize = Math.floor(MAX_IDENTITY_RESPONSE_BYTES / 2) + 1;
  let chunkedCancelled = 0;
  await assert.rejects(
    backendBoundJsonFetch({
      backendOrigin: official,
      pathname: "/bootstrap",
      fetchImpl: async () =>
        new Response(
          new ReadableStream({
            pull(controller) {
              controller.enqueue(new Uint8Array(chunkSize));
            },
            cancel() {
              chunkedCancelled += 1;
            },
          }),
          { status: 200 },
        ),
    }),
    (error) => error.code === "IDENTITY_RESPONSE_TOO_LARGE",
  );
  await Promise.resolve();
  assert.equal(chunkedCancelled, 1);
});

test("identity request timeout remains armed through a stalled response body", async () => {
  let bodyAborted = false;
  await assert.rejects(
    backendBoundJsonFetch({
      backendOrigin: official,
      pathname: "/session/renew",
      method: "POST",
      body: "{}",
      timeoutMs: 20,
      fetchImpl: async (_url, init) => {
        const body = new ReadableStream({
          start(controller) {
            const abort = () => {
              bodyAborted = true;
              controller.error(init.signal.reason);
            };
            if (init.signal.aborted) abort();
            else init.signal.addEventListener("abort", abort, { once: true });
          },
        });
        return new Response(body, {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    }),
    (error) => error?.name === "TimeoutError",
  );
  assert.equal(bodyAborted, true);
});

function session(token = "token-" + "t".repeat(40)) {
  return {
    token,
    expires_at: "2030-01-01T00:00:00Z",
    principal: { tenant_id: "tenant", owner_id: "owner", device_id: "device" },
  };
}

test("ephemeral public identity keeps bearer material out of vaults and reboots via bootstrap", async () => {
  const requests = [];
  const createManager = (prefix) => {
    let sequence = 0;
    return createEphemeralPublicSessionManager({
      newSecret: () => `${prefix}-${++sequence}`.padEnd(48, prefix),
      displayName: "test-device",
      request: async (pathname, body) => {
        requests.push({ pathname, body });
        return response(201, session(`token-${prefix}-${requests.length}`));
      },
    });
  };

  const firstProcess = createManager("first");
  const firstSession = await firstProcess.ensure();
  assert.equal(firstSession.token, "token-first-1");
  assert.equal(requests[0].pathname, "/session/enroll");
  assert.ok(requests[0].body.enrollment_id.startsWith("first-"));
  assert.ok(requests[0].body.device_secret.startsWith("first-"));

  // A fresh manager represents a process restart: no prior identity is read,
  // and a new bootstrap is sent without any vault or filesystem dependency.
  const restartedProcess = createManager("restart");
  const restartedSession = await restartedProcess.ensure();
  assert.equal(restartedSession.token, "token-restart-2");
  assert.equal(requests[1].pathname, "/session/enroll");
  assert.ok(requests[1].body.enrollment_id.startsWith("restart-"));
  assert.ok(requests[1].body.device_secret.startsWith("restart-"));

  await restartedProcess.reset();
  await restartedProcess.ensure();
  assert.equal(requests[2].pathname, "/session/enroll");
  assert.ok(requests[2].body.enrollment_id.startsWith("restart-"));
});

test("ephemeral public identity reboots after a rejected renewal and otherwise fails closed", async () => {
  const requests = [];
  let secret = 0;
  const manager = createEphemeralPublicSessionManager({
    newSecret: () => `ephemeral-${++secret}`.padEnd(48, "e"),
    request: async (pathname) => {
      requests.push(pathname);
      if (pathname === "/session/renew") return response(401);
      return response(201, session(`token-${requests.length}`));
    },
  });

  await manager.ensure();
  const renewed = await manager.renew();
  assert.equal(renewed.token, "token-3");
  assert.deepEqual(requests, ["/session/enroll", "/session/renew", "/session/enroll"]);

  const failing = createEphemeralPublicSessionManager({
    newSecret: () => "f".repeat(48),
    request: async () => response(503),
  });
  await assert.rejects(failing.ensure(), (error) => {
    assert.equal(error.code, "IDENTITY_REQUEST_FAILED");
    return true;
  });
});

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
