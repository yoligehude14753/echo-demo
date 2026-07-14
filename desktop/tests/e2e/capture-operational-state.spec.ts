import { expect, test, type Page } from "@playwright/test";
import { installEchoMock } from "./_mock";

async function stubMicPermission(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const md = window.navigator as unknown as {
      mediaDevices?: { getUserMedia: (constraints: unknown) => Promise<MediaStream> };
    };
    if (md.mediaDevices) {
      md.mediaDevices.getUserMedia = async () => {
        const context = new AudioContext();
        const destination = context.createMediaStreamDestination();
        const oscillator = context.createOscillator();
        oscillator.frequency.value = 0;
        oscillator.connect(destination);
        oscillator.start();
        return destination.stream;
      };
    }
    try {
      Object.defineProperty(window.navigator, "permissions", {
        configurable: true,
        value: {
          query: async () => ({
            state: "granted",
            addEventListener: () => undefined,
            removeEventListener: () => undefined,
          }),
        },
      });
    } catch {
      /* test environment may expose a readonly permissions object */
    }
  });
}

function captureStats(overrides: Record<string, unknown> = {}) {
  return {
    chunks_total: 0,
    gated_rms: 0,
    gated_low_speech: 0,
    stt_circuit_open: 0,
    stt_failed: 0,
    stt_empty: 0,
    hallu_dropped: 0,
    repeat_dropped: 0,
    diarize_failed: 0,
    diarize_returned_none: 0,
    stored: 0,
    last_chunk_at: null,
    last_stored_at: null,
    last_audio_stored_at: null,
    last_rms: 0,
    last_speech_ratio: 0,
    last_gate_reason: null,
    observed_audio_frames: 0,
    accepted_speech_frames: 0,
    accepted_speech_ratio: 0,
    stats_sequence: 0,
    ...overrides,
  };
}

async function expectCaptureStatus(page: Page) {
  const status = page.getByTestId("capture-status");
  await expect(status).toBeVisible({ timeout: 15_000 });
  await expect(status).not.toContainText("初始化麦克风", { timeout: 10_000 });
  return status;
}

test("transport warning 只由 upload ack 清除，旧响应字段缺失仍算 ack", async ({
  page,
}) => {
  test.setTimeout(30_000);
  let healthy = false;
  let chunkRequests = 0;
  let statsRequests = 0;

  await page.route(/\/(api\/)?capture\/chunk$/, async (route) => {
    chunkRequests += 1;
    if (!healthy) {
      await route.fulfill({ status: 503, body: "temporary failure" });
      return;
    }
    // 模拟旧 backend：HTTP 成功但两个新字段都缺失。
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ambient_stored: false }),
    });
  });
  await page.route(/\/(api\/)?capture\/stats$/, async (route) => {
    statsRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(captureStats({ stats_sequence: statsRequests })),
    });
  });
  await stubMicPermission(page);
  await installEchoMock(page, {
    skipPaths: ["/capture/chunk", "/capture/stats"],
  });
  await page.goto("/");

  const status = await expectCaptureStatus(page);
  await page.evaluate(() => {
    window.__echoAudioCapture?.__emitChunkForTest();
    window.__echoAudioCapture?.__emitChunkForTest();
  });
  await expect(status).toHaveAttribute(
    "data-transport-warning",
    "upload_unavailable",
    { timeout: 8_000 },
  );

  // 至少一次成功 stats 轮询不能越权清除 upload warning。
  const statsBefore = statsRequests;
  await expect
    .poll(() => statsRequests, { timeout: 8_000, intervals: [100] })
    .toBeGreaterThan(statsBefore);
  await expect(status).toHaveAttribute(
    "data-transport-warning",
    "upload_unavailable",
  );

  healthy = true;
  await page.evaluate(() => window.__echoAudioCapture?.__emitChunkForTest());
  await expect
    .poll(() => chunkRequests, { timeout: 8_000, intervals: [100] })
    .toBe(3);
  await expect(status).toHaveAttribute("data-transport-warning", "none");
});

test("audio admission warning 不被 upload ack 代偿，新的有效语音观测才清除", async ({
  page,
}) => {
  test.setTimeout(30_000);
  let recovered = false;
  let statsRequests = 0;

  await page.route(/\/(api\/)?capture\/chunk$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ambient_stored: false }),
    });
  });
  await page.route(/\/(api\/)?capture\/stats$/, async (route) => {
    statsRequests += 1;
    const lowInput = !recovered;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        captureStats(
          lowInput
            ? {
                chunks_total: 2,
                gated_rms: 2,
                last_rms: 4,
                last_gate_reason: "rms_too_low",
                stats_sequence: statsRequests,
                observed_audio_frames: 2,
              }
            : {
                chunks_total: 3,
                stored: 1,
                last_rms: 1_200,
                last_speech_ratio: 1,
                last_gate_reason: "ok",
                stats_sequence: statsRequests,
                observed_audio_frames: 3,
                accepted_speech_frames: 1,
                accepted_speech_ratio: 1 / 3,
              },
        ),
      ),
    });
  });
  await stubMicPermission(page);
  await installEchoMock(page, {
    skipPaths: ["/capture/chunk", "/capture/stats"],
  });
  await page.goto("/");

  const status = await expectCaptureStatus(page);
  await expect(status).toHaveAttribute("data-audio-warning", "rms_too_low");

  // ack 只改变 transport 轴，不能掩盖仍然没有有效输入的问题。
  await page.evaluate(() => window.__echoAudioCapture?.__emitChunkForTest());
  await expect(status).toHaveAttribute("data-transport-warning", "none");
  await expect(status).toHaveAttribute("data-audio-warning", "rms_too_low");

  recovered = true;
  const beforeRecoveryPoll = statsRequests;
  await expect
    .poll(() => statsRequests, { timeout: 8_000, intervals: [100] })
    .toBeGreaterThan(beforeRecoveryPoll);
  await expect(status).toHaveAttribute("data-audio-warning", "none");
});

test("freshness warning 只由新的 stats sequence 清除", async ({ page }) => {
  test.setTimeout(30_000);
  let statsRequests = 0;
  let sequence = 1;

  await page.route(/\/(api\/)?capture\/stats$/, async (route) => {
    statsRequests += 1;
    if (statsRequests > 1 && statsRequests <= 3) {
      await route.fulfill({ status: 503, body: "stats unavailable" });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        captureStats({
          stats_sequence: sequence,
          last_chunk_at: "2026-07-14T09:00:00.000Z",
        }),
      ),
    });
  });
  await stubMicPermission(page);
  await installEchoMock(page, { skipPaths: ["/capture/stats"] });
  await page.goto("/");

  const status = await expectCaptureStatus(page);
  await expect(status).toHaveAttribute(
    "data-freshness-warning",
    "stats_unavailable",
    { timeout: 12_000 },
  );

  // 成功但仍是旧 sequence，freshness warning 不能被成功 HTTP 清除。
  await expect
    .poll(() => statsRequests, { timeout: 8_000, intervals: [100] })
    .toBeGreaterThan(2);
  await expect(status).toHaveAttribute(
    "data-freshness-warning",
    "stats_unavailable",
  );

  sequence = 2;
  const beforeFreshSequence = statsRequests;
  await expect
    .poll(() => statsRequests, { timeout: 8_000, intervals: [100] })
    .toBeGreaterThan(beforeFreshSequence);
  await expect(status).toHaveAttribute("data-freshness-warning", "none");
});
