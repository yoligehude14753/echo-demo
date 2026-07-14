import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("Electron retries bootstrap after a transient unavailable failure", async ({ page }) => {
  await installEchoMock(page);
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    session.resetSessionForTest();
    const originalFetch = window.fetch.bind(window);
    let bootstrapCalls = 0;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (new URL(url, window.location.origin).pathname.endsWith("/bootstrap")) {
        bootstrapCalls += 1;
        if (bootstrapCalls === 1) throw new TypeError("backend is still starting");
      }
      return originalFetch(input, init);
    };

    let firstReason = "";
    try {
      await session.bootstrapBackend();
    } catch (error) {
      firstReason = (error as { reason?: string }).reason ?? "";
    }
    const bootstrap = await session.bootstrapBackend();
    window.fetch = originalFetch;
    return { bootstrapCalls, firstReason, backendVersion: bootstrap?.backend_version };
  });

  expect(result).toEqual({
    bootstrapCalls: 2,
    firstReason: "backend_unreachable",
    backendVersion: "0.3.2",
  });
});

test("backend URL policy allows explicit private HTTP but rejects public cleartext", async ({
  page,
}) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const accepted = [
      "http://localhost:8769",
      "http://127.0.0.1:8769",
      "http://10.1.2.3:8769",
      "http://172.16.0.1:8769",
      "http://172.31.255.254:8769",
      "http://192.168.4.5:8769",
      "http://169.254.10.20:8769",
      "http://[::1]:8769",
      "http://[fd12:3456::1]:8769",
      "http://[fe80::1234]:8769",
      "https://public.example:443",
    ].map((value) => runtime.normalizeBackendBase(value));
    const rejected = [
      "http://public.example:8769",
      "http://8.8.8.8:8769",
      "http://172.32.0.1:8769",
      "http://192.0.2.1:8769",
      "http://[2001:db8::1]:8769",
      "http://user:pass@10.0.0.2:8769",
      "http://10.0.0.2:8769/api",
      "ftp://10.0.0.2:8769",
    ].map((value) => {
      try {
        runtime.normalizeBackendBase(value);
        return null;
      } catch (error) {
        return error instanceof Error ? error.message : String(error);
      }
    });
    return { accepted, rejected };
  });

  expect(result.accepted).toEqual([
    "http://localhost:8769",
    "http://127.0.0.1:8769",
    "http://10.1.2.3:8769",
    "http://172.16.0.1:8769",
    "http://172.31.255.254:8769",
    "http://192.168.4.5:8769",
    "http://169.254.10.20:8769",
    "http://[::1]:8769",
    "http://[fd12:3456::1]:8769",
    "http://[fe80::1234]:8769",
    "https://public.example",
  ]);
  expect(result.rejected.every((message) => typeof message === "string")).toBe(true);
});

test("settings rejects public HTTP and requires explicit confirmation for private HTTP", async ({
  page,
}) => {
  await installEchoMock(page);
  await page.goto("/");
  await page.getByTestId("open-settings").click();

  const input = page.getByTestId("mobile-backend-base");
  await input.fill("http://public.example:8769");
  await page.getByTestId("save-mobile-backend-base").click();
  await expect(page.getByText("公网主机必须使用 HTTPS", { exact: false })).toBeVisible();
  await expect(input).toHaveValue("http://public.example:8769");
  await expect
    .poll(() =>
      page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase")),
    )
    .toBeNull();

  await input.fill("http://192.168.8.20:8769");
  await page.getByTestId("save-mobile-backend-base").click();
  const dialog = page.getByRole("dialog", { name: "确认使用局域网明文连接？" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("需要设备身份的服务会拒绝通过 HTTP 发送凭证");
  await expect
    .poll(() =>
      page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase")),
    )
    .toBeNull();
  await dialog.getByRole("button", { name: "确认仅用于可信局域网" }).click();
  await expect
    .poll(() =>
      page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase")),
    )
    .toBe("http://192.168.8.20:8769");
});

test("session-required private HTTP fails before any device secret fetch", async ({ page }) => {
  let bootstrapCalls = 0;
  let identityCalls = 0;
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/bootstrap", "/session/enroll", "/session/renew"],
  });
  await page.route("http://10.20.30.40:8769/bootstrap", (route) => {
    bootstrapCalls += 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        backend_version: "0.3.1-test",
        session_required: true,
        capabilities: { principal_sessions: true },
      }),
    });
  });
  await page.route(/http:\/\/10\.20\.30\.40:8769\/session\/(?:enroll|renew)$/, (route) => {
    identityCalls += 1;
    return route.fulfill({ status: 500, body: "must not be called" });
  });

  await page.goto("/");
  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("http://10.20.30.40:8769");
    session.resetSessionForTest();
    try {
      await session.ensureServerSession();
      return { kind: "", message: "" };
    } catch (error) {
      return {
        kind: (error as { kind?: string }).kind ?? "",
        message: error instanceof Error ? error.message : String(error),
      };
    }
  });

  expect(bootstrapCalls).toBe(1);
  expect(identityCalls).toBe(0);
  expect(result.kind).toBe("invalid-origin");
  expect(result.message).toContain("HTTPS");
});

test("renderer identity refuses redirects before replaying a device credential", async ({
  page,
}) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("https://identity-a.example");
    session.resetSessionForTest();
    const originalFetch = window.fetch.bind(window);
    const calls: Array<{ url: string; redirect: RequestRedirect | undefined }> = [];
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url === "https://identity-a.example/bootstrap") {
        return new Response(
          JSON.stringify({
            schema_version: 1,
            api_version: "0.3",
            session_required: true,
            minimum_client_version: "0.3.1",
            capabilities: { principal_sessions: true },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (/^https:\/\/identity-a\.example\/session\/(?:enroll|renew)$/.test(url)) {
        calls.push({ url, redirect: init?.redirect });
        return new Response(null, {
          status: 307,
          headers: { Location: "https://identity-b.example/credential-leak" },
        });
      }
      if (url.startsWith("https://identity-b.example/")) {
        calls.push({ url, redirect: init?.redirect });
        return new Response("must not be reached", { status: 200 });
      }
      return originalFetch(input, init);
    };
    try {
      await session.ensureServerSession();
      return { error: null, calls };
    } catch (error) {
      return {
        error: {
          name: error instanceof Error ? error.name : "",
          message: error instanceof Error ? error.message : String(error),
        },
        calls,
      };
    }
  });

  expect(result.error).toMatchObject({ name: "BackendRedirectForbiddenError" });
  expect(result.calls).toHaveLength(1);
  expect(result.calls[0]?.url).toMatch(
    /^https:\/\/identity-a\.example\/session\/(?:enroll|renew)$/,
  );
  expect(result.calls[0]?.redirect).toBe("error");
});

