"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  createWorkspaceBackendTransport,
  MAX_WORKSPACE_RESPONSE_BYTES,
  normalizedPublicOrigin,
  readWorkspaceJsonResponse,
} = require("../workspace-backend-transport.cjs");

const ORIGIN_A = "https://workspace-a.example";
const ORIGIN_B = "https://workspace-b.example";

test("workspace JSON failures never expose backend bodies or local-path canaries", async () => {
  const canary = "/Users/alice/Private/plan.md token=super-secret";
  await assert.rejects(
    readWorkspaceJsonResponse(new Response(canary, { status: 500 })),
    (error) =>
      error.code === "WORKSPACE_BACKEND_HTTP_ERROR" &&
      !error.message.includes(canary) &&
      !error.message.includes("super-secret"),
  );
  await assert.rejects(
    readWorkspaceJsonResponse(new Response(`not-json ${canary}`, { status: 200 })),
    (error) =>
      error.code === "WORKSPACE_BACKEND_RESPONSE_INVALID" &&
      !error.message.includes("/Users/alice") &&
      !error.message.includes("super-secret"),
  );
});

test("main workspace IPC is origin-bound and has no unauthenticated/source-label bypass", () => {
  const main = readFileSync(path.resolve(__dirname, "../main.cjs"), "utf8");
  const privateStore = readFileSync(
    path.resolve(__dirname, "../private-json-store.cjs"),
    "utf8",
  );
  const preload = readFileSync(path.resolve(__dirname, "../preload.cjs"), "utf8");
  assert.match(main, /workspaceExpectedOrigin\(event, context\)/);
  assert.match(main, /workspaceBackendTransport\(\)\.request\(\{/);
  assert.match(main, /store\.doc_ids \|\| \[\]/);
  assert.match(main, /createWorkspaceFileSnapshot\(\{/);
  assert.match(main, /verifyWorkspaceRootIdentity\(\{/);
  assert.match(main, /root_identities:\s*rootIdentities/);
  assert.match(
    main,
    /verifiedRoot = await verifyWorkspaceRootIdentity[\s\S]+?await collectWorkspaceFiles\([\s\S]+?verifiedRoot\.identity/,
  );
  assert.doesNotMatch(main, /readFile\(fileInfo\.path\)/);
  assert.match(main, /atomicWritePrivateJsonFile\(storePath, payload\)/);
  assert.match(main, /readPrivateJsonFile\(storePath\)/);
  assert.match(privateStore, /fs\.constants\.O_EXCL/);
  assert.match(privateStore, /fs\.constants\.O_NOFOLLOW/);
  assert.match(privateStore, /fs\.fchmodSync\(fd, 0o600\)/);
  assert.match(privateStore, /fsyncParentDirectory\(parent\)/);
  assert.match(main, /cleanupWorkspaceSnapshotDirectory\(snapshotDirectory/);
  assert.match(main, /WORKSPACE_DURABLE_SNAPSHOT_DIRNAME = "workspace-upload-snapshots"/);
  assert.match(main, /ensurePrivateWorkspaceSnapshotRoot\(/);
  assert.match(main, /workspaceSnapshotAllowedRoots\(\)/);
  assert.match(main, /sweepWorkspaceSnapshotRoots\(protectedDirectories\)/);
  assert.match(main, /workspaceRegistryPendingSnapshotDirectories\(/);
  assert.match(main, /allowedRoots: workspaceSnapshotAllowedRoots\(\)/);
  assert.doesNotMatch(
    main,
    /rm\(snapshotDirectory,[\s\S]{0,120}catch\(\(\) => undefined\)/,
  );
  assert.match(main, /workspaceDocReferenceCount\(nextFiles, previousDocId\)/);
  assert.doesNotMatch(main, /form\.append\("source_path"/);
  assert.match(main, /workspaceRendererHandle\(/);
  assert.match(main, /shouldRetainWorkspaceFileOnScanFailure\(/);
  assert.match(main, /prepareWorkspaceUploadsForClear\(/);
  assert.match(main, /clear_requested === true/);
  assert.doesNotMatch(main, /WORKSPACE_PENDING_RECOVERY_REQUIRED/);
  assert.match(main, /assertWorkspaceOperationCurrent\(/);
  assert.doesNotMatch(main, /configured_dirs:\s*store\.workspaces/);
  assert.doesNotMatch(main, /authorized_dirs:\s*authorized/);
  assert.doesNotMatch(main, /return r\.filePaths\[0\]/);
  assert.doesNotMatch(main, /sourcePath:\s*dbPath/);
  assert.match(
    main,
    /const uploaded = await uploadWorkspaceFile[\s\S]+?workspaceProjectionAfterUpload\([\s\S]+?persist\(\);[\s\S]+?await deleteRemoteRagDoc\(expectedOrigin, previousDocId, signal\)/,
  );
  assert.match(
    main,
    /pendingUploads\[file\.path\] = \{[\s\S]+?upload_started_at: null[\s\S]+?persist\(\);[\s\S]+?upload_started_at: Date\.now\(\)[\s\S]+?persist\(\);[\s\S]+?const uploaded = await uploadWorkspaceFile/,
  );
  assert.doesNotMatch(main, /doc\?\.source !== "workspace"/);
  assert.doesNotMatch(main, /appendApiPath\("\/rag/);
  for (const channel of [
    "workspace:pick-directory",
    "workspace:local-status",
    "workspace:add-local-dir",
    "workspace:remove-local-dir",
    "workspace:scan-local",
    "workspace:clear-local-docs",
    "workspace:cancel-origin-operations",
  ]) {
    assert.match(preload, new RegExp(`${channel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\", context`));
  }
});

test("workspace transport accepts only a credential-free HTTPS origin", () => {
  assert.equal(normalizedPublicOrigin(ORIGIN_A), ORIGIN_A);
  for (const candidate of [
    "http://workspace-a.example",
    "https://user@workspace-a.example",
    "https://workspace-a.example/private",
    "https://workspace-a.example/?query=1",
    "https://workspace-a.example/#fragment",
  ]) {
    assert.throws(
      () => normalizedPublicOrigin(candidate),
      (error) => error.code === "WORKSPACE_BACKEND_ORIGIN_INVALID",
    );
  }
});

function session(token, backendOrigin = ORIGIN_A) {
  return { token, backend_origin: backendOrigin };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function pendingBodyResponse(signal, bodyStarted) {
  const body = new ReadableStream({
    start(controller) {
      const abort = () =>
        controller.error(
          signal.reason || new DOMException("workspace request cancelled", "AbortError"),
        );
      if (signal.aborted) abort();
      else signal.addEventListener("abort", abort, { once: true });
    },
    pull() {
      bodyStarted.resolve();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

test("workspace transport rejects backend/vault and renderer origin mismatches before I/O", async () => {
  assert.throws(
    () =>
      createWorkspaceBackendTransport({
        backendBase: ORIGIN_A,
        vault: { backendOrigin: ORIGIN_B },
        ensureSession: async () => session("unused"),
        renewSession: async () => session("unused"),
        clientVersion: "0.3.1",
        fetchImpl: async () => new Response(null, { status: 200 }),
      }),
    (error) => error.code === "WORKSPACE_BACKEND_VAULT_MISMATCH",
  );

  let ensured = 0;
  let fetched = 0;
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => {
      ensured += 1;
      return session("token-a");
    },
    renewSession: async () => session("token-b"),
    clientVersion: "0.3.1",
    fetchImpl: async () => {
      fetched += 1;
      return new Response(null, { status: 200 });
    },
  });
  await assert.rejects(
    transport.request({
      expectedOrigin: ORIGIN_B,
      pathname: "/rag/docs",
    }),
    (error) => error.code === "WORKSPACE_BACKEND_ORIGIN_MISMATCH",
  );
  assert.equal(ensured, 0);
  assert.equal(fetched, 0);
});

test("workspace transport authenticates the exact origin and renews once on 401", async () => {
  const requests = [];
  let renewals = 0;
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => session("initial-token"),
    renewSession: async () => {
      renewals += 1;
      return session("renewed-token");
    },
    clientVersion: "0.3.1",
    fetchImpl: async (url, init) => {
      requests.push({
        url: url.toString(),
        authorization: init.headers.get("Authorization"),
        clientVersion: init.headers.get("X-EchoDesk-Client-Version"),
        redirect: init.redirect,
      });
      return new Response(null, { status: requests.length === 1 ? 401 : 204 });
    },
  });

  const response = await transport.request({
    expectedOrigin: ORIGIN_A,
    pathname: "/rag/docs/doc-1",
    init: { method: "DELETE" },
  });
  assert.equal(response.status, 204);
  assert.equal(renewals, 1);
  assert.deepEqual(requests, [
    {
      url: `${ORIGIN_A}/rag/docs/doc-1`,
      authorization: "Bearer initial-token",
      clientVersion: "0.3.1",
      redirect: "error",
    },
    {
      url: `${ORIGIN_A}/rag/docs/doc-1`,
      authorization: "Bearer renewed-token",
      clientVersion: "0.3.1",
      redirect: "error",
    },
  ]);
});

test("workspace transport refuses every redirect before a file body can leave the bound origin", async () => {
  let requests = 0;
  let redirectPolicy = null;
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => session("upload-token"),
    renewSession: async () => session("unused"),
    clientVersion: "0.3.1",
    fetchImpl: async (_url, init) => {
      requests += 1;
      redirectPolicy = init.redirect;
      return new Response(null, {
        status: 307,
        headers: { Location: `${ORIGIN_B}/collect-workspace` },
      });
    },
  });

  await assert.rejects(
    transport.request({
      expectedOrigin: ORIGIN_A,
      pathname: "/rag/ingest",
      init: { method: "POST", body: new FormData(), redirect: "follow" },
    }),
    (error) => {
      assert.equal(error.code, "WORKSPACE_BACKEND_REDIRECT_FORBIDDEN");
      assert.equal(error.status, 307);
      return true;
    },
  );
  assert.equal(requests, 1);
  assert.equal(redirectPolicy, "error");
});

test("workspace transport rejects only redirecting HTTP statuses", async () => {
  for (const status of [301, 302, 303, 307, 308]) {
    const transport = createWorkspaceBackendTransport({
      backendBase: ORIGIN_A,
      vault: { backendOrigin: ORIGIN_A },
      ensureSession: async () => session("redirect-token"),
      renewSession: async () => session("unused"),
      clientVersion: "0.3.1",
      fetchImpl: async () =>
        new Response(null, {
          status,
          headers: { Location: `${ORIGIN_B}/collect-workspace` },
        }),
    });
    await assert.rejects(
      transport.request({ expectedOrigin: ORIGIN_A, pathname: "/rag/docs" }),
      (error) =>
        error.code === "WORKSPACE_BACKEND_REDIRECT_FORBIDDEN" &&
        error.status === status,
    );
  }

  for (const status of [204, 205, 304]) {
    const transport = createWorkspaceBackendTransport({
      backendBase: ORIGIN_A,
      vault: { backendOrigin: ORIGIN_A },
      ensureSession: async () => session("null-body-token"),
      renewSession: async () => session("unused"),
      clientVersion: "0.3.1",
      fetchImpl: async () => new Response(null, { status }),
    });
    const response = await transport.request({
      expectedOrigin: ORIGIN_A,
      pathname: "/rag/docs",
    });
    assert.equal(response.status, status);
    assert.equal(await response.text(), "");
  }
});

test("workspace transport rejects a successful response URL from another origin", async () => {
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => session("origin-bound-token"),
    renewSession: async () => session("unused"),
    clientVersion: "0.3.1",
    fetchImpl: async () => {
      const response = new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
      Object.defineProperty(response, "url", {
        value: `${ORIGIN_B}/rag/docs`,
      });
      return response;
    },
  });

  await assert.rejects(
    transport.request({
      expectedOrigin: ORIGIN_A,
      pathname: "/rag/docs",
    }),
    (error) => error.code === "WORKSPACE_BACKEND_ORIGIN_MISMATCH",
  );
});

test("workspace transport rejects a session bound to another origin before upload", async () => {
  let fetched = 0;
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => session("wrong-origin-token", ORIGIN_B),
    renewSession: async () => session("unused"),
    clientVersion: "0.3.1",
    fetchImpl: async () => {
      fetched += 1;
      return new Response(null, { status: 200 });
    },
  });
  await assert.rejects(
    transport.request({
      expectedOrigin: ORIGIN_A,
      pathname: "/rag/ingest",
      init: { method: "POST", body: new FormData() },
    }),
    (error) => error.code === "WORKSPACE_SESSION_ORIGIN_MISMATCH",
  );
  assert.equal(fetched, 0);
});

test("workspace transport latches 426 as a terminal origin state", async () => {
  let ensured = 0;
  let fetched = 0;
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => {
      ensured += 1;
      return session("old-client-token");
    },
    renewSession: async () => session("unused"),
    clientVersion: "0.3.1",
    fetchImpl: async () => {
      fetched += 1;
      return new Response(null, {
        status: 426,
        headers: { "X-EchoDesk-Minimum-Client-Version": "0.4.0" },
      });
    },
  });

  for (let attempt = 0; attempt < 2; attempt += 1) {
    await assert.rejects(
      transport.request({
        expectedOrigin: ORIGIN_A,
        pathname: "/rag/docs",
      }),
      (error) => {
        assert.equal(error.code, "CLIENT_UPGRADE_REQUIRED");
        assert.equal(error.minimumVersion, "0.4.0");
        return true;
      },
    );
  }
  assert.equal(ensured, 1);
  assert.equal(fetched, 1);
});

test("workspace transport propagates origin cancellation to the in-flight fetch", async () => {
  const caller = new AbortController();
  const fetchStarted = deferred();
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => session("upload-token"),
    renewSession: async () => session("unused"),
    clientVersion: "0.3.1",
    fetchImpl: async (_url, init) => {
      fetchStarted.resolve();
      return new Promise((_resolve, reject) => {
        const rejectAbort = () => reject(init.signal.reason);
        if (init.signal.aborted) rejectAbort();
        else init.signal.addEventListener("abort", rejectAbort, { once: true });
      });
    },
  });

  const pending = transport.request({
    expectedOrigin: ORIGIN_A,
    pathname: "/rag/ingest",
    init: { method: "POST", body: new FormData(), signal: caller.signal },
  });
  await fetchStarted.promise;
  caller.abort(new DOMException("backend origin changed", "AbortError"));
  await assert.rejects(pending, (error) => error?.name === "AbortError");
});

test("workspace transport keeps caller cancellation wired while a response body is pending", async () => {
  const caller = new AbortController();
  const bodyStarted = deferred();
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => session("upload-token"),
    renewSession: async () => session("unused"),
    clientVersion: "0.3.1",
    fetchImpl: async (_url, init) => pendingBodyResponse(init.signal, bodyStarted),
  });

  const pending = transport.request({
    expectedOrigin: ORIGIN_A,
    pathname: "/rag/ingest",
    init: { method: "POST", body: new FormData(), signal: caller.signal },
  });
  await bodyStarted.promise;
  caller.abort(new DOMException("backend origin changed", "AbortError"));
  await assert.rejects(pending, (error) => error?.name === "AbortError");
});

test("workspace transport timeout includes a response body that stalls after headers", async () => {
  const bodyStarted = deferred();
  let fireTimeout = null;
  let clearedTimer = null;
  const timerHandle = Symbol("workspace-timeout");
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => session("upload-token"),
    renewSession: async () => session("unused"),
    clientVersion: "0.3.1",
    fetchImpl: async (_url, init) => pendingBodyResponse(init.signal, bodyStarted),
    setTimer: (callback) => {
      fireTimeout = callback;
      return timerHandle;
    },
    clearTimer: (handle) => {
      clearedTimer = handle;
    },
  });

  const pending = transport.request({
    expectedOrigin: ORIGIN_A,
    pathname: "/rag/ingest",
    init: { method: "POST", body: new FormData() },
    timeoutMs: 120_000,
  });
  await bodyStarted.promise;
  assert.equal(typeof fireTimeout, "function");
  fireTimeout();
  await assert.rejects(pending, (error) => error?.name === "TimeoutError");
  assert.equal(clearedTimer, timerHandle);
});

test("workspace transport caller cancellation settles while session ensure is pending", async () => {
  const caller = new AbortController();
  const ensured = deferred();
  let fetched = 0;
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: () => ensured.promise,
    renewSession: async () => session("unused"),
    clientVersion: "0.3.1",
    fetchImpl: async () => {
      fetched += 1;
      return new Response(null, { status: 204 });
    },
  });

  const pending = transport.request({
    expectedOrigin: ORIGIN_A,
    pathname: "/rag/docs",
    init: { signal: caller.signal },
  });
  caller.abort(new DOMException("backend origin changed", "AbortError"));
  await assert.rejects(pending, (error) => error?.name === "AbortError");
  assert.equal(fetched, 0);

  // The detached identity operation may still complete later; it must neither
  // restart the cancelled workspace request nor become an unhandled rejection.
  ensured.resolve(session("late-token"));
  await Promise.resolve();
  assert.equal(fetched, 0);
});

test("workspace transport timeout settles while session renewal is pending", async () => {
  const renewal = deferred();
  const renewalStarted = deferred();
  let fireTimeout = null;
  let fetches = 0;
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => session("expired-token"),
    renewSession: () => {
      renewalStarted.resolve();
      return renewal.promise;
    },
    clientVersion: "0.3.1",
    fetchImpl: async () => {
      fetches += 1;
      return new Response(null, { status: 401 });
    },
    setTimer: (callback) => {
      fireTimeout = callback;
      return Symbol("renew-timeout");
    },
    clearTimer: () => undefined,
  });

  const pending = transport.request({
    expectedOrigin: ORIGIN_A,
    pathname: "/rag/docs",
  });
  await renewalStarted.promise;
  assert.equal(typeof fireTimeout, "function");
  fireTimeout();
  await assert.rejects(pending, (error) => error?.name === "TimeoutError");
  assert.equal(fetches, 1);

  renewal.resolve(session("late-renewed-token"));
  await Promise.resolve();
  assert.equal(fetches, 1);
});

test("workspace transport rejects an oversized declared response before buffering", async () => {
  let cancelled = false;
  const body = new ReadableStream({
    cancel() {
      cancelled = true;
    },
  });
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => session("response-token"),
    renewSession: async () => session("unused"),
    clientVersion: "0.3.1",
    fetchImpl: async () =>
      new Response(body, {
        status: 200,
        headers: {
          "Content-Length": String(MAX_WORKSPACE_RESPONSE_BYTES + 1),
        },
      }),
  });

  await assert.rejects(
    transport.request({
      expectedOrigin: ORIGIN_A,
      pathname: "/rag/docs",
    }),
    (error) => {
      assert.equal(error.code, "WORKSPACE_BACKEND_RESPONSE_TOO_LARGE");
      assert.equal(error.status, 200);
      return true;
    },
  );
  assert.equal(cancelled, true);
});

test("workspace transport enforces the byte cap on chunked responses", async () => {
  const chunkBytes = Math.floor(MAX_WORKSPACE_RESPONSE_BYTES / 2) + 1;
  let emitted = 0;
  let cancelled = false;
  const body = new ReadableStream({
    pull(controller) {
      emitted += 1;
      controller.enqueue(new Uint8Array(chunkBytes));
    },
    cancel() {
      cancelled = true;
    },
  });
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => session("response-token"),
    renewSession: async () => session("unused"),
    clientVersion: "0.3.1",
    fetchImpl: async () => new Response(body, { status: 200 }),
  });

  await assert.rejects(
    transport.request({
      expectedOrigin: ORIGIN_A,
      pathname: "/rag/docs",
    }),
    (error) => error.code === "WORKSPACE_BACKEND_RESPONSE_TOO_LARGE",
  );
  await Promise.resolve();
  assert.ok(emitted >= 2);
  assert.equal(cancelled, true);
});

test("workspace transport preserves the original response body error", async () => {
  const upstreamError = new Error("response stream failed");
  const transport = createWorkspaceBackendTransport({
    backendBase: ORIGIN_A,
    vault: { backendOrigin: ORIGIN_A },
    ensureSession: async () => session("response-token"),
    renewSession: async () => session("unused"),
    clientVersion: "0.3.1",
    fetchImpl: async () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.error(upstreamError);
          },
        }),
        { status: 200 },
      ),
  });

  await assert.rejects(
    transport.request({
      expectedOrigin: ORIGIN_A,
      pathname: "/rag/docs",
    }),
    (error) => error === upstreamError,
  );
});
