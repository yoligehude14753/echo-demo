/**
 * E2E #3：WS 抖动恢复。
 *
 * 流程：
 * - 启动 → 已连接
 * - mock 关掉 ws → "断线" 显示
 * - 等几秒（前端用指数退避自动重连）→ 重新"已连接"
 */
import { test, expect } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("WS 断开后前端能自动重连", async ({ page }) => {
  const mock = await installEchoMock(page);
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  // 1. 关掉 ws
  await mock.closeWs(1006, "abnormal");
  await expect(page.locator("text=断线")).toBeVisible({ timeout: 5_000 });

  // 2. 允许重连（前端 onclose 后会 backoff 重连，由于 MockWebSocket 是新 instance，
  //    自动 open → onopen → 自动回 server_hello）
  await mock.reopenWs();
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 15_000 });
});