test("renderer identity timeout covers a stalled response body", async ({ page }) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("https://identity-a.example");
    session.resetSessionForTest();
    const originalFetch = window.fetch.bind(window);
    const originalSetTimeout = window.setTimeout.bind(window);
    let cancelled = 0;
    let identityCalls = 0;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url === "https://identity-a.example/bootstrap") {
        return new Response(
          JSON.stringify({
            schema_version: 1,
            api_version: "0.3",
            session_required: true,
            minimum_client_version: "0.3.1",
            capabilities: { principal_sessions: true },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (/^https:\/\/identity-a\.example\/session\/(?:enroll|renew)$/.test(url)) {
        identityCalls += 1;
        return new Response(
          new ReadableStream<Uint8Array>({
            cancel() {
              cancelled += 1;
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return originalFetch(input, init);
    };
    window.setTimeout = ((
      handler: TimerHandler,
      timeout?: number,
      ...args: unknown[]
    ) => originalSetTimeout(handler, Math.min(Number(timeout ?? 0), 75), ...args)) as typeof window.setTimeout;
    const started = performance.now();
    try {
      await session.ensureServerSession();
      return { error: null, elapsed: performance.now() - started, cancelled, identityCalls };
    } catch (error) {
      return {
        error: {
          name: error instanceof Error ? error.name : "",
          message: error instanceof Error ? error.message : String(error),
        },
        elapsed: performance.now() - started,
        cancelled,
        identityCalls,
      };
    } finally {
      window.setTimeout = originalSetTimeout as typeof window.setTimeout;
    }
  });

  expect(result.error?.name).toBe("TimeoutError");
  expect(result.elapsed).toBeLessThan(1_000);
  expect(result.identityCalls).toBe(1);
  expect(result.cancelled).toBe(1);
});

test("Electron rejects an A-origin session before any credential can reach backend B", async ({
  page,
}) => {
  let backendBCalls = 0;
  let backendBAuthorization = "";
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "echodesk.mobileBackendBase",
      "https://identity-b.example",
    );
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      backendHost: "https://identity-a.example",
      ensurePublicSession: async () => ({
        token: "identity-a-secret-token-must-never-reach-b",
        expires_at: "2099-01-01T00:00:00Z",
        backend_origin: "https://identity-a.example",
        principal: { tenant_id: "tenant-a", owner_id: "owner-a" },
      }),
    };
  });
  await installEchoMock(page, {
    skipPaths: ["/bootstrap", "/cross-origin-leak-probe"],
  });
  await page.route("https://identity-b.example/bootstrap", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        session_required: true,
        minimum_client_version: "0.3.1",
        capabilities: { principal_sessions: true },
      }),
    }),
  );
  await page.route(
    "https://identity-b.example/cross-origin-leak-probe",
    (route) => {
      backendBCalls += 1;
      backendBAuthorization = route.request().headers().authorization ?? "";
      return route.fulfill({ status: 200, body: "must not be reached" });
    },
  );
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    session.resetSessionForTest();
    try {
      await session.apiTransport(
        "https://identity-b.example/cross-origin-leak-probe",
      );
      return null;
    } catch (error) {
      return {
        name: error instanceof Error ? error.name : "",
        kind: (error as { kind?: string }).kind ?? "",
        message: error instanceof Error ? error.message : String(error),
        identity: session.currentSessionIdentityStatus(),
      };
    }
  });

  expect(result).toEqual({
    name: "IdentityCredentialStoreError",
    kind: "invalid-origin",
    message: "设备会话所属服务与当前后端不一致；已拒绝接收跨服务会话凭证",
    identity: {
      phase: "error",
      message: "设备会话所属服务与当前后端不一致；已拒绝接收跨服务会话凭证",
    },
  });
  expect(backendBCalls).toBe(0);
  expect(backendBAuthorization).toBe("");
});

test("Electron rejects a session backend URL with path/query/fragment before business I/O", async ({
  page,
}) => {
  let businessCalls = 0;
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "echodesk.mobileBackendBase",
      "https://identity-a.example",
    );
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      backendHost: "https://identity-a.example",
      ensurePublicSession: async () => ({
        token: "same-origin-but-non-origin-url-token",
        expires_at: "2099-01-01T00:00:00Z",
        backend_origin: "https://identity-a.example/private?scope=wrong#fragment",
        principal: { tenant_id: "tenant-a", owner_id: "owner-a" },
      }),
    };
  });
  await installEchoMock(page, {
    skipPaths: ["/bootstrap", "/strict-session-origin-probe"],
  });
  await page.route("https://identity-a.example/bootstrap", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        session_required: true,
        minimum_client_version: "0.3.1",
        capabilities: { principal_sessions: true },
      }),
    }),
  );
  await page.route(
    "https://identity-a.example/strict-session-origin-probe",
    (route) => {
      businessCalls += 1;
      return route.fulfill({ status: 200, body: "must not be reached" });
    },
  );
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    session.resetSessionForTest();
    try {
      await session.apiTransport(
        "https://identity-a.example/strict-session-origin-probe",
      );
      return null;
    } catch (error) {
      return {
        name: error instanceof Error ? error.name : "",
        kind: (error as { kind?: string }).kind ?? "",
      };
    }
  });

  expect(result).toEqual({
    name: "IdentityCredentialStoreError",
    kind: "invalid-origin",
  });
  expect(businessCalls).toBe(0);
});

