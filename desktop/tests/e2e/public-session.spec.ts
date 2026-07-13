import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("public client keeps the device credential out of renderer storage and WS URLs", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const credentialState = { ensureCalls: 0, renewCalls: 0 };
    (
      window as unknown as {
        __credentialState__: typeof credentialState;
      }
    ).__credentialState__ = credentialState;
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      ensurePublicSession: async () => {
        credentialState.ensureCalls += 1;
        return {
          token: "session-from-server",
          expires_at: "2099-01-01T00:00:00Z",
          principal: {
            tenant_id: "tenant-test",
            device_id: "device-test",
            owner_id: "owner-test",
            session_id: "session-test",
            mode: "public",
          },
        };
      },
      renewPublicSession: async () => {
        credentialState.renewCalls += 1;
        return null;
      },
      clearPublicCredential: async () => {
        return { cleared: true };
      },
    };
  });
  const mock = await installEchoMock(page, {
    skipPaths: ["/bootstrap", "/session", "/meetings?", "/anonymous-meta-probe"],
  });

  let meetingsAuthorization = "";
  let meetingsClientVersion = "";
  let anonymousMetaAuthorization = "";
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        backend_version: "0.3.1-test",
        session_required: true,
        capabilities: {
          principal_sessions: true,
          owner_isolation: true,
          workflow_kernel: "dispatcher-v1",
          ws_owner_filtering: true,
          server_resync_rehydrate_required: true,
          host_runtime_requires_admin: true,
        },
      }),
    }),
  );
  await page.route(/\/(api\/)?meetings\?limit=/, (route) => {
    meetingsAuthorization = route.request().headers()["authorization"] ?? "";
    meetingsClientVersion =
      route.request().headers()["x-echodesk-client-version"] ?? "";
    return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
  await page.route(/\/(api\/)?anonymous-meta-probe$/, (route) => {
    anonymousMetaAuthorization = route.request().headers().authorization ?? "";
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: '{"status":"ok"}',
    });
  });

  await page.goto("/");
  await expect.poll(() => meetingsAuthorization).toBe("Bearer session-from-server");
  expect(meetingsClientVersion).toBe("0.3.2");
  const wsUrl = await page.evaluate(
    () =>
      (
        window as unknown as {
          __echoMock__: { wsUrl?: string };
        }
      ).__echoMock__.wsUrl ?? "",
  );
  expect(new URL(wsUrl).search).toBe("");
  await expect
    .poll(async () => {
      const frames = await mock.wsSent();
      return frames.map((frame) => JSON.parse(frame) as Record<string, unknown>);
    })
    .toContainEqual(
      expect.objectContaining({
        type: "client_hello",
        client_version: "0.3.2",
        auth: { type: "bearer", token: "session-from-server" },
      }),
    );
  const credentialState = await page.evaluate(() => ({
    localStorageCredential: window.localStorage.getItem("echodesk.serverSession.v1"),
    bridge: (
      window as unknown as {
        __credentialState__: { ensureCalls: number; renewCalls: number };
      }
    ).__credentialState__,
  }));
  expect(credentialState.localStorageCredential).toBeNull();
  expect(credentialState.bridge).toEqual({
    ensureCalls: 1,
    renewCalls: 0,
  });

  const anonymousStatus = await page.evaluate(async () => {
    const { apiTransport } = await import("/src/session.ts");
    const response = await apiTransport(
      "/api/anonymous-meta-probe",
      { cache: "no-store" },
      { anonymous: true },
    );
    return response.status;
  });
  expect(anonymousStatus).toBe(200);
  expect(anonymousMetaAuthorization).toBe("");

  await expect
    .poll(async () => {
      const urls = (await mock.fetchLog()).map((entry) => new URL(entry.url, page.url()).pathname);
      return urls.some((path) => path === "/healthz" || path === "/api/healthz");
    })
    .toBe(true);
  const healthPaths = (await mock.fetchLog()).map(
    (entry) => new URL(entry.url, page.url()).pathname,
  );
  expect(healthPaths).not.toContain("/healthz/full");
  expect(healthPaths).not.toContain("/api/healthz/full");
});

