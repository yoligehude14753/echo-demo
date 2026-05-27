/**
 * 场景 5：异常路径 — LLM 失败 + WS 断线重连
 *
 * 覆盖功能：
 *  - P2.2 @生成 失败 → 错误 toast，textarea 不锁死
 *  - WS 断线自动重连
 *
 * 视频里观察点：
 *  - 第一次提交：toast 弹错误「生成失败 …」
 *  - textarea 立刻能再次输入（验证未锁死）
 *  - 第二段：断 WS → 顶栏 status pill 由绿变红 → 自动重连恢复
 */
import { test, expect } from "@playwright/test";
import { installScenarioMock } from "./_helpers";

test("S05a · @生成 后端 500 → 错误 toast，textarea 不锁死（P2.2）", async ({ page }) => {
  await page.route(/\/intent\/route$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "generate_html",
        confidence: 0.95,
        params: { artifact_type: "html", brief: "强制失败" },
      }),
    });
  });

  await installScenarioMock(page, {
    errorPaths: { "/artifacts/generate": 500 },
  });

  await test.step("打开主界面，等连接 OK", async () => {
    await page.goto("/");
    await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });
  });

  await test.step("输入 @生成 HTML 并提交，后端返 500", async () => {
    const textarea = page.getByTestId("command-textarea");
    await textarea.fill("@生成 HTML 测试报告");
    await textarea.press("Enter");
  });

  await test.step("toast 显示「生成失败 …」错误", async () => {
    await expect(
      page.locator(".ant-message-error, .ant-notification-notice-error").first(),
    ).toBeVisible({ timeout: 5_000 });
  });

  await test.step("textarea 仍可输入（不锁死）", async () => {
    const textarea = page.getByTestId("command-textarea");
    await expect(textarea).not.toBeDisabled();
    await textarea.fill("@查 今天天气");
    await expect(textarea).toHaveValue("@查 今天天气");
  });
});

test("S05b · WebSocket 断线 → 顶栏掉线提示 → 自动重连", async ({ page }) => {
  const mock = await installScenarioMock(page);

  await test.step("打开主界面，初始已连接", async () => {
    await page.goto("/");
    await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });
  });

  await test.step("主动断 WS → 顶栏显示「断线」", async () => {
    await mock.closeWs(1006, "test-disconnect");
    await expect(page.locator("text=断线")).toBeVisible({ timeout: 5_000 });
  });

  await test.step("打开 reopenable，等待自动重连恢复「已连接」", async () => {
    await mock.reopenWs();
    await expect(page.locator("text=已连接")).toBeVisible({ timeout: 10_000 });
  });
});
