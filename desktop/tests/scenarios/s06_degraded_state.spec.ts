/**
 * 场景 6：AI 引擎降级态 — 语音服务全挂 + 主模型缺 key
 *
 * 覆盖功能：
 *  - P2.3 远端降级链路（探针 fail → pill 变色 + popover 显示 fail/no_api_key 文案）
 *  - P2.1 多级状态可视化（fail vs warn 区分）
 *
 * 视频里观察点：
 *  - 语音识别是 AI 引擎的必需能力；部分探针失败时 pill 显示橙色并保留错误明细
 *  - 顶栏 AI 引擎 pill 橙色（主模型 / 联网检索都 no_api_key）
 *  - 点开 popover 看到具体错误「Connection refused」+ 提示「编辑 config.json」
 */
import { test, expect } from "@playwright/test";
import { installScenarioMock } from "./_helpers";

test("S06a · 语音服务全挂 → AI 引擎 pill 显示降级 + 错误 popover", async ({ page }) => {
  await installScenarioMock(page, { healthOverride: "service-down" });

  await test.step("打开主界面，部分必需能力失败时 pill 显示橙色", async () => {
    await page.goto("/");
    await expect(page.getByTestId("pill-ai-engine")).toBeVisible();
    await expect(page.getByTestId("pill-ai-engine").locator("span.bg-amber-500")).toBeVisible({
      timeout: 8_000,
    });
  });

  await test.step("点击 AI 引擎 pill：popover 显示可读的语音服务状态", async () => {
    await page.getByTestId("pill-ai-engine").click();
    const popover = page.locator(
      ".ant-popover:not(.ant-popover-hidden) .ant-popover-content",
    );
    await expect(popover.getByText("无法连接").first()).toBeVisible({
      timeout: 3_000,
    });
  });
});

test("S06b · 主模型 / 联网检索缺 key → 智能引擎 pill 橙色 + 提示文案", async ({ page }) => {
  await installScenarioMock(page, { healthOverride: "main-no-key" });

  await test.step("打开主界面，智能引擎 pill 应为橙色（no_api_key 归类 warn）", async () => {
    await page.goto("/");
    await expect(page.getByTestId("pill-ai-engine")).toBeVisible();
    await expect(page.getByTestId("pill-ai-engine").locator("span.bg-amber-500")).toBeVisible({
      timeout: 8_000,
    });
  });

  await test.step("点击智能引擎状态：popover 显示可理解的配置提示", async () => {
    await page.getByTestId("pill-ai-engine").click();
    const popover = page.locator(
      ".ant-popover:not(.ant-popover-hidden) .ant-popover-content",
    );
    await expect(popover.getByText(/部分服务凭证未配置/)).toBeVisible({
      timeout: 3_000,
    });
    await expect(popover.getByText(/请在设置中补充配置/)).toBeVisible();
  });
});
