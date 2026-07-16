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

test("收音选择冲突后刷新权威 revision，等待用户再次确认", async ({ page }) => {
  const updates: Array<Record<string, unknown>> = [];
  let control = {
    mode: "multi",
    selectedDeviceIds: ["device-mac"],
    revision: 12,
  };
  await installEchoMock(page, {
    skipPaths: ["/capture/control", "/capture/devices"],
  });
  await page.route("**/capture/devices", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
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
      if (updates.length === 1) {
        control = {
          mode: "single",
          selectedDeviceIds: ["device-win"],
          revision: 13,
        };
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({ detail: "capture control revision conflict" }),
        });
        return;
      }
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ ...body, revision: 14 }),
      });
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(control),
    });
  });

  await page.goto("/");
  await page.getByTestId("meeting-status-bar").click();
  await expect(page.getByText("选择收音设备")).toBeVisible();
  await page.getByLabel(/会议室 Windows/).check();
  await page.getByRole("button", { name: "开始会议" }).last().click();

  await expect(page.getByText("选择收音设备")).toBeVisible();
  await expect(page.getByText("收音选择已更新，请确认最新选择后重试")).toBeVisible();
  await expect(page.getByLabel("仅一台设备")).toBeChecked();
  await expect(page.getByLabel(/会议室 Windows/)).toBeChecked();
  expect(updates[0]).toEqual({
    mode: "multi",
    selectedDeviceIds: ["device-mac", "device-win"],
    expectedRevision: 12,
  });

  await page.getByRole("button", { name: "开始会议" }).last().click();
  await expect.poll(() => updates.length).toBe(2);
  expect(updates[1]).toEqual({
    mode: "single",
    selectedDeviceIds: ["device-win"],
    expectedRevision: 13,
  });
});

test("Android 收音选择冲突后刷新权威 revision，等待用户再次确认", async ({
  page,
}) => {
  const updates: Array<Record<string, unknown>> = [];
  let control = {
    mode: "multi",
    selectedDeviceIds: ["device-mac"],
    revision: 12,
  };
  await page.addInitScript(() => {
    (window as unknown as { androidBridge: Record<string, never> }).androidBridge = {};
    window.localStorage.setItem(
      "echodesk.mobileBackendBase",
      "http://192.168.50.10:8769",
    );
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
  });
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/capture/control", "/capture/devices"],
  });
  await page.route("**/capture/devices", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
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
      if (updates.length === 1) {
        control = {
          mode: "single",
          selectedDeviceIds: ["device-win"],
          revision: 13,
        };
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({ detail: "capture control revision conflict" }),
        });
        return;
      }
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ ...body, revision: 14 }),
      });
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(control),
    });
  });
  await page.route("**/capture/control/authorize", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ allowed: false, mode: "single", revision: 14 }),
    });
  });

  await page.goto("/");
  await page.getByTestId("meeting-status-bar").click();
  await expect(page.getByRole("dialog", { name: "选择收音设备" })).toBeVisible();
  await expect(page.getByLabel("多端收音")).toBeChecked();
  await expect(page.getByLabel(/办公室 Mac/)).toBeChecked();
  await expect(page.getByRole("button", { name: "确认并开始" })).toBeEnabled();
  await page.getByRole("button", { name: "确认并开始" }).click();

  await expect(page.getByRole("dialog", { name: "选择收音设备" })).toBeVisible();
  await expect(page.getByText("收音选择已更新，请确认最新选择后重试")).toBeVisible();
  await expect(page.getByLabel("单端收音")).toBeChecked();
  await expect(page.getByLabel(/会议室 Windows/)).toBeChecked();
  expect(updates[0]).toEqual({
    mode: "multi",
    selectedDeviceIds: ["device-mac"],
    expectedRevision: 12,
  });

  await page.getByRole("button", { name: "确认并开始" }).click();
  await expect.poll(() => updates.length).toBe(2);
  expect(updates[1]).toEqual({
    mode: "single",
    selectedDeviceIds: ["device-win"],
    expectedRevision: 13,
  });
});
