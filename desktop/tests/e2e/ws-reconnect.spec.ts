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