test("transport aborts an in-flight A response and rejects stale A URLs after switching to B", async ({
  page,
}) => {
  await installEchoMock(page);
  await page.goto("/");

  const started = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("https://identity-a.example");
    session.resetSessionForTest();
    const originalFetch = window.fetch.bind(window);
    const state: {
      calls: number;
      release: (() => void) | null;
      pending: Promise<Record<string, unknown>> | null;
    } = { calls: 0, release: null, pending: null };
    (
      window as unknown as {
        __staleOriginTransport__: typeof state;
      }
    ).__staleOriginTransport__ = state;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url === "https://identity-a.example/stale-origin-probe") {
        state.calls += 1;
        return new Promise<Response>((resolve) => {
          state.release = () =>
            resolve(
              new Response(JSON.stringify({ secret: "A_SECRET_RESPONSE" }), {
                status: 200,
                headers: { "Content-Type": "application/json" },
              }),
            );
        });
      }
      return originalFetch(input, init);
    };
    state.pending = session
      .apiTransport("https://identity-a.example/stale-origin-probe")
      .then(async (response) => ({ body: await response.json() }))
      .catch((error: unknown) => ({
        name: error instanceof Error ? error.name : "",
        kind: (error as { kind?: string }).kind ?? "",
        message: error instanceof Error ? error.message : String(error),
      }));
    return true;
  });
  expect(started).toBe(true);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __staleOriginTransport__: { calls: number };
            }
          ).__staleOriginTransport__.calls,
      ),
    )
    .toBe(1);

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    const state = (
      window as unknown as {
        __staleOriginTransport__: {
          calls: number;
          release: (() => void) | null;
          pending: Promise<Record<string, unknown>>;
        };
      }
    ).__staleOriginTransport__;
    runtime.setStoredBackendBase("https://identity-b.example");
    state.release?.();
    const staleResponse = await state.pending;
    let staleUrlError: Record<string, unknown> | null = null;
    try {
      await session.apiTransport(
        "https://identity-a.example/must-not-be-downgraded-to-external-fetch",
      );
    } catch (error) {
      staleUrlError = {
        name: error instanceof Error ? error.name : "",
        kind: (error as { kind?: string }).kind ?? "",
        message: error instanceof Error ? error.message : String(error),
      };
    }
    return { staleResponse, staleUrlError, calls: state.calls };
  });

  expect(result.staleResponse).toMatchObject({
    name: "ApiTransportError",
    kind: "stale-origin",
  });
  expect(result.staleUrlError).toMatchObject({
    name: "ApiTransportError",
    kind: "stale-origin",
  });
  expect(result.calls).toBe(1);
  expect(JSON.stringify(result)).not.toContain("A_SECRET_RESPONSE");
});

test("backend transport forbids redirects without replaying a request body", async ({
  page,
}) => {
  await installEchoMock(page);
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("https://identity-a.example");
    session.resetSessionForTest();
    const originalFetch = window.fetch.bind(window);
    const calls: Array<{ url: string; redirect: RequestRedirect | undefined }> = [];
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url.startsWith("https://identity-a.example/redirect-probe")) {
        calls.push({ url, redirect: init?.redirect });
        return new Response(null, {
          status: 307,
          headers: { Location: "https://identity-b.example/leak" },
        });
      }
      if (url.startsWith("https://identity-b.example/")) {
        calls.push({ url, redirect: init?.redirect });
        return new Response("must not be reached", { status: 200 });
      }
      return originalFetch(input, init);
    };
    try {
      await session.apiTransport(
        "https://identity-a.example/redirect-probe",
        { method: "POST", body: "PRIVATE_BUSINESS_BODY" },
        { anonymous: true },
      );
      return { error: null, calls };
    } catch (error) {
      return {
        error: {
          name: error instanceof Error ? error.name : "",
          kind: (error as { kind?: string }).kind ?? "",
          message: error instanceof Error ? error.message : String(error),
        },
        calls,
      };
    }
  });

  expect(result.error).toMatchObject({
    name: "ApiTransportError",
    kind: "redirect-forbidden",
  });
  expect(result.calls).toEqual([
    {
      url: "https://identity-a.example/redirect-probe",
      redirect: "error",
    },
  ]);
});

test("HTTP error detail rejects a declared oversized body without buffering it", async ({
  page,
}) => {
  await installEchoMock(page);
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("https://identity-a.example");
    session.resetSessionForTest();
    const originalFetch = window.fetch.bind(window);
    let pulls = 0;
    let cancelled = 0;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url === "https://identity-a.example/oversized-error") {
        return new Response(
          new ReadableStream<Uint8Array>({
            pull() {
              pulls += 1;
            },
            cancel() {
              cancelled += 1;
            },
          }),
          {
            status: 500,
            headers: { "Content-Length": "100000000" },
          },
        );
      }
      return originalFetch(input, init);
    };
    try {
      await session.apiTransport(
        "https://identity-a.example/oversized-error",
        {},
        { anonymous: true, timeoutMs: 1_000 },
      );
      return { error: null, pulls, cancelled };
    } catch (error) {
      return {
        error: {
          name: error instanceof Error ? error.name : "",
          kind: (error as { kind?: string }).kind ?? "",
          detail: (error as { detail?: string | null }).detail ?? null,
        },
        pulls,
        cancelled,
      };
    }
  });

  expect(result.error).toEqual({
    name: "ApiTransportError",
    kind: "http",
    detail: null,
  });
  expect(result.pulls).toBeLessThanOrEqual(1);
  expect(result.cancelled).toBe(1);
});

