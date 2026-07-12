/**
 * 场景 1：新用户首次启动 → 完成引导 → 查看「关于」对话框
 *
 * 覆盖功能：
 *  - P3.1 首次启动 3 步引导（欢迎 / 麦克风 / 完成）
 *  - P3.5 Electron systemPreferences 麦克风权限探测（mock granted）
 *  - P3.3 顶栏 v0.2.0 徽章 → About 对话框（前后端版本 + 链接）
 *
 * 视频里观察点：
 *  - Modal 平滑弹出 / 步骤切换
 *  - "已授权" 绿色 + 自动进入下一步
 *  - About modal 里前端 0.2.0 / 后端 0.2.0 同时显示
 *  - CHANGELOG / INSTALL 链接 href 正确
 */
import { test, expect } from "@playwright/test";
import { installScenarioMock } from "./_helpers";

test("S01 · 首次启动引导 → About 对话框（P3.1 + P3.5 + P3.3）", async ({ page }) => {
  await installScenarioMock(page, {
    keepOnboarding: true, // 这个场景就是要测引导
    micPermission: "granted",
  });

  await test.step("打开 EchoDesk，引导 Modal 自动弹出", async () => {
    await page.goto("/");
    await expect(page.getByText("欢迎来到 EchoDesk")).toBeVisible({ timeout: 5_000 });
    // 步骤指示器：当前在第 1 步
    await expect(page.getByText(/会议\s*\+\s*办公的本地分身|本地优先/i)).toBeVisible();
  });

  await test.step("第 1 步 → 第 2 步（麦克风授权页）", async () => {
    await page.getByTestId("onboarding-next").click();
    await expect(page.getByText("授权麦克风")).toBeVisible();
    await expect(page.getByTestId("onboarding-mic-state")).toBeVisible();
  });

  await test.step("第 2 步 → 第 3 步（完成页）", async () => {
    await page.getByTestId("onboarding-next").click();
    await expect(page.getByText("准备就绪")).toBeVisible();
  });

  await test.step("点「完成」关闭引导，主界面可见", async () => {
    await page.getByTestId("onboarding-next").click();
    await expect(page.getByText("准备就绪")).not.toBeVisible({ timeout: 3_000 });
    // 主界面顶栏
    await expect(page.getByTestId("status-bar")).toBeVisible();
    await expect(page.getByTestId("open-about")).toBeVisible();
  });

  await test.step("点顶栏 v0.2.0 徽章 → About 对话框弹出", async () => {
    await page.getByTestId("open-about").click();
    const body = page.getByTestId("about-modal-body");
    await expect(body).toBeVisible();
    // 前端版本：从 package.json 编译注入
    await expect(page.getByTestId("about-frontend-version")).toContainText(/^v\d+\.\d+\.\d+/);
    // 后端版本：从 mock /healthz/full 返回的 backend.version
    await expect(page.getByTestId("about-backend-version")).toContainText("0.2.0", {
      timeout: 5_000,
    });
  });

  await test.step("验证 CHANGELOG / INSTALL.md 链接 href 正确", async () => {
    await expect(page.getByTestId("about-changelog-link")).toHaveAttribute(
      "href",
      /CHANGELOG\.md$/,
    );
    await expect(page.getByTestId("about-install-link")).toHaveAttribute(
      "href",
      /docs\/INSTALL\.md$/,
    );
  });

  await test.step("关闭 About 对话框", async () => {
    await page.locator(".ant-modal-close").click();
    await expect(page.getByTestId("about-modal-body")).toBeHidden();
  });

  await test.step("刷新后引导不再弹（持久化生效）", async () => {
    await page.reload();
    await page.waitForTimeout(800);
    await expect(page.getByText("欢迎来到 EchoDesk")).not.toBeVisible();
    await expect(page.getByTestId("status-bar")).toBeVisible();
  });

  await test.step("从设置重新打开引导时回到欢迎页", async () => {
    await page.getByTestId("open-settings").click();
    await page.getByTestId("replay-onboarding").click();
    await expect(page.getByText("欢迎来到 EchoDesk")).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText("准备就绪")).not.toBeVisible();
    await expect(page.getByTestId("onboarding-prev")).toHaveCount(0);
    await expect(page.getByTestId("onboarding-next")).toHaveText("下一步");
  });
});
