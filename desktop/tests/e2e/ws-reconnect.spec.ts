/**
 * E2E #3：WS 抖动恢复。
 *
 * 流程：
 * - 启动 → 已连接
 * - mock 关掉 ws → "断线" 显示
 * - 等几秒（前端用指数退避自动重连）→ 重新"已连接"
 */
import { test, expect } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("WS 断开后前端能自动重连", async ({ page }) => {
  const mock = await installEchoMock(page);
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  // 1. 关掉 ws
  await mock.closeWs(1006, "abnormal");
  await expect(page.locator("text=断线")).toBeVisible({ timeout: 5_000 });

  // 2. 允许重连（前端 onclose 后会 backoff 重连，由于 MockWebSocket 是新 instance，
  //    自动 open → onopen → 自动回 server_hello）
  await mock.reopenWs();
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 15_000 });
});

for (const frame of [
  { name: "oversized UTF-8", kind: "oversized" },
  { name: "non-string", kind: "binary" },
] as const) {
  test(`WS ${frame.name} frame is closed before application payload handling`, async ({
    page,
  }) => {
    await installEchoMock(page);
    await page.goto("/");
    await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

    const result = await page.evaluate((frameKind) => {
      const ctrl = (
        window as unknown as {
          __echoMock__: {
            ws: {
              onmessage?: ((event: MessageEvent) => void) | null;
              close(code?: number, reason?: string): void;
            } | null;
            wsClosed: boolean;
          };
        }
      ).__echoMock__;
      let closeCode: number | null = null;
      let closeReason = "";
      if (!ctrl.ws) throw new Error("mock websocket missing");
      const socket = ctrl.ws;
      const originalClose = socket.close.bind(socket);
      socket.close = (code?: number, reason?: string) => {
        closeCode = code ?? null;
        closeReason = reason ?? "";
        originalClose(code, reason);
      };
      const data =
        frameKind === "oversized"
          // Character count stays below the cap; UTF-8 bytes exceed it.
          ? "界".repeat(Math.floor((1024 * 1024) / 3) + 1)
          : new Uint8Array([123, 125]).buffer;
      socket.onmessage?.(new MessageEvent("message", { data }));
      return { closeCode, closeReason, wsClosed: ctrl.wsClosed };
    }, frame.kind);

    expect(result).toEqual({
      closeCode: 4008,
      closeReason: "invalid or oversized event frame",
      wsClosed: true,
    });
    await expect(page.locator("text=断线")).toBeVisible({ timeout: 5_000 });
  });
}

test("backend origin change replaces the event source and ignores stale 4426", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "echodesk.mobileBackendBase",
      "https://identity-a.example",
    );
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
  });
  await installEchoMock(page);
  await page.goto("/");
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __echoMock__: { wsUrl?: string; wsCreated: number };
            }
          ).__echoMock__,
      ),
    )
    .toMatchObject({ wsUrl: "wss://identity-a.example/ws/echo" });

  const socketsAtA = await page.evaluate(
    () =>
      (
        window as unknown as { __echoMock__: { wsCreated: number } }
      ).__echoMock__.wsCreated,
  );

  await page.evaluate(async () => {
    const ctrl = (
      window as unknown as {
        __echoMock__: {
          ws: { onclose?: ((event: CloseEvent) => void) | null } | null;
          staleWs?: { onclose?: ((event: CloseEvent) => void) | null } | null;
        };
      }
    ).__echoMock__;
    ctrl.staleWs = ctrl.ws;
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase("https://identity-b.example");
  });

  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __echoMock__: { wsUrl?: string; wsCreated: number };
            }
          ).__echoMock__,
      ),
    )
    .toMatchObject({ wsUrl: "wss://identity-b.example/ws/echo" });

  const socketsAtB = await page.evaluate(
    () =>
      (
        window as unknown as { __echoMock__: { wsCreated: number } }
      ).__echoMock__.wsCreated,
  );
  expect(socketsAtB).toBeGreaterThan(socketsAtA);

  const state = await page.evaluate(async () => {
    const ctrl = (
      window as unknown as {
        __echoMock__: {
          wsUrl?: string;
          wsCreated: number;
          staleWs?: { onclose?: ((event: CloseEvent) => void) | null } | null;
        };
      }
    ).__echoMock__;
    ctrl.staleWs?.onclose?.(
      new CloseEvent("close", {
        code: 4426,
        reason: "client upgrade required:9.9.9",
      }),
    );
    await new Promise<void>((resolve) =>
      window.requestAnimationFrame(() =>
        window.requestAnimationFrame(() => resolve()),
      ),
    );
    const session = await import("/src/session.ts");
    return {
      compatibility: session.backendCompatibility(),
      identity: session.currentSessionIdentityStatus(),
      wsUrl: ctrl.wsUrl,
      wsCreated: ctrl.wsCreated,
    };
  });

  expect(state).toEqual({
    compatibility: "compatible",
    identity: { phase: "idle", message: null },
    wsUrl: "wss://identity-b.example/ws/echo",
    wsCreated: socketsAtB,
  });
});

