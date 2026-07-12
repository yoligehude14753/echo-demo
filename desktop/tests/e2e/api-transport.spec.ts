import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("API transport reuses server session and normalizes HTTP/timeout failures", async ({
  page,
}) => {
  let authorization = "";
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: [
      "/bootstrap",
      "/session/enroll",
      "/transport-ok",
      "/transport-http",
      "/transport-slow",
    ],
  });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        backend_version: "0.3.1-test",
        session_required: true,
        capabilities: { principal_sessions: true },
      }),
    }),
  );
  await page.route(/\/(api\/)?session\/enroll$/, (route) =>
    route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        token: "transport-session-token",
        expires_at: "2099-01-01T00:00:00Z",
      }),
    }),
  );
  await page.route(/\/(api\/)?transport-ok$/, (route) => {
    authorization = route.request().headers().authorization ?? "";
    return route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
  });
  await page.route(/\/(api\/)?transport-http$/, (route) =>
    route.fulfill({
      status: 503,
      contentType: "text/plain",
      body: "/Users/alice/Private/provider.env token=super-secret",
    }),
  );
  await page.route(/\/(api\/)?transport-slow$/, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 250));
    await route.fulfill({ status: 200, body: "late" });
  });

  await page.goto("/");
  const result = await page.evaluate(async () => {
    const { apiTransport, ApiTransportError, resetSessionForTest } = await import(
      "/src/session.ts"
    );
    resetSessionForTest();
    window.localStorage.removeItem("echodesk.serverSession.v1");

    const ok = await apiTransport("/api/transport-ok");
    let http: Record<string, unknown> = {};
    try {
      await apiTransport("/api/transport-http");
    } catch (error) {
      http = {
        typed: error instanceof ApiTransportError,
        kind: error instanceof ApiTransportError ? error.kind : null,
        status: error instanceof ApiTransportError ? error.status : null,
        detail: error instanceof ApiTransportError ? error.detail : null,
        message: error instanceof Error ? error.message : "",
      };
    }

    let timeout: Record<string, unknown> = {};
    try {
      await apiTransport("/api/transport-slow", {}, { timeoutMs: 20 });
    } catch (error) {
      timeout = {
        typed: error instanceof ApiTransportError,
        kind: error instanceof ApiTransportError ? error.kind : null,
      };
    }
    return { ok: ok.status, http, timeout };
  });

  expect(authorization).toBe("Bearer transport-session-token");
  expect(result.ok).toBe(200);
  expect(result.http).toEqual({
    typed: true,
    kind: "http",
    status: 503,
    detail: null,
    message: "HTTP 503",
  });
  expect(result.timeout).toEqual({ typed: true, kind: "timeout" });
});

test("API transport distinguishes redirects from null-body statuses", async ({ page }) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const { apiTransport, ApiTransportError } = await import("/src/session.ts");
    const originalFetch = window.fetch;
    let status = 204;
    window.fetch = async () => new Response(null, { status });
    try {
      const redirects: Array<{ status: number; kind: string | null }> = [];
      for (status of [301, 302, 303, 307, 308]) {
        try {
          await apiTransport(
            `/api/status-${status}`,
            {},
            { anonymous: true, throwHttpErrors: false },
          );
        } catch (error) {
          redirects.push({
            status,
            kind: error instanceof ApiTransportError ? error.kind : null,
          });
        }
      }

      const nullBodies: Array<{ status: number; text: string }> = [];
      for (status of [204, 205, 304]) {
        const response = await apiTransport(
          `/api/status-${status}`,
          {},
          { anonymous: true, throwHttpErrors: false },
        );
        nullBodies.push({ status: response.status, text: await response.text() });
      }
      return { redirects, nullBodies };
    } finally {
      window.fetch = originalFetch;
    }
  });

  expect(result.redirects).toEqual(
    [301, 302, 303, 307, 308].map((status) => ({
      status,
      kind: "redirect-forbidden",
    })),
  );
  expect(result.nullBodies).toEqual(
    [204, 205, 304].map((status) => ({ status, text: "" })),
  );
});

test("renderer API/RAG/Chat/TTS failures never expose server body canaries", async ({
  page,
}) => {
  const canary = "/Users/alice/Private/provider.env token=super-secret";
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/rag/docs", "/rag/ask", "/chat", "/tts/speak"],
  });
  await page.route(/\/(api\/)?rag\/docs$/, (route) =>
    route.fulfill({ status: 500, contentType: "text/plain", body: canary }),
  );
  await page.route(/\/(api\/)?rag\/ask$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: `event: error\ndata: ${JSON.stringify({ type: "error", error: canary })}\n\n`,
    }),
  );
  await page.route(/\/(api\/)?chat$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: `event: error\ndata: ${JSON.stringify({ error: canary })}\n\n`,
    }),
  );
  await page.route(/\/(api\/)?tts\/speak$/, (route) =>
    route.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({ detail: `tts_upstream_error: ${canary}` }),
    }),
  );
  await page.goto("/");

  const failures = await page.evaluate(async () => {
    const api = await import("/src/api.ts");
    const attempts: Array<() => Promise<unknown>> = [
      () => api.listRagDocs(),
      () => api.ragAsk("canary"),
      () => api.chatAsk("canary"),
      () => api.ttsSpeak("canary"),
    ];
    const caught: Array<{ message: string; detail: string | null }> = [];
    for (const attempt of attempts) {
      try {
        await attempt();
      } catch (error) {
        caught.push({
          message: error instanceof Error ? error.message : String(error),
          detail:
            error instanceof api.TtsSpeakError
              ? error.detail
              : null,
        });
      }
    }
    return caught;
  });

  expect(failures).toHaveLength(4);
  const serialized = JSON.stringify(failures);
  expect(serialized).not.toContain("/Users/alice");
  expect(serialized).not.toContain("super-secret");
  expect(failures[3]).toEqual({
    message: "语音播报服务暂时不可用，请稍后重试",
    detail: "tts_upstream_error",
  });
});

