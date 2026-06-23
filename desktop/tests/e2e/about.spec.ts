/**
 * About 对话框（P3.3）e2e
 *
 * 覆盖：
 *  - 点 v0.x 徽章 → 弹出 AboutModal
 *  - 显示前端版本（编译时 0.2.0）+ 后端版本（mock 返回 0.2.0-mock）
 *  - CHANGELOG / INSTALL 链接 href 正确
 *  - 关闭后 modal 消失
 */
import { test, expect } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("点 v 徽章 → 弹出关于对话框，前后端版本可见", async ({ page }) => {
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const badge = page.getByTestId("open-about");
  await expect(badge).toBeVisible();
  // 文本应包含 "v0." 前缀（不锁死小版本号，避免每次发版改 spec）
  await expect(badge).toContainText(/^v\d+\./);

  await badge.click();

  const body = page.getByTestId("about-modal-body");
  await expect(body).toBeVisible();

  // 前端版本：编译期由 vite define 注入
  const feLine = page.getByTestId("about-frontend-version");
  await expect(feLine).toContainText(/^v\d+\.\d+\.\d+/);

  // 后端版本：mock /healthz/full → "0.2.0-mock"
  const beLine = page.getByTestId("about-backend-version");
  await expect(beLine).toContainText("0.2.0-mock", { timeout: 5000 });

  // 链接 href 正确
  const changelog = page.getByTestId("about-changelog-link");
  await expect(changelog).toHaveAttribute(
    "href",
    "https://github.com/yoligehude14753/echo-demo/blob/main/CHANGELOG.md",
  );
  const install = page.getByTestId("about-install-link");
  await expect(install).toHaveAttribute(
    "href",
    "https://github.com/yoligehude14753/echo-demo/blob/main/docs/INSTALL.md",
  );

  // 关闭：用键盘路径验证关闭按钮，兼容 TV/遥控器 Enter 操作。
  const closeButton = page.locator(".ant-modal-close").first();
  await closeButton.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".ant-modal-wrap")).toBeHidden({ timeout: 10_000 });
});
