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
