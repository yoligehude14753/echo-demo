import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("打开 App 只同步控制 revision，不自动请求麦克风", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { __getUserMediaCalls: number }).__getUserMediaCalls = 0;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        enumerateDevices: async () => [],
        getUserMedia: async () => {
          (window as unknown as { __getUserMediaCalls: number }).__getUserMediaCalls += 1;
          throw new Error("should not be called");
        },
      },
    });
  });
  await installEchoMock(page, { skipPaths: ["/capture/control"] });
  await page.route("**/capture/control", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        mode: "single",
        selectedDeviceIds: ["device-other"],
        revision: 7,
      }),
    });
  });
  await page.route("**/capture/control/authorize", async (route) => {
    const body = route.request().postDataJSON() as { revision: number };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        allowed: false,
        mode: "multi",
        revision: body.revision,
      }),
    });
  });

  await page.goto("/");
  await expect(page.getByTestId("meeting-item-ambient")).toContainText("待机");
  await expect
    .poll(() =>
      page.evaluate(
        () => (window as unknown as { __getUserMediaCalls: number }).__getUserMediaCalls,
      ),
    )
    .toBe(0);
});

test("多设备在线时点击开始弹框，并按 expectedRevision 提交 multi 选择", async ({
  page,
}) => {
  const updates: Array<Record<string, unknown>> = [];
  await installEchoMock(page, {
    skipPaths: ["/capture/control", "/capture/devices"],
  });
  await page.route("**/capture/devices", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        control: { mode: "single", selectedDeviceIds: [], revision: 12 },
        devices: [
          {
            deviceId: "device-mac",
            displayName: "办公室 Mac",
            platform: "macos",
            online: true,
          },
          {
            deviceId: "device-win",
            displayName: "会议室 Windows",
            platform: "windows",
            online: true,
          },
        ],
      }),
    });
  });
  await page.route("**/capture/control", async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      updates.push(body);
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ ...body, revision: 13 }),
      });
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        mode: "single",
        selectedDeviceIds: [],
        revision: 12,
      }),
    });
  });
  await page.route("**/capture/control/authorize", async (route) => {
    const body = route.request().postDataJSON() as { revision: number };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        allowed: false,
        mode: "multi",
        revision: body.revision,
      }),
    });
  });

  await page.goto("/");
  await page.getByTestId("meeting-status-bar").click();
  await expect(page.getByText("选择收音设备")).toBeVisible();
  await page.getByLabel("多台设备同时收音").check();
  await page.getByLabel(/办公室 Mac/).check();
  await page.getByLabel(/会议室 Windows/).check();
  await page.getByRole("button", { name: "开始会议" }).last().click();

  await expect.poll(() => updates.length).toBe(1);
  expect(updates[0]).toEqual({
    mode: "multi",
    selectedDeviceIds: ["device-mac", "device-win"],
    expectedRevision: 12,
  });
});
