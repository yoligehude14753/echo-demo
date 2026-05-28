/**
 * E2E：CaptureStatus 两计数器（"采集" vs "入库"）分别走
 *
 * 修复 Phase 4 文案误导：顶栏曾经显示「已转 4266」实际是 chunk POST 成功计数，
 * 不区分后端是否真的把内容写进 ambient_segments；用户误以为转写出 4266 段、
 * 实际可能 0 段。本 spec 用 mock `/capture/chunk` 验证：
 *
 *  - "采集 N" = onChunkPosted（每次 200 都 +1，含 ambient_stored=false 的）
 *  - "入库 M" = onAmbientUploaded（仅 ambient_stored=true 才 +1）
 *
 * 通过让后端 mock 一半 chunk 返 stored=true、一半 stored=false，
 * 期望 N >> M（这里至少能看到 N ≥ 5、M ≈ N/2 且 N > M）。
 *
 * 注意：headless Chromium 无真实麦克风、AudioContext 也不跑，
 * 所以用 audioCapture 暴露的 __emitChunkForTest test seam 同步触发 chunk，
 * 走真实 ChunkRouter → POST /capture/chunk（被 page.route mock）→ 计数器 +1。
 */
import { test, expect } from "@playwright/test";
import { installEchoMock } from "./_mock";

/**
 * 注入：1) 假 getUserMedia（无真实麦克风也能让 audioCapture 进入 capturing 态，
 *      避免 UI 渲染成「麦克风不可用」错误态而盖掉新文案）2) 假 permissions
 *
 * 真实 chunk emit 由 __emitChunkForTest 同步触发，无需等 ScriptProcessorNode。
 */
async function stubMicPermission(
  page: import("@playwright/test").Page,
): Promise<void> {
  await page.addInitScript(() => {
    const md = window.navigator as unknown as {
      mediaDevices?: { getUserMedia: (c: unknown) => Promise<MediaStream> };
    };
    if (md.mediaDevices) {
      md.mediaDevices.getUserMedia = async () => {
        const ctx = new AudioContext();
        const dst = ctx.createMediaStreamDestination();
        const osc = ctx.createOscillator();
        osc.frequency.value = 0;
        osc.connect(dst);
        osc.start();
        return dst.stream;
      };
    }
    const fakePermissions = {
      query: async (q: { name: string }) => ({
        state: q.name === "microphone" ? "granted" : "prompt",
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
      }),
    };
    try {
      Object.defineProperty(window.navigator, "permissions", {
        value: fakePermissions,
        configurable: true,
        writable: true,
      });
    } catch {
      /* ignore */
    }
  });
}

/** 通过 audioCapture test seam 合成 N 次 chunk emit（同步排队） */
async function emitChunks(
  page: import("@playwright/test").Page,
  n: number,
): Promise<void> {
  await page.evaluate(async (count) => {
    type WithAudio = Window & {
      __echoAudioCapture?: { __emitChunkForTest: (b?: Blob) => void };
    };
    const ac = (window as WithAudio).__echoAudioCapture;
    if (!ac) throw new Error("__echoAudioCapture not exposed; dev build only");
    for (let i = 0; i < count; i++) {
      ac.__emitChunkForTest();
      // 让 microtask 队列把 fetch 推出去，避免一次性塞爆 router
      await new Promise((r) => setTimeout(r, 10));
    }
  }, n);
}

test("采集计数器与入库计数器分别走（mock /capture/chunk 交替返回 ambient_stored）", async ({
  page,
}) => {
  test.setTimeout(30_000);

  await stubMicPermission(page);
  await installEchoMock(page);

  // 偶数次返回 stored=true，奇数次返回 stored=false → 让 UI 两个计数器分别走
  let postedCount = 0;
  await page.route(/\/(api\/)?capture\/chunk$/, async (route) => {
    postedCount += 1;
    const stored = postedCount % 2 === 0;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ambient_stored: stored,
        ambient_text: stored ? "测试转写片段" : null,
        audio_ref: `mock-${postedCount}`,
        meeting_segments: [],
      }),
    });
  });

  await page.goto("/");

  const cap = page.getByTestId("capture-status");
  await expect(cap).toBeVisible({ timeout: 15_000 });
  // 等 audioCapture 进入 capturing 态（headless 拿不到 mic 会落到 error 态，
  // 但 router 仍然挂着，__emitChunkForTest 同样能触发）
  await expect(cap).not.toContainText("初始化麦克风", { timeout: 10_000 });

  // 合成 6 次 chunk：postedCount 应 → 6，stored 应 → 3
  await emitChunks(page, 6);

  await expect
    .poll(async () => await cap.textContent(), {
      timeout: 10_000,
      intervals: [200],
    })
    .toMatch(/采集 [3-9]\d* · 入库 [1-9]\d*/);

  // 进一步校验数字关系：采集 > 入库（差距即"被过滤的静音/底噪"语义）
  const text = (await cap.textContent()) ?? "";
  const m = text.match(/采集 (\d+) · 入库 (\d+)/);
  expect(m).not.toBeNull();
  const captured = Number(m![1]);
  const stored = Number(m![2]);
  expect(captured).toBeGreaterThan(stored);
  expect(captured).toBeGreaterThanOrEqual(3);
  expect(stored).toBeGreaterThanOrEqual(1);

  // Tooltip 解释文案：hover 后能看到差距大的原因
  await cap.hover();
  await expect(
    page.locator(".ant-tooltip-inner").filter({ hasText: /VAD/ }),
  ).toBeVisible({ timeout: 5_000 });
});

test("待机文案：持续采集 · 采集 · 入库 · 静音/底噪自动过滤（无 meeting overlay 时）", async ({
  page,
}) => {
  test.setTimeout(20_000);

  await stubMicPermission(page);
  await installEchoMock(page);

  // 简单稳定 mock：永远返 stored=false
  await page.route(/\/(api\/)?capture\/chunk$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ambient_stored: false,
        ambient_text: null,
        audio_ref: "mock",
        meeting_segments: [],
      }),
    });
  });

  await page.goto("/");

  const cap = page.getByTestId("capture-status");
  await expect(cap).toBeVisible({ timeout: 15_000 });
  await expect(cap).not.toContainText("初始化麦克风", { timeout: 10_000 });

  // 待机态文案断言：必须包含两个新计数器名 + 静音/底噪过滤说明
  await expect
    .poll(async () => await cap.textContent(), { timeout: 8_000, intervals: [200] })
    .toMatch(/持续采集.*采集 \d+.*入库 \d+.*静音\/底噪自动过滤/);

  // 旧文案"已转"不应再出现
  await expect(cap).not.toContainText("已转");

  // aria-label 已注入（无障碍）
  const ariaLabel = await cap.getAttribute("aria-label");
  expect(ariaLabel).toMatch(/持续采集中，已采集 \d+ 段，入库 \d+ 段/);
});
