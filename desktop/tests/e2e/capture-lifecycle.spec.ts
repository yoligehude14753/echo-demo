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
  await expect(page.getByTestId("capture-status")).toHaveAttribute(
    "data-transport-warning",
    "backpressure",
  );
  await expect.poll(() => requests, { timeout: 5_000 }).toBe(5);
  expect(maxConcurrent).toBe(1);
  expect(requests).toBeLessThanOrEqual(5);
});

test("切换 backend origin 会丢弃旧 Capture 响应和排队音频", async ({
  page,
}) => {
  const backendA = "http://127.0.0.1:18881";
  const backendB = "http://127.0.0.1:18882";
  await page.addInitScript((base) => {
    window.localStorage.setItem("echodesk.mobileBackendBase", base);
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
    const navigatorWithMedia = window.navigator as unknown as {
      mediaDevices?: { getUserMedia: () => Promise<MediaStream> };
    };
    if (navigatorWithMedia.mediaDevices) {
      navigatorWithMedia.mediaDevices.getUserMedia = async () => {
        const context = new AudioContext();
        const destination = context.createMediaStreamDestination();
        const oscillator = context.createOscillator();
        oscillator.frequency.value = 0;
        oscillator.connect(destination);
        oscillator.start();
        return destination.stream;
      };
    }
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
  }, backendA);
  await installEchoMock(page);
  await page.goto("/");

  await page.evaluate(
    ({ originA, originB }) => {
      type FenceState = {
        aCompleted: number;
        aRequests: number;
        bRequests: number;
        releaseA: (() => void) | null;
      };
      type FenceWindow = Window & { __captureOriginFence?: FenceState };
      const target = window as FenceWindow;
      const originalFetch = window.fetch.bind(window);
      const state: FenceState = {
        aCompleted: 0,
        aRequests: 0,
        bRequests: 0,
        releaseA: null,
      };
      target.__captureOriginFence = state;
      const captureResponse = (text: string, audioRef: string) =>
        new Response(
          JSON.stringify({
            ambient_stored: true,
            ambient_text: text,
            audio_ref: audioRef,
            meeting_segments: [],
            stt_status: "ok",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );

      window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        if (url === `${originA}/capture/chunk`) {
          state.aRequests += 1;
          const response = await new Promise<Response>((resolve) => {
            // 故意忽略 init.signal，证明 router generation fence 独立成立。
            state.releaseA = () =>
              resolve(captureResponse("A 的延迟响应不得写入", "stale-a"));
          });
          state.aCompleted += 1;
          return response;
        }
        if (url === `${originB}/capture/chunk`) {
          state.bRequests += 1;
          return captureResponse("B 的当前响应", `fresh-b-${state.bRequests}`);
        }
        return originalFetch(input, init);
      };
    },
    { originA: backendA, originB: backendB },
  );

  await page.evaluate(() => window.__echoAudioCapture?.__emitChunkForTest());
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __captureOriginFence?: { aRequests: number };
            }
          ).__captureOriginFence?.aRequests ?? 0,
      ),
    )
    .toBe(1);

  // 第一段仍在 A 请求中，第二段属于 A generation，只能留在旧队列。
  await page.evaluate(() => window.__echoAudioCapture?.__emitChunkForTest());
  await page.evaluate(async (nextBase) => {
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase(nextBase);
    (
      window as unknown as {
        __captureOriginFence?: { releaseA: (() => void) | null };
      }
    ).__captureOriginFence?.releaseA?.();
  }, backendB);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __captureOriginFence?: { aCompleted: number };
            }
          ).__captureOriginFence?.aCompleted ?? 0,
      ),
    )
    .toBe(1);

  const afterSwitch = await page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    const fence = (
      window as unknown as {
        __captureOriginFence?: { aRequests: number; bRequests: number };
      }
    ).__captureOriginFence;
    return {
      aRequests: fence?.aRequests ?? 0,
      bRequests: fence?.bRequests ?? 0,
      ambientTexts: useStore
        .getState()
        .ambientSegments.map((segment) => segment.text),
    };
  });
  expect(afterSwitch).toEqual({
    aRequests: 1,
    bRequests: 0,
    ambientTexts: [],
  });
  await expect(page.getByTestId("capture-status")).toContainText(
    "已采集 0 · 已保存 0",
  );

  // B generation 的新音频仍应正常上传，不能被 A 的旧 drain 阻塞。
  await page.evaluate(() => window.__echoAudioCapture?.__emitChunkForTest());
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __captureOriginFence?: { bRequests: number };
            }
          ).__captureOriginFence?.bRequests ?? 0,
      ),
    )
    .toBe(1);
  await expect
    .poll(async () => {
      return page.evaluate(async () => {
        const { useStore } = await import("/src/store.ts");
        return useStore
          .getState()
          .ambientSegments.map((segment) => segment.text);
      });
    })
    .toEqual(["B 的当前响应"]);
  await expect(page.getByTestId("capture-status")).toContainText(
    "已采集 1 · 已保存 1",
  );
});

declare global {
  interface Window {
    __resolveLateStream?: () => void;
    __lateStreamTrackStops?: number;
  }
}
