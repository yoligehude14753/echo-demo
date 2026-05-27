/**
 * 真后端 烟雾测试（fast paths only）：不调慢路径 LLM，验证 UI 基础接通。
 *
 * 跑 ~30s。
 */
import { test, expect } from "@playwright/test";

// 真实可见的 CommandBar textarea（绕开 AntD hiddenTextarea）
const COMMAND_BAR_TA = "textarea[placeholder*='生成']";

test("smoke: page loads, ws connects, capture status visible", async ({ page }) => {
  test.setTimeout(60_000);

  await page.goto("/");

  // 1. 已连接（WS 真握手 client_hello → server_hello）
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 15_000 });

  // 2. CommandBar 输入框存在
  await expect(page.locator(COMMAND_BAR_TA)).toBeVisible();

  // 3. CaptureSession 状态（持续采集，无手动按钮）
  await expect(page.getByTestId("capture-status")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("capture-status")).toContainText(/持续采集|初始化麦克风|ambient/);

  // 4. ArtifactPanel 的"生成"按钮可见（首页右下角）
  await expect(page.getByRole("button", { name: /^生成/ }).first()).toBeVisible();

  // 5. 事件计数有更新（至少 server_hello 算一个）
  await expect(page.locator("text=/事件 \\d+/")).toBeVisible();
});

test("smoke: @start-meeting fast path", async ({ page }) => {
  test.setTimeout(60_000);

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 15_000 });

  const ta = page.locator(COMMAND_BAR_TA);
  await ta.fill("@开始会议");
  await ta.press("Enter");

  // 期望：toast "已开启"
  await expect(page.locator(".ant-message").filter({ hasText: /已开启|开启/ })).toBeVisible({
    timeout: 30_000,
  });
});

test("smoke: @generate-html keyword route is dispatched (don't wait for LLM)", async ({
  page,
}) => {
  test.setTimeout(30_000);

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 15_000 });

  const ta = page.locator(COMMAND_BAR_TA);
  await ta.fill("@生成 HTML 测试");
  await ta.press("Enter");

  // 期望：意图标签出现（不等真 LLM 完成）
  await expect(page.locator(".ant-tag").filter({ hasText: /生成 HTML/ })).toBeVisible({
    timeout: 20_000,
  });
});