test("transport timeout remains active after response headers", async ({ page }) => {
  await installEchoMock(page);
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("https://identity-a.example");
    session.resetSessionForTest();
    const originalFetch = window.fetch.bind(window);
    let cancelled = 0;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url === "https://identity-a.example/stalled-body") {
        return new Response(
          new ReadableStream<Uint8Array>({
            cancel() {
              cancelled += 1;
            },
          }),
          { status: 200 },
        );
      }
      return originalFetch(input, init);
    };
    const response = await session.apiTransport(
      "https://identity-a.example/stalled-body",
      {},
      { anonymous: true, timeoutMs: 75 },
    );
    try {
      await response.text();
      return { error: null, cancelled };
    } catch (error) {
      return {
        error: {
          name: error instanceof Error ? error.name : "",
          kind: (error as { kind?: string }).kind ?? "",
          message: error instanceof Error ? error.message : String(error),
        },
        cancelled,
      };
    }
  });

  expect(result.error).toMatchObject({ name: "ApiTransportError", kind: "timeout" });
  expect(result.cancelled).toBe(1);
});

test("origin switch aborts a response body after headers", async ({ page }) => {
  await installEchoMock(page);
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("https://identity-a.example");
    session.resetSessionForTest();
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url === "https://identity-a.example/origin-stalled-body") {
        return new Response(new ReadableStream<Uint8Array>({}), { status: 200 });
      }
      return originalFetch(input, init);
    };
    const response = await session.apiTransport(
      "https://identity-a.example/origin-stalled-body",
      {},
      { anonymous: true, timeoutMs: 5_000 },
    );
    runtime.setStoredBackendBase("https://identity-b.example");
    try {
      await response.text();
      return null;
    } catch (error) {
      return {
        name: error instanceof Error ? error.name : "",
        kind: (error as { kind?: string }).kind ?? "",
        message: error instanceof Error ? error.message : String(error),
      };
    }
  });

  expect(result).toMatchObject({ name: "ApiTransportError", kind: "stale-origin" });
});

test("caller abort remains attached after response headers", async ({ page }) => {
  await installEchoMock(page);
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("https://identity-a.example");
    session.resetSessionForTest();
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url === "https://identity-a.example/caller-stalled-body") {
        return new Response(new ReadableStream<Uint8Array>({}), { status: 200 });
      }
      return originalFetch(input, init);
    };
    const controller = new AbortController();
    const response = await session.apiTransport(
      "https://identity-a.example/caller-stalled-body",
      { signal: controller.signal },
      { anonymous: true, timeoutMs: 5_000 },
    );
    controller.abort(new DOMException("caller stopped", "AbortError"));
    try {
      await response.text();
      return null;
    } catch (error) {
      return {
        name: error instanceof Error ? error.name : "",
        kind: (error as { kind?: string }).kind ?? "",
        message: error instanceof Error ? error.message : String(error),
      };
    }
  });

  expect(result).toMatchObject({ name: "ApiTransportError", kind: "aborted" });
});

test("Request signal and headers survive transport while caller authorization is stripped", async ({
  page,
}) => {
  await installEchoMock(page);
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("https://identity-a.example");
    session.resetSessionForTest();
    const originalFetch = window.fetch.bind(window);
    let observed: Record<string, string | null> = {};
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url === "https://identity-a.example/request-object") {
        const headers = new Headers(
          init?.headers ?? (input instanceof Request ? input.headers : undefined),
        );
        observed = {
          authorization: headers.get("Authorization"),
          business: headers.get("X-Business"),
          fromInit: headers.get("X-Init"),
          contentType: headers.get("Content-Type"),
          clientVersion: headers.get("X-EchoDesk-Client-Version"),
          redirect: init?.redirect ?? null,
          method: init?.method ?? (input instanceof Request ? input.method : null),
        };
        return new Response(new ReadableStream<Uint8Array>({}), { status: 200 });
      }
      return originalFetch(input, init);
    };
    const controller = new AbortController();
    const request = new Request("https://identity-a.example/request-object", {
      method: "POST",
      headers: {
        Authorization: "request-secret-must-not-leave",
        "Content-Type": "text/plain",
        "X-Business": "preserved",
      },
      body: "request-body",
      signal: controller.signal,
    });
    const response = await session.apiTransport(
      request,
      {
        headers: {
          Authorization: "init-secret-must-not-leave",
          "X-Init": "preserved-too",
        },
      },
      { anonymous: true, timeoutMs: 5_000 },
    );
    controller.abort(new DOMException("caller stopped", "AbortError"));
    try {
      await response.text();
      return { error: null, observed };
    } catch (error) {
      return {
        error: {
          name: error instanceof Error ? error.name : "",
          kind: (error as { kind?: string }).kind ?? "",
        },
        observed,
      };
    }
  });

  expect(result.error).toEqual({ name: "ApiTransportError", kind: "aborted" });
  expect(result.observed).toEqual({
    authorization: null,
    business: "preserved",
    fromInit: "preserved-too",
    contentType: "text/plain",
    clientVersion: "0.3.2",
    redirect: "error",
    method: "POST",
  });
});

