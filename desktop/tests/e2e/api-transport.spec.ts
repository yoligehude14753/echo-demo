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
    route.fulfill({ status: 503, contentType: "text/plain", body: "provider unavailable" }),
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
    detail: "provider unavailable",
  });
  expect(result.timeout).toEqual({ typed: true, kind: "timeout" });
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
    const response = await authenticatedFetch("https://outside.example.test/collect");
    return response.status;
  });

  expect(status).toBe(401);
  expect(externalCalls).toBe(1);
  expect(externalAuthorization).toBe("");
  expect(sessionCalls).toBe(baselineSessionCalls);
});