test("401 renewal failure is identity-lost and never enrolls a replacement owner", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const state = { ensureCalls: 0, renewCalls: 0 };
    (window as unknown as { __identityLostState__: typeof state }).__identityLostState__ = state;
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      ensurePublicSession: async () => {
        state.ensureCalls += 1;
        return { token: "old-owner-token", expires_at: "2099-01-01T00:00:00Z" };
      },
      renewPublicSession: async () => {
        state.renewCalls += 1;
        return null;
      },
    };
  });
  await installEchoMock(page, { skipPaths: ["/bootstrap", "/identity-lost"] });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        session_required: true,
        capabilities: { principal_sessions: true },
      }),
    }),
  );
  await page.route(/\/(api\/)?identity-lost$/, (route) =>
    route.fulfill({ status: 401, body: "expired" }),
  );

  await page.goto("/");
  const result = await page.evaluate(async () => {
    const { apiTransport } = await import("/src/session.ts");
    try {
      await apiTransport("/api/identity-lost");
      return { kind: "", message: "" };
    } catch (error) {
      return {
        kind: (error as { kind?: string }).kind ?? "",
        message: error instanceof Error ? error.message : String(error),
      };
    }
  });
  const state = await page.evaluate(
    () =>
      (window as unknown as {
        __identityLostState__: { ensureCalls: number; renewCalls: number };
      }).__identityLostState__,
  );
  expect(result.kind).toBe("identity-lost");
  expect(result.message).toContain("不会自动创建");
  expect(state).toEqual({ ensureCalls: 1, renewCalls: 1 });
});

test("401 renews through the stable Electron credential without enrolling a new device", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const state = { allowRenew: false, ensureCalls: 0, renewCalls: 0 };
    (window as unknown as { __credentialRenewState__: typeof state }).__credentialRenewState__ =
      state;
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      ensurePublicSession: async () => {
        state.ensureCalls += 1;
        return {
          token: "enrolled-session-token",
          expires_at: "2099-01-01T00:00:00Z",
        };
      },
      renewPublicSession: async () => {
        state.renewCalls += 1;
        if (!state.allowRenew) return null;
        return {
          token: "renewed-session-token",
          expires_at: "2099-01-01T00:00:00Z",
        };
      },
      clearPublicCredential: async () => ({ cleared: true }),
    };
  });
  await installEchoMock(page, {
    skipPaths: ["/bootstrap", "/session", "/credential-renew-test"],
  });

  const authorizationAttempts: string[] = [];
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
  await page.route(/\/(api\/)?credential-renew-test$/, (route) => {
    const authorization = route.request().headers().authorization ?? "";
    authorizationAttempts.push(authorization);
    return route.fulfill({
      status: authorization === "Bearer renewed-session-token" ? 200 : 401,
      contentType: "application/json",
      body: JSON.stringify({ ok: authorization === "Bearer renewed-session-token" }),
    });
  });

  await page.goto("/");
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __credentialRenewState__: { ensureCalls: number };
            }
          ).__credentialRenewState__.ensureCalls,
      ),
    )
    .toBe(1);
  await page.evaluate(() => {
    (
      window as unknown as {
        __credentialRenewState__: { allowRenew: boolean };
      }
    ).__credentialRenewState__.allowRenew = true;
  });
  const status = await page.evaluate(async () => {
    const { apiTransport } = await import("/src/session.ts");
    return (await apiTransport("/api/credential-renew-test")).status;
  });

  expect(status).toBe(200);
  expect(authorizationAttempts).toEqual([
    "Bearer enrolled-session-token",
    "Bearer renewed-session-token",
  ]);
  const state = await page.evaluate(
    () =>
      (
        window as unknown as {
          __credentialRenewState__: { ensureCalls: number; renewCalls: number };
        }
      ).__credentialRenewState__,
  );
  expect(state).toEqual({
    allowRenew: true,
    ensureCalls: 1,
    renewCalls: 1,
  });
  expect(
    await page.evaluate(() => window.localStorage.getItem("echodesk.serverSession.v1")),
  ).toBeNull();
});