test("late 4401 renewal from origin A cannot block origin B reconnect", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "echodesk.mobileBackendBase",
      "https://identity-a.example",
    );
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
    const state: {
      ensureOrigins: string[];
      renewCalls: number;
      renewSettled: boolean;
      releaseRenew: (() => void) | null;
    } = {
      ensureOrigins: [],
      renewCalls: 0,
      renewSettled: false,
      releaseRenew: null,
    };
    (
      window as unknown as { __wsOriginRenewState__: typeof state }
    ).__wsOriginRenewState__ = state;
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      ensurePublicSession: async () => {
        const origin = new URL(
          window.localStorage.getItem("echodesk.mobileBackendBase") ??
            window.location.origin,
        ).origin;
        state.ensureOrigins.push(origin);
        return {
          token: origin.includes("identity-b") ? "ws-b-token" : "ws-a-token",
          expires_at: "2099-01-01T00:00:00Z",
        };
      },
      renewPublicSession: async () => {
        state.renewCalls += 1;
        const result = await new Promise<null>((resolve) => {
          state.releaseRenew = () => resolve(null);
        });
        state.renewSettled = true;
        return result;
      },
    };
  });
  const mock = await installEchoMock(page, { skipPaths: ["/bootstrap"] });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(true),
    }),
  );

  await page.goto("/");
  await expect
    .poll(async () => mock.wsSent())
    .toContainEqual(expect.stringContaining("ws-a-token"));
  await mock.closeWs(4401, "origin A session expired");
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __wsOriginRenewState__: { renewCalls: number };
            }
          ).__wsOriginRenewState__.renewCalls,
      ),
    )
    .toBe(1);

  await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase("https://identity-b.example");
    (
      window as unknown as { __echoMock__: { wsClosed: boolean } }
    ).__echoMock__.wsClosed = false;
  });
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __echoMock__: { wsUrl?: string };
            }
          ).__echoMock__.wsUrl,
      ),
    )
    .toBe("wss://identity-b.example/ws/echo");
  await expect
    .poll(async () => mock.wsSent())
    .toContainEqual(expect.stringContaining("ws-b-token"));
  const socketsAtB = await page.evaluate(
    () =>
      (
        window as unknown as { __echoMock__: { wsCreated: number } }
      ).__echoMock__.wsCreated,
  );

  await page.evaluate(() => {
    (
      window as unknown as {
        __wsOriginRenewState__: { releaseRenew: (() => void) | null };
      }
    ).__wsOriginRenewState__.releaseRenew?.();
  });
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __wsOriginRenewState__: { renewSettled: boolean };
            }
          ).__wsOriginRenewState__.renewSettled,
      ),
    )
    .toBe(true);
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        queueMicrotask(() => queueMicrotask(resolve)),
      ),
  );

  await mock.closeWs(1006, "origin B reconnect probe");
  await mock.reopenWs();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as { __echoMock__: { wsCreated: number } }
          ).__echoMock__.wsCreated,
      ),
      { timeout: 15_000 },
    )
    .toBeGreaterThan(socketsAtB);
  const finalState = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    return {
      identity: session.currentSessionIdentityStatus(),
      wsUrl: (
        window as unknown as { __echoMock__: { wsUrl?: string } }
      ).__echoMock__.wsUrl,
    };
  });
  expect(finalState).toEqual({
    identity: { phase: "ready", message: null },
    wsUrl: "wss://identity-b.example/ws/echo",
  });
});