test("401 renew never replays a one-shot Request body", async ({ page }) => {
  await page.addInitScript(() => {
    const state = { ensureCalls: 0, renewCalls: 0 };
    (window as unknown as { __oneShot401__: typeof state }).__oneShot401__ = state;
    window.localStorage.setItem(
      "echodesk.mobileBackendBase",
      "https://identity-a.example",
    );
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      ensurePublicSession: async () => {
        state.ensureCalls += 1;
        return {
          token: "one-shot-token-1",
          expires_at: "2099-01-01T00:00:00Z",
          backend_origin: "https://identity-a.example",
          principal: { tenant_id: "tenant", owner_id: "owner" },
        };
      },
      renewPublicSession: async () => {
        state.renewCalls += 1;
        return {
          token: "one-shot-token-2",
          expires_at: "2099-01-01T00:00:00Z",
          backend_origin: "https://identity-a.example",
          principal: { tenant_id: "tenant", owner_id: "owner" },
        };
      },
    };
  });
  await installEchoMock(page, {
    skipPaths: ["/bootstrap", "/one-shot-401"],
  });
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const originalFetch = window.fetch.bind(window);
    const state = (
      window as unknown as {
        __oneShot401__: { ensureCalls: number; renewCalls: number };
      }
    ).__oneShot401__;
    state.ensureCalls = 0;
    state.renewCalls = 0;
    let businessCalls = 0;
    let authorization = "";
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url === "https://identity-a.example/bootstrap") {
        return new Response(
          JSON.stringify({
            schema_version: 1,
            api_version: "0.3",
            session_required: true,
            minimum_client_version: "0.3.1",
            capabilities: { principal_sessions: true },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url === "https://identity-a.example/one-shot-401") {
        businessCalls += 1;
        authorization = new Headers(
          init?.headers ?? (input instanceof Request ? input.headers : undefined),
        ).get("Authorization") ?? "";
        return new Response("expired", { status: 401 });
      }
      return originalFetch(input, init);
    };
    session.resetSessionForTest();
    const request = new Request("https://identity-a.example/one-shot-401", {
      method: "POST",
      headers: { "Content-Type": "text/plain" },
      body: "one-shot-private-body",
    });
    try {
      await session.apiTransport(request);
      return { error: null, businessCalls, authorization, state };
    } catch (error) {
      return {
        error: {
          name: error instanceof Error ? error.name : "",
          kind: (error as { kind?: string }).kind ?? "",
        },
        businessCalls,
        authorization,
        state: { ...state },
      };
    }
  });

  expect(result).toEqual({
    error: { name: "ApiTransportError", kind: "replay-required" },
    businessCalls: 1,
    authorization: "Bearer one-shot-token-1",
    state: { ensureCalls: 1, renewCalls: 1 },
  });
});

test("transport response fails closed on clone and tee branch buffering", async ({
  page,
}) => {
  await installEchoMock(page);
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("https://identity-a.example");
    session.resetSessionForTest();
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url === "https://identity-a.example/no-branch") {
        return new Response("single leased body", { status: 200 });
      }
      return originalFetch(input, init);
    };
    const response = await session.apiTransport(
      "https://identity-a.example/no-branch",
      {},
      { anonymous: true, timeoutMs: 5_000 },
    );
    const errors: Array<{ name: string; kind: string }> = [];
    for (const branch of [
      () => response.clone(),
      () => response.body?.tee(),
    ]) {
      try {
        branch();
      } catch (error) {
        errors.push({
          name: error instanceof Error ? error.name : "",
          kind: (error as { kind?: string }).kind ?? "",
        });
      }
    }
    return { errors, body: await response.text() };
  });

  expect(result).toEqual({
    errors: [
      { name: "ApiTransportError", kind: "stream-branch-forbidden" },
      { name: "ApiTransportError", kind: "stream-branch-forbidden" },
    ],
    body: "single leased body",
  });
});

test("late meeting hydrate from backend A cannot repopulate backend B state", async ({
  page,
}) => {
  await installEchoMock(page);
  await page.goto("/");
  await expect
    .poll(() =>
      page.evaluate(async () => {
        const { useStore } = await import("/src/store.ts");
        return useStore.getState().meetingListLoadPhase;
      }),
    )
    .toBe("ready");

  await page.evaluate(async () => {
    const originalFetch = window.fetch.bind(window);
    const state: {
      aCalls: number;
      aCompleted: number;
      bCalls: number;
      releaseA: (() => void) | null;
    } = { aCalls: 0, aCompleted: 0, bCalls: 0, releaseA: null };
    (
      window as unknown as {
        __originHydrateState__: typeof state;
      }
    ).__originHydrateState__ = state;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const url = new URL(raw, window.location.href);
      if (url.pathname === "/meetings" && url.searchParams.has("limit")) {
        if (url.origin === "https://identity-a.example") {
          state.aCalls += 1;
          const response = await new Promise<Response>((resolve) => {
            state.releaseA = () =>
              resolve(
                new Response(
                  JSON.stringify([
                    {
                      meeting_id: "tenant-a-secret-meeting",
                      title: "A 租户机密会议",
                      display_title: "A_SECRET_MEETING",
                      state: "ended",
                      started_at: "2026-07-12T00:00:00Z",
                      ended_at: "2026-07-12T00:01:00Z",
                      finalized_at: null,
                      n_segments: 1,
                      n_speakers: 1,
                      has_minutes: false,
                    },
                  ]),
                  {
                    status: 200,
                    headers: { "Content-Type": "application/json" },
                  },
                ),
              );
          });
          state.aCompleted += 1;
          return response;
        }
        if (url.origin === "https://identity-b.example") {
          state.bCalls += 1;
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
      }
      return originalFetch(input, init);
    };
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase("https://identity-a.example");
  });

  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __originHydrateState__: { aCalls: number };
            }
          ).__originHydrateState__.aCalls,
      ),
    )
    .toBe(1);

  await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase("https://identity-b.example");
  });
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __originHydrateState__: { bCalls: number };
            }
          ).__originHydrateState__.bCalls,
      ),
    )
    .toBe(1);
  await expect
    .poll(() =>
      page.evaluate(async () => {
        const { useStore } = await import("/src/store.ts");
        return useStore.getState().meetingListLoadPhase;
      }),
    )
    .toBe("ready");

  await page.evaluate(() => {
    const state = (
      window as unknown as {
        __originHydrateState__: { releaseA: (() => void) | null };
      }
    ).__originHydrateState__;
    state.releaseA?.();
  });
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __originHydrateState__: { aCompleted: number };
            }
          ).__originHydrateState__.aCompleted,
      ),
    )
    .toBe(1);
  const finalState = await page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    return {
      meetingIds: Object.keys(useStore.getState().meetings),
      serialized: JSON.stringify(useStore.getState().meetings),
    };
  });

  expect(finalState.meetingIds).not.toContain("tenant-a-secret-meeting");
  expect(finalState.serialized).not.toContain("A_SECRET_MEETING");
  expect(await page.locator("body").innerText()).not.toContain("A 租户机密会议");
});

