import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("AudioCapture stop 后晚到 getUserMedia 结果不会复活 capturing", async ({ page }) => {
  await page.addInitScript(() => {
    type TestWindow = Window & {
      __resolveLateStream?: () => void;
      __lateStreamTrackStops?: number;
    };
    const target = window as TestWindow;
    target.__lateStreamTrackStops = 0;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        enumerateDevices: async () => [],
        getUserMedia: () =>
          new Promise<MediaStream>((resolve) => {
            const track = {
              stop: () => {
                target.__lateStreamTrackStops = (target.__lateStreamTrackStops ?? 0) + 1;
              },
            } as unknown as MediaStreamTrack;
            target.__resolveLateStream = () =>
              resolve({ getTracks: () => [track] } as unknown as MediaStream);
          }),
      },
    });
  });
  await installEchoMock(page, { keepOnboarding: true });
  await page.goto("/");

  await page.evaluate(async () => {
    const { audioCapture } = await import("/src/capture/audioCapture.ts");
    audioCapture.start();
  });
  await expect.poll(() => page.evaluate(() => Boolean(window.__resolveLateStream))).toBe(true);
  await page.evaluate(async () => {
    const { audioCapture } = await import("/src/capture/audioCapture.ts");
    audioCapture.stop();
    window.__resolveLateStream?.();
  });
  await page.waitForTimeout(100);

  const result = await page.evaluate(async () => {
    const { audioCapture } = await import("/src/capture/audioCapture.ts");
    return {
      state: audioCapture.getState(),
      trackStops: window.__lateStreamTrackStops ?? 0,
    };
  });
  expect(result.state).toBe("initializing");
  expect(result.trackStops).toBe(1);
});

