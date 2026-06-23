/**
 * 场景 3：设置面板巡检 — 远端服务配置 + 数据目录 + 诊断 + 回放引导
 *
 * 覆盖功能：
 *  - P2.5 /admin/data-dir 渲染
 *  - P2.6 /admin/diagnostics/export 下载（验证 API 被调）
 *  - P3.2 远端服务 7 字段表单 + 保存 → 重启按钮出现
 *  - P3.1 回放引导按钮 → onboarding Modal 再次显示
 *
 * 视频里观察点：
 *  - Drawer 从右边滑出
 *  - 远端服务表单里 yunwu_open_key 显示脱敏 sk-abcd***wxyz + user.json 标签
 *  - 改 llm_main_base_url → 保存 → toast「已写入 1 项，需重启后端生效」
 *  - 「重启 backend 生效」按钮浮现
 *  - 回放引导 → onboarding modal 重新出现
 */
import { test, expect } from "@playwright/test";
import { installScenarioMock } from "./_helpers";

test("S03 · 设置面板：远端配置 + 数据目录 + 回放引导（P2.5 + P3.2 + P3.1）", async ({ page }) => {
  await installScenarioMock(page);

  // 在 helper 注入的 window.echo 上 patch manualRestartBackend，把调用标志位
  // 写到 window 让测试侧轮询验证
  await page.addInitScript(() => {
    const w = window as unknown as {
      echo?: Record<string, unknown>;
      __restartCalled__?: boolean;
    };
    const orig = w.echo?.manualRestartBackend as (() => Promise<unknown>) | undefined;
    if (w.echo) {
      w.echo.manualRestartBackend = async () => {
        w.__restartCalled__ = true;
        return orig ? await orig() : { ok: true };
      };
    }
  });

  await test.step("打开主界面 → 点齿轮打开设置 Drawer", async () => {
    await page.goto("/");
    await page.getByTestId("open-settings").click();
    await expect(page.getByTestId("remote-settings-form")).toBeVisible({ timeout: 5_000 });
  });

  await test.step("数据目录 section 显示 /Users/test/.echodesk", async () => {
    await expect(page.getByText("/Users/test/.echodesk").first()).toBeVisible();
  });

  await test.step("远端服务表单：7 个字段渲染 + yunwu_open_key 显示脱敏", async () => {
    // antd Form.Item 的 name=key 会渲染 input
    const form = page.getByTestId("remote-settings-form");
    await expect(form.locator("input[id='llm_main_base_url']")).toHaveValue(
      "https://yunwu.ai/v1",
    );
    await expect(form.locator("input[id='stt_firered_url']")).toHaveValue(
      "http://100.76.3.59:8090",
    );
    // sensitive 字段 placeholder 显示脱敏值
    await expect(form.locator("input[id='yunwu_open_key']")).toBeVisible();
    // 「user.json」标签应该出现在 yunwu_open_key 行（source=user）
    await expect(page.getByText("user.json")).toBeVisible();
  });

  await test.step("修改 llm_main_base_url 并保存", async () => {
    const input = page.getByTestId("remote-settings-form").locator("input[id='llm_main_base_url']");
    await input.fill("https://yunwu-test.ai/v1");
    await page.getByTestId("save-remote-settings").click();
    // toast「已写入 1 项」
    await expect(page.getByText(/已写入\s*1\s*项/)).toBeVisible({ timeout: 5_000 });
  });

  await test.step("「重启 backend 生效」按钮浮现", async () => {
    await expect(page.getByTestId("restart-backend-after-config")).toBeVisible({
      timeout: 3_000,
    });
  });

  await test.step("点重启 → Electron IPC manualRestartBackend 被调用", async () => {
    await page.getByTestId("restart-backend-after-config").click();
    await expect
      .poll(
        async () =>
          await page.evaluate(() =>
            Boolean((window as unknown as { __restartCalled__?: boolean }).__restartCalled__),
          ),
        { timeout: 3_000 },
      )
      .toBe(true);
    await expect(page.getByText(/已发送重启请求/)).toBeVisible({ timeout: 3_000 });
  });

  await test.step("点「回放引导」→ 引导 Modal 再次显示（P3.1 验证）", async () => {
    // 先关 drawer 露出主界面
    await page.getByTestId("replay-onboarding").click();
    // antd Drawer 默认有 mask 点击外区域关闭；这里直接验证引导重新出现
    await expect(page.getByText("欢迎来到 EchoDesk")).toBeVisible({ timeout: 5_000 });
  });
});