test("WS 4401 single-flights renewal and reconnects with the new token", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const state: {
      ensureCalls: number;
      renewCalls: number;
      releaseRenew: (() => void) | null;
    } = { ensureCalls: 0, renewCalls: 0, releaseRenew: null };
    (
      window as unknown as { __wsAuthState__: typeof state }
    ).__wsAuthState__ = state;
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      ensurePublicSession: async () => {
        state.ensureCalls += 1;
        return {
          token: "ws-old-token",
          expires_at: "2099-01-01T00:00:00Z",
        };
      },
      renewPublicSession: () => {
        state.renewCalls += 1;
        return new Promise((resolve) => {
          state.releaseRenew = () =>
            resolve({
              token: "ws-new-token",
              expires_at: "2099-01-01T00:00:00Z",
            });
        });
      },
    };
  });
  const mock = await installEchoMock(page, { skipPaths: ["/bootstrap"] });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(true),
    }),
  );

  await page.goto("/");
  await expect
    .poll(async () => mock.wsSent())
    .toContainEqual(expect.stringContaining("ws-old-token"));
  await mock.closeWs(4401, "session expired");
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __wsAuthState__: { renewCalls: number };
            }
          ).__wsAuthState__.renewCalls,
      ),
    )
    .toBe(1);

  await page.evaluate(async () => {
    const { ensureServerSession } = await import("/src/session.ts");
    (
      window as unknown as { __joinedForceRenew__?: Promise<unknown> }
    ).__joinedForceRenew__ = Promise.all([
      ensureServerSession(true),
      ensureServerSession(true),
    ]);
  });
  await mock.reopenWs();
  await page.evaluate(() => {
    (
      window as unknown as {
        __wsAuthState__: { releaseRenew: (() => void) | null };
      }
    ).__wsAuthState__.releaseRenew?.();
  });
  await page.evaluate(async () => {
    await (
      window as unknown as { __joinedForceRenew__?: Promise<unknown> }
    ).__joinedForceRenew__;
  });

  await expect
    .poll(async () => mock.wsSent())
    .toContainEqual(expect.stringContaining("ws-new-token"));
  const state = await page.evaluate(
    () =>
      (
        window as unknown as {
          __wsAuthState__: { ensureCalls: number; renewCalls: number };
        }
      ).__wsAuthState__,
  );
  expect(state.ensureCalls).toBe(1);
  expect(state.renewCalls).toBe(1);
});

test("WS 4401 identity-lost stops reconnecting and shows an explicit state", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const state = { ensureCalls: 0, renewCalls: 0 };
    (
      window as unknown as { __wsLostState__: typeof state }
    ).__wsLostState__ = state;
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      ensurePublicSession: async () => {
        state.ensureCalls += 1;
        return {
          token: "ws-expiring-token",
          expires_at: "2099-01-01T00:00:00Z",
        };
      },
      renewPublicSession: async () => {
        state.renewCalls += 1;
        return null;
      },
    };
  });
  const mock = await installEchoMock(page, { skipPaths: ["/bootstrap"] });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(true),
    }),
  );

  await page.goto("/");
  await expect
    .poll(async () => mock.wsSent())
    .toContainEqual(expect.stringContaining("ws-expiring-token"));
  await page.clock.install();
  await mock.closeWs(4401, "identity lost");
  await mock.reopenWs();
  await expect(page.getByTestId("identity-status-lost")).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.dataset.sessionIdentity),
  ).toBe("identity-lost");

  await page.clock.runFor(9_000);
  const frames = await mock.wsSent();
  expect(frames.filter((frame) => frame.includes("client_hello"))).toHaveLength(1);
  const state = await page.evaluate(
    () =>
      (
        window as unknown as {
          __wsLostState__: { ensureCalls: number; renewCalls: number };
        }
      ).__wsLostState__,
  );
  expect(state).toEqual({ ensureCalls: 1, renewCalls: 1 });
});

