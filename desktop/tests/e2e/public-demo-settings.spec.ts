import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("公网 demo 设置页：admin 禁用时仍可查看和修改移动端后端地址", async ({ page }) => {
  await page.route(/\/(api\/)?admin\/(data-dir|settings\/remote)$/, async (route) => {
    await route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "admin endpoints are disabled in public demo mode" }),
    });
  });
  await page.route(/\/(api\/)?workspace\/status$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        configured_dirs: [],
        authorized_dirs: [],
        n_indexed: 0,
        max_file_mb: 100,
        scan_on_startup: true,
      }),
    });
  });

  await installEchoMock(page, {
    skipPaths: ["/admin/data-dir", "/admin/settings/remote", "/workspace/status"],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.getByTestId("open-settings").click();
  await expect(page.getByText("公网 demo backend 不开放本机数据目录")).toBeVisible();
  await expect(page.getByTestId("mobile-backend-base")).toHaveValue(
    "https://echodesk.yoliyoli.uk",
  );
  await expect(page.getByTestId("remote-settings-form")).toBeHidden();
});

test("公网 demo 启动会清理旧历史状态和非显式后端地址", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { echo?: Record<string, unknown> }).echo = {
      isElectron: true,
      isPublicDemo: true,
    };
    window.localStorage.setItem("echodesk.mobileBackendBase", "http://10.10.12.32:8769");
    window.localStorage.setItem("echodesk.currentMeetingId", "m-old");
    window.localStorage.setItem("echodesk.capture.recent", "[{\"text\":\"old\"}]");
  });
  const mock = await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase")),
  ).toBeNull();
  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.currentMeetingId")),
  ).toBeNull();
  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.capture.recent")),
  ).toBeNull();
  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.publicDataBoundary.v2")),
  ).toContain('"schema":3');

  const fetchLog = await mock.fetchLog();
  expect(fetchLog.some((r) => /\/(api\/)?meetings\?/.test(r.url))).toBe(false);
  expect(fetchLog.some((r) => /\/(api\/)?capture\/recent/.test(r.url))).toBe(false);
});

test("公网 demo 已完成数据边界迁移后不会在每次启动清空本机历史", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { echo?: Record<string, unknown> }).echo = {
      isElectron: true,
      isPublicDemo: true,
    };
    window.localStorage.setItem(
      "echodesk.publicDataBoundary.v2",
      JSON.stringify({ schema: 3, appVersion: "0.2.22" }),
    );
    window.localStorage.setItem("echodesk.currentMeetingId", "m-local-after-migration");
    window.localStorage.setItem(
      "echodesk.capture.recent",
      JSON.stringify([{ text: "迁移后的本机转写" }]),
    );
    window.localStorage.setItem(
      "echodesk.localCaptureState.v1",
      JSON.stringify({
        schema: 1,
        appVersion: "0.2.19",
        savedAt: new Date().toISOString(),
        currentMeetingId: "m-local-after-migration",
        meetings: [],
        ambientSegments: [{ text: "迁移后的本机转写", captured_at: new Date().toISOString(), speaker_id: null, speaker_label: null, duration_ms: 0 }],
        artifacts: [],
      }),
    );
  });
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.currentMeetingId")),
  ).toBe("m-local-after-migration");
  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.capture.recent")),
  ).toContain("迁移后的本机转写");
  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.localCaptureState.v1")),
  ).toContain("迁移后的本机转写");
});

test("公网 demo 显式保存过的自定义后端会保留，并允许加载私有历史", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { echo?: Record<string, unknown> }).echo = {
      isElectron: true,
      isPublicDemo: true,
    };
    window.localStorage.setItem("echodesk.mobileBackendBase", "http://10.10.12.32:8769");
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
  });
  const mock = await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase")),
  ).toBe("http://10.10.12.32:8769");

  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("mobile-backend-base")).toHaveValue("http://10.10.12.32:8769");

  const fetchLog = await mock.fetchLog();
  expect(fetchLog.some((r) => /\/(api\/)?meetings\?/.test(r.url))).toBe(true);
});

