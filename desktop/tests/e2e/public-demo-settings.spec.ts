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

test("设置页：检查更新会展示当前平台优选 release 资产", async ({ page }) => {
  await page.route(
    "https://api.github.com/repos/yoligehude14753/echo-demo/releases/latest",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          tag_name: "v0.2.18",
          name: "EchoDesk v0.2.18",
          html_url: "https://github.com/yoligehude14753/echo-demo/releases/tag/v0.2.18",
          assets: [
            {
              name: "EchoDesk.Setup.0.2.18.exe",
              size: 123,
              browser_download_url:
                "https://github.com/yoligehude14753/echo-demo/releases/download/v0.2.18/EchoDesk.Setup.0.2.18.exe",
            },
            {
              name: "EchoDesk-0.2.18-smart-tv.apk",
              size: 456,
              browser_download_url:
                "https://github.com/yoligehude14753/echo-demo/releases/download/v0.2.18/EchoDesk-0.2.18-smart-tv.apk",
            },
            {
              name: "EchoDesk-0.2.18-arm64.dmg",
              size: 789,
              browser_download_url:
                "https://github.com/yoligehude14753/echo-demo/releases/download/v0.2.18/EchoDesk-0.2.18-arm64.dmg",
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
  await expect(page.getByTestId("updates-section").getByText("v0.2.18")).toBeVisible();
  await expect(page.getByTestId("update-asset-name")).toContainText(
    "EchoDesk.Setup.0.2.18.exe",
  );
  await expect(page.getByTestId("install-update")).toBeEnabled();
});