test("bootstrap minimum version blocks identity establishment with an upgrade state", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const state = { ensureCalls: 0 };
    (window as unknown as { __upgradeBootstrapState__: typeof state })
      .__upgradeBootstrapState__ = state;
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      ensurePublicSession: async () => {
        state.ensureCalls += 1;
        return { token: "must-not-be-issued" };
      },
    };
  });
  await installEchoMock(page, { skipPaths: ["/bootstrap"] });
  await page.route(/\/(?:api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        session_required: true,
        minimum_client_version: "0.4.0",
        capabilities: { principal_sessions: true },
      }),
    }),
  );
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    session.resetSessionForTest();
    try {
      await session.ensureServerSession();
      return { name: "", message: "", compatibility: "", ensureCalls: -1 };
    } catch (error) {
      return {
        name: error instanceof Error ? error.name : "",
        message: error instanceof Error ? error.message : String(error),
        compatibility: session.backendCompatibility(),
        ensureCalls: (
          window as unknown as {
            __upgradeBootstrapState__: { ensureCalls: number };
          }
        ).__upgradeBootstrapState__.ensureCalls,
      };
    }
  });

  expect(result).toEqual({
    name: "ClientUpgradeRequiredError",
    message: "需要 EchoDesk 0.4.0 或更高版本才能连接公共服务",
    compatibility: "upgrade-required",
    ensureCalls: 0,
  });
});

test("runtime 426 stops renewal and becomes a precise upgrade-required error", async ({
  page,
}) => {
  let probeCalls = 0;
  await page.addInitScript(() => {
    const state = { ensureCalls: 0, renewCalls: 0 };
    (window as unknown as { __runtimeUpgradeState__: typeof state })
      .__runtimeUpgradeState__ = state;
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      ensurePublicSession: async () => {
        state.ensureCalls += 1;
        return {
          token: "compatible-session-token",
          expires_at: "2099-01-01T00:00:00Z",
          principal: { tenant_id: "tenant", owner_id: "owner" },
        };
      },
      renewPublicSession: async () => {
        state.renewCalls += 1;
        return { token: "must-not-renew" };
      },
    };
  });
  await installEchoMock(page, {
    skipPaths: ["/bootstrap", "/runtime-upgrade-probe"],
  });
  await page.route(/\/(?:api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        session_required: true,
        minimum_client_version: "0.3.1",
        capabilities: { principal_sessions: true },
      }),
    }),
  );
  await page.route(/\/(?:api\/)?runtime-upgrade-probe$/, (route) => {
    probeCalls += 1;
    return route.fulfill({
      status: 426,
      contentType: "application/json",
      headers: { "X-EchoDesk-Minimum-Client-Version": "0.4.0" },
      body: JSON.stringify({ error: { code: "client_upgrade_required" } }),
    });
  });
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const runtimeState = (
      window as unknown as {
        __runtimeUpgradeState__: {
          ensureCalls: number;
          renewCalls: number;
        };
      }
    ).__runtimeUpgradeState__;
    runtimeState.ensureCalls = 0;
    runtimeState.renewCalls = 0;
    session.resetSessionForTest();
    try {
      await session.apiTransport("/api/runtime-upgrade-probe");
      return null;
    } catch (error) {
      const latched = await Promise.allSettled([
        session.ensureServerSession(true),
        session.apiTransport("/api/runtime-upgrade-probe"),
      ]);
      return {
        name: error instanceof Error ? error.name : "",
        message: error instanceof Error ? error.message : String(error),
        compatibility: session.backendCompatibility(),
        latchedNames: latched.map((item) =>
          item.status === "rejected" && item.reason instanceof Error
            ? item.reason.name
            : "",
        ),
        state: (
          window as unknown as {
            __runtimeUpgradeState__: {
              ensureCalls: number;
              renewCalls: number;
            };
          }
        ).__runtimeUpgradeState__,
      };
    }
  });

  expect(result).toEqual({
    name: "ClientUpgradeRequiredError",
    message: "需要 EchoDesk 0.4.0 或更高版本才能连接公共服务",
    compatibility: "upgrade-required",
    latchedNames: [
      "ClientUpgradeRequiredError",
      "ClientUpgradeRequiredError",
    ],
    state: { ensureCalls: 1, renewCalls: 0 },
  });
  expect(probeCalls).toBe(1);
});

test("Electron credential rotation 426 enters upgrade-required without losing identity semantics", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const state = { ensureCalls: 0, rotateCalls: 0 };
    (window as unknown as { __rotationUpgradeState__: typeof state })
      .__rotationUpgradeState__ = state;
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      ensurePublicSession: async () => {
        state.ensureCalls += 1;
        return {
          token: "rotation-upgrade-token",
          expires_at: "2099-01-01T00:00:00Z",
          principal: { tenant_id: "tenant", owner_id: "owner" },
        };
      },
      rotatePublicCredential: async () => {
        state.rotateCalls += 1;
        const error = Object.assign(
          new Error("CLIENT_UPGRADE_REQUIRED minimum=0.4.0"),
          { code: "CLIENT_UPGRADE_REQUIRED", minimumVersion: "0.4.0" },
        );
        throw error;
      },
    };
  });
  await installEchoMock(page, { skipPaths: ["/bootstrap"] });
  await page.route(/\/(?:api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        session_required: true,
        minimum_client_version: "0.3.1",
        capabilities: { principal_sessions: true },
      }),
    }),
  );
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const rotationState = (
      window as unknown as {
        __rotationUpgradeState__: { ensureCalls: number; rotateCalls: number };
      }
    ).__rotationUpgradeState__;
    rotationState.ensureCalls = 0;
    rotationState.rotateCalls = 0;
    session.resetSessionForTest();
    try {
      await session.rotateServerCredential();
      return null;
    } catch (error) {
      return {
        name: error instanceof Error ? error.name : "",
        message: error instanceof Error ? error.message : String(error),
        compatibility: session.backendCompatibility(),
        identity: session.currentSessionIdentityStatus(),
        state: (
          window as unknown as {
            __rotationUpgradeState__: { ensureCalls: number; rotateCalls: number };
          }
        ).__rotationUpgradeState__,
      };
    }
  });

  expect(result).toEqual({
    name: "ClientUpgradeRequiredError",
    message: "需要 EchoDesk 0.4.0 或更高版本才能连接公共服务",
    compatibility: "upgrade-required",
    identity: {
      phase: "upgrade-required",
      message: "需要 EchoDesk 0.4.0 或更高版本才能连接公共服务",
    },
    state: { ensureCalls: 1, rotateCalls: 1 },
  });
});

