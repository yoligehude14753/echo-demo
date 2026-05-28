/**
 * 场景验证用 mock helpers（复用 e2e/_mock.ts 主体逻辑，但默认参数更适合演示）
 *
 * 主要差异：
 *  - 默认 keepOnboarding=false（除非场景显式要测引导）
 *  - 额外暴露 window.echo Electron IPC 兜底 mock（getMicStatus / openMicSystemPrefs /
 *    manualRestartBackend），让纯浏览器场景也能演示 Electron-only 功能
 *  - mock 多一组 health pill 用的 healthz/full 完整 fixture
 */
import type { Page } from "@playwright/test";
import { installEchoMock as installBase, type EchoMock, type EchoMockOptions } from "../e2e/_mock";

export interface ScenarioMockOptions extends EchoMockOptions {
  /** 模拟 Electron 环境（默认 true）：注入 window.echo + mic 权限态 */
  electron?: boolean;
  /** 麦克风权限初始值 */
  micPermission?: "granted" | "denied" | "not-determined";
  /** /healthz/full 返回的 remote.* 模板（默认全 ok）*/
  healthOverride?: "all-ok" | "yunwu-no-key" | "heyi-down";
}

export async function installScenarioMock(
  page: Page,
  opts: ScenarioMockOptions = {},
): Promise<EchoMock> {
  const electron = opts.electron ?? true;
  const micPerm = opts.micPermission ?? "granted";
  const health = opts.healthOverride ?? "all-ok";

  // 先注入 window.echo + navigator.permissions（必须在页面 JS 之前）
  // useBackendHealth 的 supervisor 状态由 onBackendStatus IPC 推；mic 由
  // navigator.permissions.query 直接读。两者都要 mock 才能让 pill 变绿/变色。
  if (electron) {
    await page.addInitScript(
      ({ mic }) => {
        const w = window as unknown as {
          echo?: Record<string, unknown>;
        };
        w.echo = {
          isElectron: true,
          getBackendHost: async () => "http://127.0.0.1:8769",
          // 模拟 BackendSupervisor IPC：订阅时立即推一次 external（dev 模式 backend
          // 由别的进程跑），让顶栏 backend pill 直接进入 ok 态
          onBackendStatus: (cb: (s: Record<string, unknown>) => void) => {
            setTimeout(() => cb({ state: "external", port: 8769 }), 0);
            return () => {
              /* noop unsubscribe */
            };
          },
          manualRestartBackend: async () => ({ ok: true }),
          getMicStatus: async () => mic,
          requestMic: async () => mic === "granted",
          openMicSystemPrefs: async () => ({ ok: true }),
        };

        // stub navigator.mediaDevices.getUserMedia —— 否则 useEchoCapture 拿不到
        // MediaStream，会不停弹「麦克风不可用：Not supported」toast 挡住 pill 点击
        const md = window.navigator as unknown as {
          mediaDevices?: { getUserMedia: (c: unknown) => Promise<MediaStream> };
        };
        if (md.mediaDevices) {
          md.mediaDevices.getUserMedia = async () => {
            // 返回带一条静音音轨的假 MediaStream，让 capture pipeline 安静运转
            const ctx = new AudioContext();
            const dst = ctx.createMediaStreamDestination();
            const osc = ctx.createOscillator();
            osc.frequency.value = 0;
            osc.connect(dst);
            osc.start();
            return dst.stream;
          };
        }

        // mock navigator.permissions.query —— useBackendHealth.fetchMicPermission 用它
        // 注意：navigator.permissions 在 Chromium 是 readonly accessor，直接赋值会
        // 在 strict mode 静默失败；用 Object.defineProperty 才能覆盖
        const permState = mic === "not-determined" ? "prompt" : mic;
        const fakePermissions = {
          query: async (q: { name: string }) => {
            if (q.name === "microphone") {
              return {
                state: permState,
                addEventListener: () => {
                  /* noop */
                },
                removeEventListener: () => {
                  /* noop */
                },
              } as unknown as { state: string };
            }
            return { state: "prompt" } as unknown as { state: string };
          },
        };
        try {
          Object.defineProperty(window.navigator, "permissions", {
            value: fakePermissions,
            configurable: true,
            writable: true,
          });
        } catch {
          // 部分浏览器 navigator 已 frozen；退而通过 prototype 改 query 本身
          const np = (window.navigator as unknown as { permissions?: { query: unknown } })
            .permissions;
          if (np) np.query = fakePermissions.query as unknown as typeof np.query;
        }
      },
      { mic: micPerm },
    );
  }

  // 注入 /healthz/full override 在 _mock.ts 通用 mock 之前
  // 现有 _mock.ts 已经 mock 了 /healthz/full 但格式简单，这里覆写更真实的内容
  await page.route(/\/(api\/)?healthz\/full$/, async (route) => {
    const remoteOk = {
      heyi_stt_firered: { ok: true, latency_ms: 38, checked_at: Math.floor(Date.now() / 1000) - 5 },
      heyi_tts_qwen3: { ok: true, latency_ms: 42, checked_at: Math.floor(Date.now() / 1000) - 5 },
      heyi_llm_fast: { ok: true, latency_ms: 51, checked_at: Math.floor(Date.now() / 1000) - 5 },
      yunwu_llm_main: { ok: true, latency_ms: 280, checked_at: Math.floor(Date.now() / 1000) - 5 },
      tavily: { ok: true, latency_ms: 220, checked_at: Math.floor(Date.now() / 1000) - 5 },
    };
    const remote =
      health === "all-ok"
        ? remoteOk
        : health === "yunwu-no-key"
          ? {
              ...remoteOk,
              yunwu_llm_main: { ok: null, reason: "no_api_key" },
              tavily: { ok: null, reason: "no_api_key" },
            }
          : // heyi-down
            {
              ...remoteOk,
              heyi_stt_firered: { ok: false, error: "Connection refused" },
              heyi_tts_qwen3: { ok: false, error: "Connection refused" },
              heyi_llm_fast: { ok: false, error: "Connection refused" },
            };

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        backend: { ok: true, version: "0.2.0", port: 8769, uptime_s: 142.7 },
        db: { ok: true, size_mb: 1.4 },
        remote,
        mic: { ok: "unknown" },
      }),
    });
  });

  // /admin/data-dir：SettingsPanel & OnboardingModal 都读，需要正确 shape
  await page.route(/\/(api\/)?admin\/data-dir$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        path: "/Users/test/.echodesk",
        exists: true,
        size_bytes: 12_345_678,
        breakdown: {
          db: 1_400_000,
          storage: 8_500_000,
          rag_index: 1_200_000,
          logs: 945_000,
          skill_build: 300_678,
        },
      }),
    });
  });

  // 远端设置：返回 7 个字段（默认值 + 一个 user override）
  await page.route(/\/(api\/)?admin\/settings\/remote$/, async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          config_path: "/Users/test/.echodesk/config.json",
          fields: [
            {
              key: "llm_main_base_url",
              value: "https://yunwu.ai/v1",
              sensitive: false,
              source: "default",
            },
            {
              key: "yunwu_open_key",
              value: "sk-abcd***wxyz",
              sensitive: true,
              source: "user",
            },
            {
              key: "llm_fast_base_url",
              value: "http://100.87.251.9:7860/v1",
              sensitive: false,
              source: "default",
            },
            {
              key: "stt_firered_url",
              value: "http://100.87.251.9:8090",
              sensitive: false,
              source: "default",
            },
            {
              key: "tts_qwen3_url",
              value: "http://100.87.251.9:8094",
              sensitive: false,
              source: "default",
            },
            {
              key: "tts_qwen3_voice",
              value: "longxiaocheng",
              sensitive: false,
              source: "default",
            },
            {
              key: "tavily_api_key",
              value: "",
              sensitive: true,
              source: "default",
            },
          ],
        }),
      });
      return;
    }
    if (req.method() === "PATCH") {
      const body = req.postDataJSON() ?? {};
      const updates: Record<string, string> = body.updates ?? {};
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          written_keys: Object.keys(updates),
          skipped_keys: [],
          restart_required: Object.keys(updates).length > 0,
          config_path: "/Users/test/.echodesk/config.json",
        }),
      });
      return;
    }
    await route.fallback();
  });

  // 让 _mock.ts 的 window.fetch 不要短路这两个路径，给 page.route() 接管
  return installBase(page, {
    ...opts,
    skipPaths: [
      ...(opts.skipPaths ?? []),
      "/healthz/full",
      "/admin/settings/remote",
      "/admin/data-dir",
    ],
  });
}

export { type EchoMock, publishArtifactReady, publishMeetingStarted, publishMinutesReady } from "../e2e/_mock";