test("backend 版本落后时顶部和设置页都显式警告", async ({ page }) => {
  await page.route(/\/(api\/)?healthz\/full$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        backend: { ok: true, version: "0.2.5", port: 8769, uptime_s: 12.3 },
        db: { ok: true },
        remote: {},
        mic: { ok: "unknown" },
      }),
    });
  });
  await installEchoMock(page, { skipPaths: ["/healthz/full"] });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.getByTestId("pill-backend").click();
  await expect(page.getByTestId("backend-version-warning")).toContainText(
    "远程 backend 还是 v0.2.5",
  );
  await page.keyboard.press("Escape");

  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("settings-backend-version")).toContainText(
    "落后于客户端",
  );
});

test("设置页：检查更新会展示当前平台优选 release 资产", async ({ page }) => {
  await page.route(
    "https://api.github.com/repos/yoligehude14753/echo-demo/releases/latest",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          tag_name: "v0.2.28",
          name: "EchoDesk v0.2.28",
          html_url: "https://github.com/yoligehude14753/echo-demo/releases/tag/v0.2.28",
          assets: [
            {
              name: "EchoDesk.Setup.0.2.28.exe",
              size: 123,
              browser_download_url:
                "https://github.com/yoligehude14753/echo-demo/releases/download/v0.2.28/EchoDesk.Setup.0.2.28.exe",
            },
            {
              name: "EchoDesk-0.2.28-smart-tv.apk",
              size: 456,
              browser_download_url:
                "https://github.com/yoligehude14753/echo-demo/releases/download/v0.2.28/EchoDesk-0.2.28-smart-tv.apk",
            },
            {
              name: "EchoDesk-0.2.28-arm64.dmg",
              size: 789,
              browser_download_url:
                "https://github.com/yoligehude14753/echo-demo/releases/download/v0.2.28/EchoDesk-0.2.28-arm64.dmg",
            },
          ],
        }),
      });
    },
  );
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("updates-section")).toBeVisible();
  await page.getByTestId("check-updates").click();

  await expect(page.getByTestId("update-status-tag")).toContainText("发现新版本");
  await expect(page.getByTestId("updates-section").getByText("v0.2.28")).toBeVisible();
  await expect(page.getByTestId("update-asset-name")).toContainText(
    "EchoDesk.Setup.0.2.28.exe",
  );
  await expect(page.getByTestId("install-update")).toBeEnabled();
});

test("TV 模式检查更新优先展示 smart-tv APK", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window.navigator, "userAgent", {
      value: "Mozilla/5.0 (Linux; Android 11; SmartTV) AppleWebKit/537.36",
      configurable: true,
    });
    window.localStorage.setItem("echodesk.forceTvUi", "1");
    (window as unknown as { Capacitor?: { isNativePlatform: () => boolean } }).Capacitor = {
      isNativePlatform: () => true,
    };
  });
  await page.route(
    "https://api.github.com/repos/yoligehude14753/echo-demo/releases/latest",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          tag_name: "v0.2.28",
          name: "EchoDesk v0.2.28",
          html_url: "https://github.com/yoligehude14753/echo-demo/releases/tag/v0.2.28",
          assets: [
            {
              name: "EchoDesk.Setup.0.2.28.exe",
              size: 123,
              browser_download_url:
                "https://github.com/yoligehude14753/echo-demo/releases/download/v0.2.28/EchoDesk.Setup.0.2.28.exe",
            },
            {
              name: "EchoDesk-0.2.28-smart-tv.apk",
              size: 456,
              browser_download_url:
                "https://github.com/yoligehude14753/echo-demo/releases/download/v0.2.28/EchoDesk-0.2.28-smart-tv.apk",
            },
            {
              name: "EchoDesk-0.2.28-android.apk",
              size: 789,
              browser_download_url:
                "https://github.com/yoligehude14753/echo-demo/releases/download/v0.2.28/EchoDesk-0.2.28-android.apk",
            },
          ],
        }),
      });
    },
  );
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.getByTestId("open-settings").click();
  await page.getByTestId("check-updates").click();

  await expect(page.getByTestId("update-status-tag")).toContainText("发现新版本");
  await expect(page.getByTestId("update-asset-name")).toContainText(
    "EchoDesk-0.2.28-smart-tv.apk",
  );
});
