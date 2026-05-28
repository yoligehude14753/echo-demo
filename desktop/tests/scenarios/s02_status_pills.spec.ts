/**
 * 场景 2：顶栏诊断 pill 巡检（点开 4 个 pill 看明细）
 *
 * 覆盖功能：
 *  - P2.1 状态可视化：backend / heyi-bj / 云 / 麦克风 4 个 pill
 *  - 每个 pill popover 显示版本 / 探针 / 权限态
 *  - P3.5 mic denied 时显示「打开系统设置」深链按钮
 *
 * 视频里观察点：
 *  - 4 个 pill 颜色（绿/橙/红）随状态变化
 *  - 点击 pill → popover 弹出 → 内容真实
 *  - mic denied 子场景：popover 里出现红色文案 + 深链按钮可点
 */
import { test, expect } from "@playwright/test";
import { installScenarioMock } from "./_helpers";

/**
 * 当前可见的 antd Popover 内容（排除 ant-popover-hidden）
 */
function visiblePopover(page: import("@playwright/test").Page) {
  return page.locator(".ant-popover:not(.ant-popover-hidden) .ant-popover-content");
}

/**
 * 点 pill → 等可见 popover 出现 → 跑断言 → 再点该 pill 切回（toggle close）→ 等关闭
 */
async function probePill(
  page: import("@playwright/test").Page,
  testId: string,
  assertContent: (popover: ReturnType<typeof visiblePopover>) => Promise<void>,
) {
  await page.getByTestId(testId).click();
  await page.waitForSelector(".ant-popover:not(.ant-popover-hidden)", { timeout: 5_000 });
  await assertContent(visiblePopover(page));
  // toggle close：再点同一个 pill
  await page.getByTestId(testId).click();
  // 等 antd 把 popover 加 ant-popover-hidden
  await page
    .waitForFunction(
      () => !document.querySelector(".ant-popover:not(.ant-popover-hidden)"),
      undefined,
      { timeout: 3_000 },
    )
    .catch(() => {
      /* 即便没完全关也无所谓，下一个 click 会切换 */
    });
}

test("S02 · 顶栏 4 个 pill 巡检（P2.1 全绿态）", async ({ page }) => {
  await installScenarioMock(page, {
    micPermission: "granted",
    healthOverride: "all-ok",
  });

  await test.step("打开主界面，顶栏 4 个 pill 渲染", async () => {
    await page.goto("/");
    await expect(page.getByTestId("status-bar")).toBeVisible();
    for (const id of ["pill-backend", "pill-heyi", "pill-yunwu", "pill-mic"]) {
      await expect(page.getByTestId(id)).toBeVisible();
    }
  });

  await test.step("backend pill popover：supervisor + version 0.2.0 + port 8769", async () => {
    await probePill(page, "pill-backend", async (po) => {
      await expect(po.getByText(/version/i).first()).toBeVisible({ timeout: 3_000 });
      await expect(po.getByText("0.2.0").first()).toBeVisible();
      await expect(po.getByText("8769").first()).toBeVisible();
    });
  });

  await test.step("heyi-bj pill popover：3 个探针（STT / TTS / Fast LLM）", async () => {
    await probePill(page, "pill-heyi", async (po) => {
      await expect(po.getByText(/STT FireRed/i)).toBeVisible({ timeout: 3_000 });
      await expect(po.getByText(/TTS Qwen3/i)).toBeVisible();
      await expect(po.getByText(/Fast LLM/i)).toBeVisible();
    });
  });

  await test.step("云 pill popover：Yunwu MiniMax + Tavily", async () => {
    await probePill(page, "pill-yunwu", async (po) => {
      await expect(po.getByText(/Yunwu MiniMax/i)).toBeVisible({ timeout: 3_000 });
      await expect(po.getByText(/Tavily/i)).toBeVisible();
    });
  });

  await test.step("mic pill popover：granted 绿色", async () => {
    await probePill(page, "pill-mic", async (po) => {
      await expect(po.getByText("权限状态")).toBeVisible({ timeout: 3_000 });
      await expect(po.getByText("granted")).toBeVisible();
    });
  });
});

test("S02b · mic denied 时 popover 显示「打开系统设置」深链（P3.5）", async ({ page }) => {
  await installScenarioMock(page, { micPermission: "denied" });

  // 在 _helpers.ts 注入的 window.echo 上 patch openMicSystemPrefs，
  // 调用时把标志位写到 window 让 Node 侧轮询
  await page.addInitScript(() => {
    const w = window as unknown as {
      echo?: Record<string, unknown>;
      __openPrefsCalled__?: boolean;
    };
    const orig = w.echo?.openMicSystemPrefs as (() => Promise<unknown>) | undefined;
    if (w.echo) {
      w.echo.openMicSystemPrefs = async () => {
        w.__openPrefsCalled__ = true;
        return orig ? await orig() : { ok: true };
      };
    }
  });

  await test.step("打开主界面，mic pill 应为红色", async () => {
    await page.goto("/");
    await expect(page.getByTestId("pill-mic")).toBeVisible();
    await expect(page.getByTestId("pill-mic").locator("span.bg-err")).toBeVisible({
      timeout: 5_000,
    });
  });

  await test.step("点击 mic pill：popover 出现「打开系统设置」按钮", async () => {
    await page.getByTestId("pill-mic").click();
    const btn = page.getByTestId("mic-open-system-prefs");
    await expect(btn).toBeVisible({ timeout: 3_000 });
  });

  await test.step("点「打开系统设置」→ Electron IPC 被调用", async () => {
    await page.getByTestId("mic-open-system-prefs").click();
    await expect
      .poll(
        async () =>
          await page.evaluate(() =>
            Boolean((window as unknown as { __openPrefsCalled__?: boolean }).__openPrefsCalled__),
          ),
        { timeout: 3_000 },
      )
      .toBe(true);
  });
});