test("bounded bootstrap and identity invalid JSON expose only stable errors", async ({
  page,
}) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");
  const result = await page.evaluate(async () => {
    const canary = "/Users/alice/Private/identity.json token=super-secret";
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("https://identity-json.example");
    session.resetSessionForTest();
    const originalFetch = window.fetch;
    const originalWarn = console.warn;
    const warnings: string[] = [];
    console.warn = (...args: unknown[]) => {
      warnings.push(
        args
          .map((value) =>
            value instanceof Error
              ? `${value.name}:${value.message}:${String(value.cause ?? "")}`
              : String(value),
          )
          .join(" "),
      );
    };
    let mode: "bootstrap" | "identity" = "bootstrap";
    window.fetch = async (input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (url.endsWith("/bootstrap")) {
        if (mode === "bootstrap") {
          return new Response(`not-json ${canary}`, { status: 200 });
        }
        return new Response(
          JSON.stringify({
            schema_version: 1,
            api_version: "0.3",
            session_required: true,
            capabilities: { principal_sessions: true },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.endsWith("/session/enroll") || url.endsWith("/session/renew")) {
        return new Response(`invalid identity ${canary}`, { status: 200 });
      }
      return new Response(null, { status: 404 });
    };
    try {
      const bootstrap = await session.bootstrapBackend();
      mode = "identity";
      session.resetSessionForTest();
      let identityError = "";
      try {
        await session.ensureServerSession();
      } catch (error) {
        identityError =
          error instanceof Error
            ? `${error.name}:${error.message}:${String(error.cause ?? "")}`
            : String(error);
      }
      return { bootstrap, warnings, identityError };
    } finally {
      window.fetch = originalFetch;
      console.warn = originalWarn;
    }
  });

  expect(result.bootstrap).toBeNull();
  const serialized = JSON.stringify(result);
  expect(serialized).not.toContain("/Users/alice");
  expect(serialized).not.toContain("super-secret");
  expect(result.identityError).toContain("identity response is invalid JSON");
});

test("authenticatedFetch never sends backend bearer or retries cross-origin requests", async ({
  page,
}) => {
  let sessionCalls = 0;
  let externalCalls = 0;
  let externalAuthorization = "";
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/session/enroll"],
  });
  await page.route(/\/(api\/)?session\/enroll$/, (route) => {
    sessionCalls += 1;
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        token: "must-not-leak",
        expires_at: "2099-01-01T00:00:00Z",
      }),
    });
  });
  await page.route("https://outside.example.test/collect", (route) => {
    externalCalls += 1;
    externalAuthorization = route.request().headers().authorization ?? "";
    return route.fulfill({ status: 401, body: "not authorized" });
  });

  await page.goto("/");
  const baselineSessionCalls = sessionCalls;
  const status = await page.evaluate(async () => {
    const { authenticatedFetch, resetSessionForTest } = await import("/src/session.ts");
    resetSessionForTest();
    const response = await authenticatedFetch(
      "https://outside.example.test/collect",
      { headers: { Authorization: "caller-bearer-must-not-cross-origin" } },
    );
    return response.status;
  });

  expect(status).toBe(401);
  expect(externalCalls).toBe(1);
  expect(externalAuthorization).toBe("");
  expect(sessionCalls).toBe(baselineSessionCalls);
});

test("API transport caps declared and chunked successful response bodies", async ({
  page,
}) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const { apiTransport, ApiTransportError } = await import("/src/session.ts");
    const originalFetch = window.fetch.bind(window);
    let mode: "declared" | "chunked" = "declared";
    let cancelled = 0;
    window.fetch = async () => {
      if (mode === "declared") {
        return new Response(
          new ReadableStream<Uint8Array>({
            cancel() {
              cancelled += 1;
            },
          }),
          { status: 200, headers: { "Content-Length": "9" } },
        );
      }
      let pulls = 0;
      return new Response(
        new ReadableStream<Uint8Array>({
          pull(controller) {
            pulls += 1;
            controller.enqueue(new TextEncoder().encode("12345"));
            if (pulls > 3) controller.close();
          },
          cancel() {
            cancelled += 1;
          },
        }),
        { status: 200 },
      );
    };
    const failures: Array<{ typed: boolean; kind: string | null }> = [];
    try {
      try {
        await apiTransport(
          "/api/declared-oversize",
          {},
          { anonymous: true, maxResponseBytes: 8 },
        );
      } catch (error) {
        failures.push({
          typed: error instanceof ApiTransportError,
          kind: error instanceof ApiTransportError ? error.kind : null,
        });
      }
      mode = "chunked";
      try {
        const response = await apiTransport(
          "/api/chunked-oversize",
          {},
          { anonymous: true, maxResponseBytes: 8 },
        );
        await response.text();
      } catch (error) {
        failures.push({
          typed: error instanceof ApiTransportError,
          kind: error instanceof ApiTransportError ? error.kind : null,
        });
      }
      await Promise.resolve();
      return { failures, cancelled };
    } finally {
      window.fetch = originalFetch;
    }
  });

  expect(result.failures).toEqual([
    { typed: true, kind: "response-too-large" },
    { typed: true, kind: "response-too-large" },
  ]);
  expect(result.cancelled).toBe(2);
});