test("WS identity-lost explicitly reconnects the same owner with one new client hello", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const principal = {
      tenant_id: "tenant-a",
      device_id: "device-a",
      owner_id: "owner-a",
      session_id: "session-a",
      mode: "public",
    };
    const state = { ensureCalls: 0, renewCalls: 0, recover: false };
    (
      window as unknown as { __wsExplicitRecovery__: typeof state }
    ).__wsExplicitRecovery__ = state;
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      ensurePublicSession: async () => {
        state.ensureCalls += 1;
        return {
          token: "ws-owner-a-old-token",
          expires_at: "2099-01-01T00:00:00Z",
          principal,
        };
      },
      renewPublicSession: async () => {
        state.renewCalls += 1;
        if (!state.recover) return null;
        return {
          token: "ws-owner-a-new-token",
          expires_at: "2099-01-01T00:00:00Z",
          principal,
        };
      },
    };
  });
  const mock = await installEchoMock(page, { skipPaths: ["/bootstrap"] });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(true),
    }),
  );

  await page.goto("/");
  await expect
    .poll(async () => mock.wsSent())
    .toContainEqual(expect.stringContaining("ws-owner-a-old-token"));

  await mock.closeWs(4401, "identity lost");
  await expect(page.getByTestId("identity-status-lost")).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __wsExplicitRecovery__: { renewCalls: number };
            }
          ).__wsExplicitRecovery__.renewCalls,
      ),
    )
    .toBe(1);

  await page.evaluate(() => {
    (
      window as unknown as {
        __wsExplicitRecovery__: { recover: boolean };
      }
    ).__wsExplicitRecovery__.recover = true;
  });
  await mock.reopenWs();
  await page.getByTestId("identity-status-lost").click();

  await expect
    .poll(async () => mock.wsSent())
    .toContainEqual(expect.stringContaining("ws-owner-a-new-token"));
  await expect(page.locator("text=已连接")).toBeVisible();

  const result = await page.evaluate(() => ({
    state: (
      window as unknown as {
        __wsExplicitRecovery__: {
          ensureCalls: number;
          renewCalls: number;
        };
      }
    ).__wsExplicitRecovery__,
    cursorKeys: Object.keys(window.localStorage).filter((key) =>
      key.startsWith("echodesk.wsCursor.v1:"),
    ),
  }));
  const clientHellos = (await mock.wsSent())
    .filter((frame) => frame.includes("client_hello"))
    .map((frame) => JSON.parse(frame) as { auth?: { token?: string } });
  expect(clientHellos).toHaveLength(2);
  expect(clientHellos.map((frame) => frame.auth?.token)).toEqual([
    "ws-owner-a-old-token",
    "ws-owner-a-new-token",
  ]);
  expect(result.state).toEqual({ ensureCalls: 1, renewCalls: 2, recover: true });
  expect(result.cursorKeys).toHaveLength(1);
  expect(result.cursorKeys[0]).toContain("tenant-a:owner-a");
});

function bootstrapBody(sessionRequired: boolean): string {
  return JSON.stringify({
    schema_version: 1,
    api_version: "0.3",
    backend_version: "0.3.1-test",
    session_required: sessionRequired,
    capabilities: { principal_sessions: true },
  });
}
