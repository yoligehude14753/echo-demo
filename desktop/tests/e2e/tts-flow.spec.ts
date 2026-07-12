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
  delayMs = 0,
): Promise<void> {
  await page.route("**/tts/diag**", async (route) => {
    if (delayMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
    await route.fulfill({
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
    });
  });
}

test("健康场景：/tts/diag=ok → 顶栏显示『语音播报』绿色态", async ({ page }) => {
  await mockTtsDiag(page, { ok: true, state: "ok" });
  await installEchoMock(page, { skipPaths: ["/tts/diag"] });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const toggle = page.getByTestId("tts-toggle");
  await expect(toggle).toBeVisible();
  await expect(toggle).toContainText(/^语音播报$/);
  await expect(toggle).toHaveAttribute("data-tts-state", "ok");
});

test("异常场景：/tts/diag=silent_output → 顶栏切『播报异常』+ Popover 显示原因", async ({
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
  await expect(toggle).toContainText("播报异常");

  // AI 引擎状态：打开诊断信息 → 看到合成回环行
  await page.getByTestId("pill-ai-engine").click();
  const synth = page.getByTestId("tts-synth-status");
  await expect(synth).toBeVisible();
  await expect(synth).toHaveAttribute("data-tts-state", "silent_output");
  await expect(synth).toContainText("未检测到可播放的声音");
});

test("初始诊断迟到且明确 disabled：消费事件但不请求 speak 或弹 toast", async ({
  page,
}) => {
  await mockTtsDiag(
    page,
    {
      ok: false,
      state: "disabled",
      detail: "TTS_ENABLED=false",
      latency_ms: null,
      pcm_bytes: null,
      rms: null,
      peak: null,
    },
    500,
  );
  const mock = await installEchoMock(page, {
    skipPaths: ["/tts/diag", "/tts/speak"],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const toggle = page.getByTestId("tts-toggle");
  await expect(toggle).toBeVisible();
  await mock.publish({
    type: "tts.suggested",
    seq: 1,
    ts: new Date().toISOString(),
    payload: { text: "诊断确认关闭前也不能请求" },
  });

  await expect(toggle).toHaveAttribute("data-tts-state", "unhealthy", {
    timeout: 10_000,
  });
  await page.waitForTimeout(100);
  const speakRequests = (await mock.fetchLog()).filter(({ url }) =>
    url.replace(/^https?:\/\/[^/]+/, "").includes("/tts/speak"),
  );
  expect(speakRequests).toHaveLength(0);
  await expect(
    page.locator(".ant-message-error").filter({ hasText: /语音播报/ }),
  ).toHaveCount(0);
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
    seq: 1,
    ts: new Date().toISOString(),
    payload: { text: "测试一下" },
  });

  // App 与 CommandBar 共享 TtsProvider 的单例 controller；同一个 WS 事件
  // 只能发出一次请求，并产生一条用户可见错误。
  await expect(
    page
      .locator(".ant-message-error")
      .filter({ hasText: /语音播报未检测到可播放的声音/ })
      .first(),
  ).toBeVisible({ timeout: 10_000 });

  // 期望：toggle 切到 unhealthy 态
  await expect(toggle).toHaveAttribute("data-tts-state", "unhealthy", {
    timeout: 10_000,
  });
  await expect(toggle).toContainText("播报异常");

  const speakRequests = (await mock.fetchLog()).filter(({ url }) =>
    url.replace(/^https?:\/\/[^/]+/, "").includes("/tts/speak"),
  );
  expect(speakRequests).toHaveLength(1);
});

test("批量 tts.suggested 始终按 seq 升序播报", async ({ page }) => {
  await mockTtsDiag(page, { ok: true, state: "ok" });
  const mock = await installEchoMock(page, {
    skipPaths: ["/tts/diag"],
    errorPaths: { "/tts/speak": 502 },
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("tts-toggle")).toHaveAttribute("data-tts-state", "ok");

  await page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    const event = (seq: number, text: string) => ({
      type: "tts.suggested",
      seq,
      ts: new Date().toISOString(),
      payload: { text },
    });
    useStore.setState({
      events: [event(3, "third"), event(1, "first"), event(2, "second")],
    });
  });

  await expect
    .poll(async () => {
      const requests = (await mock.fetchLog()).filter(({ url }) =>
        url.replace(/^https?:\/\/[^/]+/, "").includes("/tts/speak"),
      );
      return requests.map(({ bodyText }) =>
        JSON.parse(bodyText ?? "{}") as { text?: string },
      );
    })
    .toEqual([{ text: "first" }, { text: "second" }, { text: "third" }]);
});

test("用户关闭语音播报：顶栏切『已静音』+ 后续入口不再请求 speak", async ({ page }) => {
  await mockTtsDiag(page, { ok: true, state: "ok" });
  const mock = await installEchoMock(page, { skipPaths: ["/tts/diag"] });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const toggle = page.getByTestId("tts-toggle");
  await expect(toggle).toHaveAttribute("data-tts-state", "ok");
  await toggle.click();
  await expect(toggle).toHaveAttribute("data-tts-state", "disabled");
  await expect(toggle).toContainText("已静音");

  await mock.publish({
    type: "tts.suggested",
    seq: 1,
    ts: new Date().toISOString(),
    payload: { text: "静音后不应请求" },
  });
  await page.waitForTimeout(100);
  const speakRequests = (await mock.fetchLog()).filter(({ url }) =>
    url.replace(/^https?:\/\/[^/]+/, "").includes("/tts/speak"),
  );
  expect(speakRequests).toHaveLength(0);
});

test("静音期间持续推进 seq：重开不重放旧事件且只消费新事件", async ({ page }) => {
  await mockTtsDiag(page, { ok: true, state: "ok" });
  const mock = await installEchoMock(page, {
    skipPaths: ["/tts/diag"],
    errorPaths: { "/tts/speak": 502 },
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const toggle = page.getByTestId("tts-toggle");
  await expect(toggle).toHaveAttribute("data-tts-state", "ok");
  await toggle.click();
  await expect(toggle).toHaveAttribute("data-tts-state", "disabled");

  await mock.publish({
    type: "tts.suggested",
    seq: 1,
    ts: new Date().toISOString(),
    payload: { text: "静音期间的旧事件" },
  });
  // Deliberately re-enable immediately. The event effect may not have run yet,
  // so setEnabled(true) must synchronously absorb the store high-water mark.
  await toggle.click();
  await expect(toggle).toHaveAttribute("data-tts-state", "ok");
  await page.waitForTimeout(100);
  expect(
    (await mock.fetchLog()).filter(({ url }) =>
      url.replace(/^https?:\/\/[^/]+/, "").includes("/tts/speak"),
    ),
  ).toHaveLength(0);

  await mock.publish({
    type: "tts.suggested",
    seq: 2,
    ts: new Date().toISOString(),
    payload: { text: "重开后的新事件" },
  });
  await expect
    .poll(async () => {
      const requests = (await mock.fetchLog()).filter(({ url }) =>
        url.replace(/^https?:\/\/[^/]+/, "").includes("/tts/speak"),
      );
      return requests.map(({ bodyText }) =>
        JSON.parse(bodyText ?? "{}") as { text?: string },
      );
    })
    .toEqual([{ text: "重开后的新事件" }]);
});
