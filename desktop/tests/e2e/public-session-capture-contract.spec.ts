import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

function bootstrapBody(): string {
  return JSON.stringify({
    schema_version: 1,
    api_version: "0.3",
    backend_version: "0.3.5-test",
    session_required: true,
    capabilities: {
      principal_sessions: true,
      owner_isolation: true,
      workflow_kernel: "dispatcher-v1",
      ws_owner_filtering: true,
      ws_stream_epoch: true,
      server_resync_rehydrate_required: true,
      host_runtime_requires_admin: true,
    },
  });
}

test("public renderer establishes session before capture requests", async ({ page }) => {
  const calls: string[] = [];
  const requestOrigins: string[] = [];
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: [
      "/bootstrap",
      "/session/enroll",
      "/session/renew",
      "/capture/control",
      "/capture/devices",
    ],
  });
  await page.route(/\/(api\/)?bootstrap$/, (route) => {
    calls.push("/bootstrap");
    requestOrigins.push(new URL(route.request().url()).origin);
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(),
    });
  });
  await page.route(/\/(api\/)?session\/(enroll|renew)$/, (route) => {
    calls.push(route.request().url().includes("/enroll") ? "/session/enroll" : "/session/renew");
    requestOrigins.push(new URL(route.request().url()).origin);
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        token: "public-renderer-session-token",
        expires_at: "2099-01-01T00:00:00Z",
        principal: {
          tenant_id: "tenant-test",
          owner_id: "owner-test",
          device_id: "device-test",
          session_id: "session-test",
          mode: "public",
        },
      }),
    });
  });
  await page.route(/\/(api\/)?capture\/control$/, (route) => {
    calls.push("/capture/control");
    requestOrigins.push(new URL(route.request().url()).origin);
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ mode: "single", selectedDeviceIds: [], revision: 1 }),
    });
  });
  await page.route(/\/(api\/)?capture\/devices$/, (route) => {
    calls.push("/capture/devices");
    requestOrigins.push(new URL(route.request().url()).origin);
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ devices: [] }),
    });
  });

  await page.goto("/");
  const sessionReady = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const token = await session.ensureServerSession();
    return typeof token === "string" && token.length > 0;
  });
  expect(sessionReady).toBe(true);
  const sessionReadyIndex = calls.lastIndexOf("/session/enroll");
  const snapshot = await page.evaluate(async () => {
    const api = await import("/src/api.ts");
    return api.getCaptureDevices();
  });

  expect(snapshot.devices).toEqual([]);
  expect(new Set(requestOrigins)).toEqual(new Set(["https://localhost:5174"]));
  const firstCaptureIndex = calls.findIndex((path) => path.startsWith("/capture/"));
  expect(sessionReadyIndex).toBeGreaterThanOrEqual(0);
  expect(sessionReadyIndex).toBeLessThan(firstCaptureIndex);
  expect(calls.slice(firstCaptureIndex).filter((path) => path.startsWith("/capture/"))).toEqual(
    expect.arrayContaining(["/capture/control", "/capture/devices"]),
  );
  expect(calls.filter((path) => path === "/session/enroll")).toHaveLength(1);
  expect(calls.filter((path) => path === "/session/renew")).toHaveLength(0);
});