test("explicit reconnect preserves upgrade-required instead of overwriting identity-lost", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      renewPublicSession: async () => {
        throw Object.assign(
          new Error("CLIENT_UPGRADE_REQUIRED minimum=0.4.0"),
          { code: "CLIENT_UPGRADE_REQUIRED", minimumVersion: "0.4.0" },
        );
      },
    };
  });
  await installEchoMock(page);
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    session.resetSessionForTest();
    try {
      await session.reconnectServerIdentity();
      return null;
    } catch (error) {
      return {
        name: error instanceof Error ? error.name : "",
        compatibility: session.backendCompatibility(),
        identity: session.currentSessionIdentityStatus(),
      };
    }
  });

  expect(result).toEqual({
    name: "ClientUpgradeRequiredError",
    compatibility: "upgrade-required",
    identity: {
      phase: "upgrade-required",
      message: "需要 EchoDesk 0.4.0 或更高版本才能连接公共服务",
    },
  });
});

test("WebSocket preflight upgrade error never creates or retries a socket", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const state = { ensureCalls: 0, updateCalls: 0 };
    (window as unknown as { __wsPreflightUpgradeState__: typeof state })
      .__wsPreflightUpgradeState__ = state;
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      ensurePublicSession: async () => {
        state.ensureCalls += 1;
        throw Object.assign(
          new Error("CLIENT_UPGRADE_REQUIRED minimum=0.4.0"),
          { code: "CLIENT_UPGRADE_REQUIRED", minimumVersion: "0.4.0" },
        );
      },
      openExternal: async () => {
        state.updateCalls += 1;
        return { ok: true };
      },
    };
  });
  await installEchoMock(page, { skipPaths: ["/bootstrap"] });
  await page.route(/\/(?:api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        session_required: true,
        minimum_client_version: "0.3.1",
        capabilities: { principal_sessions: true },
      }),
    }),
  );
  await page.goto("/");
  await expect(page.getByTestId("identity-status-upgrade")).toBeVisible();
  const socketsAtUpgrade = await page.evaluate(
    () =>
      (
        window as unknown as { __echoMock__: { wsCreated: number } }
      ).__echoMock__.wsCreated,
  );
  await page.clock.install();
  await page.getByTestId("identity-status-upgrade").click();
  await page.clock.runFor(10_000);

  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const { useStore } = await import("/src/store.ts");
    return {
      compatibility: session.backendCompatibility(),
      identity: session.currentSessionIdentityStatus(),
      connected: useStore.getState().connected,
      wsCreated: (
        window as unknown as { __echoMock__: { wsCreated: number } }
      ).__echoMock__.wsCreated,
      ensureCalls: (
        window as unknown as {
          __wsPreflightUpgradeState__: {
            ensureCalls: number;
            updateCalls: number;
          };
        }
      ).__wsPreflightUpgradeState__.ensureCalls,
    };
  });

  expect(result.compatibility).toBe("upgrade-required");
  expect(result.identity.phase).toBe("upgrade-required");
  expect(result.connected).toBe(false);
  expect(result.wsCreated).toBe(socketsAtUpgrade);
  expect(result.ensureCalls).toBe(1);
  expect(
    await page.evaluate(
      () =>
        (
          window as unknown as {
            __wsPreflightUpgradeState__: { updateCalls: number };
          }
        ).__wsPreflightUpgradeState__.updateCalls,
    ),
  ).toBe(1);
});

test("WebSocket 4426 enters upgrade-required and never reconnects", async ({
  page,
}) => {
  const mock = await installEchoMock(page);
  await page.goto("/");
  await expect.poll(async () => (await mock.wsSent()).length).toBeGreaterThan(0);
  await page.evaluate(() => {
    const ctrl = (
      window as unknown as {
        __echoMock__: { ws: unknown; originalWs?: unknown };
      }
    ).__echoMock__;
    ctrl.originalWs = ctrl.ws;
  });

  await page.clock.install();
  await mock.closeWs(4426, "client upgrade required:0.4.0");
  await page.clock.runFor(10_000);

  const state = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const ctrl = (
      window as unknown as {
        __echoMock__: { ws: unknown; originalWs?: unknown };
      }
    ).__echoMock__;
    return {
      compatibility: session.backendCompatibility(),
      sameSocket: ctrl.ws === ctrl.originalWs,
      identity: session.currentSessionIdentityStatus(),
    };
  });
  expect(state.compatibility).toBe("upgrade-required");
  expect(state.sameSocket).toBe(true);
  expect(state.identity).toEqual({
    phase: "upgrade-required",
    message: "需要 EchoDesk 0.4.0 或更高版本才能连接公共服务",
  });
});

