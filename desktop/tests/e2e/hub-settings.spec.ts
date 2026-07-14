import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("desktop Hub pairing code and device binding controls work", async ({ page }) => {
  let pairingCode = "ABCD-1234";
  let devices = [
    {
      device_id: "desktop-1",
      name: "EchoDesk PC",
      platform: "darwin",
      status: "online",
      is_current: true,
      last_seen_at: "2026-07-14T12:00:00Z",
    },
    {
      device_id: "android-1",
      name: "EchoDesk Android",
      platform: "android",
      status: "online",
      is_current: false,
      last_seen_at: "2026-07-14T12:00:00Z",
    },
  ];

  await page.route(/\/(api\/)?hub\/status$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        enabled: true,
        configured: true,
        device_id: "desktop-1",
        paired: true,
        connection: "connected",
        pairing_code: pairingCode,
        pairing_expires_at: "2026-07-14T13:00:00Z",
        devices,
        last_sync_at: "2026-07-14T12:30:00Z",
        last_connected_at: "2026-07-14T12:29:00Z",
        last_error: null,
      }),
    });
  });
  await page.route(/\/(api\/)?hub\/pairings$/, async (route) => {
    expect(route.request().method()).toBe("POST");
    pairingCode = "EFGH-5678";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ pairing_code: pairingCode, expires_at: "2026-07-14T13:00:00Z" }),
    });
  });
  await page.route(/\/(api\/)?hub\/devices\/android-1$/, async (route) => {
    expect(route.request().method()).toBe("DELETE");
    devices = devices.filter((device) => device.device_id !== "android-1");
    await route.fulfill({ status: 204 });
  });

  await installEchoMock(page, {
    skipPaths: ["/hub/status", "/hub/pairings", "/hub/devices/android-1"],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByTestId("open-settings").click();

  const section = page.getByTestId("hub-settings-section");
  await expect(section).toBeVisible();
  await expect(page.getByTestId("hub-connection-status")).toContainText("已连接");
  await expect(page.getByTestId("hub-device-id")).toContainText("desktop-1");
  await expect(page.getByTestId("hub-last-sync")).not.toContainText("尚未同步");
  await expect(section).toContainText("EchoDesk Android");

  await page.getByTestId("hub-generate-pairing").click();
  await expect(page.getByTestId("hub-pairing-code")).toContainText("EFGH-5678");
  await expect(page.getByTestId("hub-copy-pairing")).toBeEnabled();

  await page.getByTestId("hub-revoke-android-1").click();
  await page.getByRole("button", { name: "解除绑定" }).click();
  await expect(section).not.toContainText("EchoDesk Android");
});