test("Android 原生录音错误会隔离旧监听、单次恢复并在三次后停止", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window.navigator, "userAgent", {
      configurable: true,
      get: () => "Mozilla/5.0 (Linux; Android 14; EchoDesk Native Test)",
    });
    (
      window as unknown as {
        CapacitorCustomPlatform?: { name: string };
      }
    ).CapacitorCustomPlatform = { name: "android" };
  });
  await installEchoMock(page, { keepOnboarding: true });
  await page.goto("/");
  await page.clock.install();

  await page.evaluate(async () => {
    const { audioCapture } = await import("/src/capture/audioCapture.ts");
    type ListenerRecord = {
      eventName: "chunk" | "error";
      listener: (event: Record<string, unknown>) => void;
      active: boolean;
    };
    const records: ListenerRecord[] = [];
    const state = {
      startCalls: 0,
      stopCalls: 0,
      removeCalls: 0,
      deliveredErrorCallbacks: 0,
      deliveredChunkCallbacks: 0,
      emittedChunks: 0,
      maxActiveChunkListeners: 0,
      maxActiveErrorListeners: 0,
    };
    const activeCount = (eventName: "chunk" | "error") =>
      records.filter((record) => record.active && record.eventName === eventName)
        .length;
    const updateMaxListeners = () => {
      state.maxActiveChunkListeners = Math.max(
        state.maxActiveChunkListeners,
        activeCount("chunk"),
      );
      state.maxActiveErrorListeners = Math.max(
        state.maxActiveErrorListeners,
        activeCount("error"),
      );
    };
    const controller = {
      snapshot: () => ({
        ...state,
        activeChunkListeners: activeCount("chunk"),
        activeErrorListeners: activeCount("error"),
      }),
      emitError: (repeat = 1) => {
        const callbacks = records.filter(
          (record) => record.active && record.eventName === "error",
        );
        for (let i = 0; i < repeat; i += 1) {
          for (const record of callbacks) {
            state.deliveredErrorCallbacks += 1;
            record.listener({ message: "mock AudioRecord runtime failure" });
          }
        }
      },
      emitChunk: () => {
        const base64 = btoa("\0".repeat(44));
        for (const record of records.filter(
          (candidate) => candidate.active && candidate.eventName === "chunk",
        )) {
          state.deliveredChunkCallbacks += 1;
          record.listener({ base64, sampleRate: 16_000, rms: 2, peak: 5 });
        }
      },
      dispose: () => undefined,
    };
    (
      window as unknown as { __nativeAudioMock__: typeof controller }
    ).__nativeAudioMock__ = controller;

    audioCapture.__setNativePluginForTest({
      start: async () => {
        state.startCalls += 1;
        return { sampleRate: 16_000, source: "mock-native" };
      },
      stop: async () => {
        state.stopCalls += 1;
      },
      addListener: async (
        eventName: "chunk" | "error",
        listener: (event: Record<string, unknown>) => void,
      ) => {
        const record: ListenerRecord = { eventName, listener, active: true };
        records.push(record);
        updateMaxListeners();
        return {
          remove: async () => {
            await new Promise<void>((resolve) => window.setTimeout(resolve, 50));
            if (!record.active) return;
            record.active = false;
            state.removeCalls += 1;
          },
        };
      },
    });
    const offChunk = audioCapture.onChunk(() => {
      state.emittedChunks += 1;
    });
    controller.dispose = () => {
      offChunk();
      audioCapture.stop();
    };
    audioCapture.start();
  });

  const snapshot = () =>
    page.evaluate(() =>
      (
        window as unknown as {
          __nativeAudioMock__: {
            snapshot: () => {
              startCalls: number;
              stopCalls: number;
              removeCalls: number;
              deliveredErrorCallbacks: number;
              deliveredChunkCallbacks: number;
              emittedChunks: number;
              maxActiveChunkListeners: number;
              maxActiveErrorListeners: number;
              activeChunkListeners: number;
              activeErrorListeners: number;
            };
          };
        }
      ).__nativeAudioMock__.snapshot(),
    );
  const emitError = (repeat = 1) =>
    page.evaluate(
      (count) =>
        (
          window as unknown as {
            __nativeAudioMock__: { emitError: (repeat?: number) => void };
          }
        ).__nativeAudioMock__.emitError(count),
      repeat,
    );

  await expect.poll(async () => (await snapshot()).startCalls).toBe(1);
  await emitError(2);
  await page.clock.runFor(5_100);
  await expect.poll(async () => (await snapshot()).startCalls).toBe(2);

  await page.evaluate(() =>
    (
      window as unknown as {
        __nativeAudioMock__: { emitChunk: () => void };
      }
    ).__nativeAudioMock__.emitChunk(),
  );
  await expect.poll(async () => (await snapshot()).emittedChunks).toBe(1);

  for (let expectedStarts = 3; expectedStarts <= 5; expectedStarts += 1) {
    await emitError();
    await page.clock.runFor(5_100);
    await expect.poll(async () => (await snapshot()).startCalls).toBe(expectedStarts);
  }
  await emitError();
  await page.clock.runFor(10_000);

  const result = await snapshot();
  const captureState = await page.evaluate(async () => {
    const { audioCapture } = await import("/src/capture/audioCapture.ts");
    return {
      state: audioCapture.getState(),
      error: audioCapture.getErrorMessage(),
    };
  });
  expect(result.startCalls).toBe(5);
  expect(result.deliveredErrorCallbacks).toBe(6);
  expect(result.deliveredChunkCallbacks).toBe(1);
  expect(result.emittedChunks).toBe(1);
  expect(result.maxActiveChunkListeners).toBe(1);
  expect(result.maxActiveErrorListeners).toBe(1);
  expect(result.activeChunkListeners).toBe(0);
  expect(result.activeErrorListeners).toBe(0);
  expect(result.removeCalls).toBe(10);
  expect(captureState.state).toBe("error");
  expect(captureState.error).toContain("自动恢复已达 3 次上限");

  await page.evaluate(() =>
    (
      window as unknown as {
        __nativeAudioMock__: { dispose: () => void };
      }
    ).__nativeAudioMock__.dispose(),
  );
});

test("Capture 上传单飞且队列有界，过载时明确背压", async ({ page }) => {
  let concurrent = 0;
  let maxConcurrent = 0;
  let requests = 0;
  await page.route(/\/(api\/)?capture\/chunk$/, async (route) => {
    requests += 1;
    concurrent += 1;
    maxConcurrent = Math.max(maxConcurrent, concurrent);
    await new Promise((resolve) => setTimeout(resolve, 150));
    concurrent -= 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ambient_stored: false,
        ambient_text: null,
        audio_ref: `slow-${requests}`,
        meeting_segments: [],
        stt_status: "empty",
      }),
    });
  });
  await installEchoMock(page, { skipPaths: ["/capture/chunk"] });
  await page.goto("/");

  await page.evaluate(() => {
    for (let i = 0; i < 20; i += 1) {
      window.__echoAudioCapture?.__emitChunkForTest();
    }
  });
  await expect(
    page.locator(".ant-message-warning").filter({ hasText: "已丢弃过期片段" }),
  ).toBeVisible();
  await expect.poll(() => requests, { timeout: 5_000 }).toBe(5);
  expect(maxConcurrent).toBe(1);
  expect(requests).toBeLessThanOrEqual(5);
});

declare global {
  interface Window {
    __resolveLateStream?: () => void;
    __lateStreamTrackStops?: number;
  }
}