test("public session consumes layered readiness without transcription fallback", async ({
  page,
}) => {
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/bootstrap", "/session/enroll", "/session/renew"],
  });

  let bootstrapStatus = 200;
  let bootstrapBody: Record<string, unknown> = {
    schema_version: 1,
    api_version: "0.3",
    session_required: false,
    capabilities: { principal_sessions: true },
  };
  await page.route(/\/(?:api\/)?bootstrap$/, (route) =>
    bootstrapStatus === 0
      ? route.abort("failed")
      : route.fulfill({
          status: bootstrapStatus,
          contentType: "application/json",
          body: JSON.stringify(bootstrapBody),
        }),
  );
  let sessionStatus = 200;
  await page.route(/\/(?:api\/)?session\/(?:enroll|renew)$/, (route) =>
    route.fulfill({
      status: sessionStatus,
      contentType: "application/json",
      body: JSON.stringify({
        token: "readiness-session-token",
        backend_origin: "http://127.0.0.1:8769",
      }),
    }),
  );
  await page.goto("/");

  const inspectBootstrap = async (
    body: Record<string, unknown>,
    status = 200,
  ) => {
    bootstrapBody = body;
    bootstrapStatus = status;
    return page.evaluate(async () => {
      const session = await import("/src/session.ts");
      session.resetSessionForTest();
      try {
        const bootstrap = await session.bootstrapBackend();
        return {
          ok: true,
          bootstrap: bootstrap !== null,
          readiness: session.backendReadiness(),
          diagnostic: session.backendReadinessDiagnosticCode(),
        };
      } catch (error) {
        return {
          ok: false,
          name: error instanceof Error ? error.name : "",
          code: (error as { code?: string }).code ?? "",
          reason: (error as { reason?: string }).reason ?? "",
          readiness: session.backendReadiness(),
          diagnostic: session.backendReadinessDiagnosticCode(),
        };
      }
    });
  };

  const old = await inspectBootstrap({
    schema_version: 1,
    api_version: "0.3",
    session_required: false,
    capabilities: { principal_sessions: true },
  });
  const ready = await inspectBootstrap({
    schema_version: 1,
    api_version: "0.3",
    session_required: false,
    capabilities: {
      principal_sessions: true,
      transcription_readiness: {
        schema_version: 1,
        status: "ready",
        accepting: true,
        checked_at: "2099-01-01T00:00:00.000Z",
        ttl_s: 3600,
      },
    },
  });
  const degraded = await inspectBootstrap({
    schema_version: 1,
    api_version: "0.3",
    session_required: false,
    capabilities: {
      principal_sessions: true,
      transcription_readiness: {
        schema_version: 1,
        status: "degraded",
        accepting: true,
        checked_at: "2099-01-01T00:00:00.000Z",
        ttl_s: 3600,
        reason_code: "capacity_degraded",
        retry_after_s: 30,
      },
    },
  });
  const unavailable = await inspectBootstrap({
    schema_version: 1,
    api_version: "0.3",
    session_required: false,
    capabilities: {
      principal_sessions: true,
      transcription_readiness: {
        schema_version: 1,
        status: "unavailable",
        accepting: false,
        checked_at: "2099-01-01T00:00:00.000Z",
        ttl_s: 3600,
      },
    },
  });
  const malformed = await inspectBootstrap({
    schema_version: 1,
    api_version: "0.3",
    session_required: false,
    capabilities: {
      principal_sessions: true,
      transcription_readiness: {
        schema_version: 1,
        status: "ready",
        accepting: false,
        checked_at: "2099-01-01T00:00:00.000Z",
        ttl_s: 3600,
      },
    },
  });
  const stale = await inspectBootstrap({
    schema_version: 1,
    api_version: "0.3",
    session_required: false,
    capabilities: {
      principal_sessions: true,
      transcription_readiness: {
        schema_version: 1,
        status: "ready",
        accepting: true,
        checked_at: "2020-01-01T00:00:00.000Z",
        ttl_s: 1,
      },
    },
  });
  const mismatch = await inspectBootstrap({
    schema_version: 99,
    api_version: "0.3",
    session_required: false,
    capabilities: { principal_sessions: true },
  });
  const unreachable = await inspectBootstrap({}, 0);

  const authCases: Record<number, unknown> = {};
  for (const status of [401, 403]) {
    sessionStatus = status;
    const bootstrap = await inspectBootstrap({
      schema_version: 1,
      api_version: "0.3",
      session_required: true,
      capabilities: { principal_sessions: true },
    });
    const sessionResult = await page.evaluate(async () => {
      const session = await import("/src/session.ts");
      try {
        await session.ensureServerSession();
        return { ok: true, readiness: session.backendReadiness() };
      } catch (error) {
        return {
          ok: false,
          name: error instanceof Error ? error.name : "",
          kind: (error as { kind?: string }).kind ?? "",
          code: (error as { code?: string }).code ?? "",
          readiness: session.backendReadiness(),
        };
      }
    });
    authCases[status] = { bootstrap, session: sessionResult };
  }

  const result = {
    old,
    ready,
    degraded,
    unavailable,
    malformed,
    stale,
    mismatch,
    unreachable,
    authCases,
  };

  expect(result.old).toMatchObject({
    ok: true,
    bootstrap: true,
    readiness: {
      reachability: "reachable",
      auth: "not_required",
      api_contract: "legacy",
      transcription_readiness: "unknown",
    },
  });
  expect(result.ready).toMatchObject({
    ok: true,
    readiness: { api_contract: "compatible", transcription_readiness: "ready" },
  });
  expect(result.degraded).toMatchObject({
    ok: true,
    readiness: { transcription_readiness: "degraded" },
  });
  expect(result.unavailable).toMatchObject({
    ok: true,
    readiness: { transcription_readiness: "unavailable" },
  });
  expect(result.malformed).toMatchObject({
    ok: true,
    readiness: { transcription_readiness: "unknown" },
    diagnostic: "readiness_unknown_malformed",
  });
  expect(result.stale).toMatchObject({
    ok: true,
    readiness: { transcription_readiness: "unknown" },
    diagnostic: "readiness_unknown_stale",
  });
  expect(result.mismatch).toMatchObject({
    ok: false,
    name: "BackendContractMismatchError",
    reason: "bootstrap-contract-mismatch",
    readiness: { reachability: "reachable", api_contract: "mismatch" },
  });
  expect(result.unreachable).toMatchObject({
    ok: false,
    name: "BackendReadinessError",
    code: "backend_unreachable",
    readiness: { reachability: "unreachable" },
  });
  for (const status of [401, 403]) {
    expect(result.authCases[status]).toMatchObject({
      bootstrap: { ok: true },
      session: {
        ok: false,
        readiness: { reachability: "reachable", auth: "failed" },
      },
    });
  }
});
