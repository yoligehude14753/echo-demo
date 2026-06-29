/**
 * 场景 6：模型服务降级态 — 语音模型服务全挂 + 主模型缺 key
 *
 * 覆盖功能：
 *  - P2.3 远端降级链路（探针 fail → pill 变色 + popover 显示 fail/no_api_key 文案）
 *  - P2.1 多级状态可视化（fail vs warn 区分）
 *
 * 视频里观察点：
 *  - 顶栏模型服务 pill 红色（3 个探针全 fail）
 *  - 顶栏智能引擎 pill 橙色（主模型 / 联网检索都 no_api_key）
 *  - 点开 popover 看到具体错误「Connection refused」+ 提示「编辑 config.json」
 */
import { test, expect } from "@playwright/test";
import { installScenarioMock } from "./_helpers";

test("S06a · 模型服务全挂 → 模型服务 pill 红色 + 错误 popover", async ({ page }) => {
  await installScenarioMock(page, { healthOverride: "service-down" });

  await test.step("打开主界面，模型服务 pill 应为红色", async () => {
    await page.goto("/");
    await expect(page.getByTestId("pill-model-service")).toBeVisible();
    await expect(page.getByTestId("pill-model-service").locator("span.bg-err")).toBeVisible({
      timeout: 8_000,
    });
  });

  await test.step("点击模型服务 pill：popover 显示 3 个探针 fail + 错误原因", async () => {
    await page.getByTestId("pill-model-service").click();
    const popover = page.locator(
      ".ant-popover:not(.ant-popover-hidden) .ant-popover-content",
    );
    await expect(popover.getByText("Connection refused").first()).toBeVisible({
      timeout: 3_000,
    });
  });
});

test("S06b · 主模型 / 联网检索缺 key → 智能引擎 pill 橙色 + 提示文案", async ({ page }) => {
  await installScenarioMock(page, { healthOverride: "main-no-key" });

  await test.step("打开主界面，智能引擎 pill 应为橙色（no_api_key 归类 warn）", async () => {
    await page.goto("/");
    await expect(page.getByTestId("pill-main-model")).toBeVisible();
    await expect(page.getByTestId("pill-main-model").locator("span.bg-amber-500")).toBeVisible({
      timeout: 8_000,
    });
  });

  await test.step("点击智能引擎 pill：popover 显示「部分密钥未配置」橙色提示", async () => {
    await page.getByTestId("pill-main-model").click();
    const popover = page.locator(
      ".ant-popover:not(.ant-popover-hidden) .ant-popover-content",
    );
    await expect(popover.getByText(/部分密钥未配置/)).toBeVisible({ timeout: 3_000 });
    await expect(popover.getByText(/config\.json/)).toBeVisible();
  });
});
