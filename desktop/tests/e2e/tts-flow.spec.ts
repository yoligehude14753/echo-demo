/**
 * TTS 主链路 E2E（M_tts_check 复盘新增）。
 *
 * 覆盖之前用户报告"TTS 完全失效"时藏在系统里的三条裂缝：
 *  1. /tts/diag 返回 ok → 顶栏 TTS 标签显示绿色「TTS」
 *  2. /tts/diag 返回 silent_output → 顶栏切橙色「TTS 异常」+ ModelServicePopover 反映
 *  3. /tts/speak 502 时 → message.error 出现，顶栏切异常态（不再 console.warn 静默）
 *
 * 这些场景以前没有 e2e 覆盖，是 phase4-tts 的关键退路。
 */
import { test, expect } from "@playwright/test";
import { installEchoMock } from "./_mock";

// mock /tts/diag：根据 state 返回对应 payload；route 函数会读 query
async function mockTtsDiag(
  page: import("@playwright/test").Page,
  payload: {
    ok: boolean;
    state: "ok" | "disabled" | "upstream_error" | "silent_output" | "empty";
    detail?: string | null;
    latency_ms?: number | null;
    pcm_bytes?: number | null;
    rms?: number | null;
    peak?: number | null;
  },
): Promise<void> {
  await page.route("**/tts/diag**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: payload.ok,
        state: payload.state,
        detail: payload.detail ?? null,
        latency_ms: payload.latency_ms ?? 220.5,
        pcm_bytes: payload.pcm_bytes ?? 32_000,
        rms: payload.rms ?? 2500,
        peak: payload.peak ?? 18_000,
        voice: "aiden",
        base_url: "http://100.76.3.59:8094",
        checked_at: Date.now() / 1000,
      }),
    }),
  );
}

test("健康场景：/tts/diag=ok → 顶栏显示『TTS』绿色态", async ({ page }) => {
  await mockTtsDiag(page, { ok: true, state: "ok" });
  await installEchoMock(page, { skipPaths: ["/tts/diag"] });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const toggle = page.getByTestId("tts-toggle");
  await expect(toggle).toBeVisible();
  await expect(toggle).toContainText(/^TTS$/);
  await expect(toggle).toHaveAttribute("data-tts-state", "ok");
});

test("异常场景：/tts/diag=silent_output → 顶栏切『TTS 异常』+ Popover 显示原因", async ({
  page,
}) => {
  await mockTtsDiag(page, {
    ok: false,
    state: "silent_output",
    detail: "upstream returned 30720 bytes but rms=3.2 (< 50.0)",
    rms: 3.2,
  });
  await installEchoMock(page, { skipPaths: ["/tts/diag"] });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const toggle = page.getByTestId("tts-toggle");
  await expect(toggle).toHaveAttribute("data-tts-state", "unhealthy", { timeout: 10_000 });
  await expect(toggle).toContainText("TTS 异常");

  // ModelServicePopover：点 模型服务标签 → 看到合成回环行
  await page.getByTestId("pill-model-service").click();
  const synth = page.getByTestId("tts-synth-status");
  await expect(synth).toBeVisible();
  await expect(synth).toHaveAttribute("data-tts-state", "silent_output");
  await expect(synth).toContainText("silent_output");
});

test("失败场景：/tts/speak 502 → message.error 且顶栏切异常", async ({
  page,
}) => {
  // diag 起点是 ok（让初始顶栏是绿的，方便观察切换）
  await mockTtsDiag(page, { ok: true, state: "ok" });
  // speak 返回 502 silent_output（FastAPI HTTPException JSON 包成 detail）
  await page.route("**/tts/speak", (route) =>
    route.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({
        detail:
          "tts_silent_output: upstream returned 30720 bytes PCM but rms=2.1 (< 50.0)",
      }),
    }),
  );

  const mock = await installEchoMock(page, {
    skipPaths: ["/tts/diag", "/tts/speak"],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  // 启动时 toggle 是绿色 ok
  const toggle = page.getByTestId("tts-toggle");
  await expect(toggle).toHaveAttribute("data-tts-state", "ok");

  // 触发 TTS：通过 WS 推一个 tts.suggested 事件（hook 会自动 fetch /tts/speak）
  await mock.publish({
    type: "tts.suggested",
    seq: 100,
    ts: new Date().toISOString(),
    payload: { text: "测试一下" },
  });

  // 期望：message.error 弹出。
  // 注：App.tsx 与 CommandBar.tsx 各自调用 useTtsPlayer()，会产生两个独立的
  // hook 实例 → 同一个 WS 事件触发两次 fetch、两次 toast。用 .first() 包容
  // 这一既有现象；真正要测的是"用户看到了错误"，不是"toast 只显示一次"。
  await expect(
    page
      .locator(".ant-message-error")
      .filter({ hasText: /TTS 上游返回静音/ })
      .first(),
  ).toBeVisible({ timeout: 10_000 });

  // 期望：toggle 切到 unhealthy 态
  await expect(toggle).toHaveAttribute("data-tts-state", "unhealthy", {
    timeout: 10_000,
  });
  await expect(toggle).toContainText("TTS 异常");
});

test("用户关 TTS：顶栏切『静音』+ 不再轮询 diag", async ({ page }) => {
  await mockTtsDiag(page, { ok: true, state: "ok" });
  await installEchoMock(page, { skipPaths: ["/tts/diag"] });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const toggle = page.getByTestId("tts-toggle");
  await expect(toggle).toHaveAttribute("data-tts-state", "ok");
  await toggle.click();
  await expect(toggle).toHaveAttribute("data-tts-state", "disabled");
  await expect(toggle).toContainText("静音");
});
