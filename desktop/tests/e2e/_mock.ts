/**
 * Playwright 测试公用：浏览器内 mock fetch + WebSocket。
 *
 * 设计：
 * - mockEcho(page) 注入页面，覆写 window.fetch 与 window.WebSocket
 * - mock 维护一个全局 controller (window.__echoMock__)，测试代码通过
 *   page.evaluate 推送 server event / 控制 ws 状态
 */
import type { Page } from "@playwright/test";

export interface EchoMockOptions {
  /** 默认模拟 Electron preload；纯浏览器契约测试显式传 false。 */
  isElectron?: boolean;
  /** 让指定路径返回非 2xx，触发前端 sad path 处理。
   *  键为 path 前缀（如 "/artifacts/generate"、"/rag/ask"），值为 HTTP 状态码。
   *  匹配规则：path.startsWith(key)。
   */
  errorPaths?: Record<string, number>;
  /** P3.1：默认会把 onboarding 标志预设为 completed（避免 Modal 遮挡）。
   *  专门测引导流程的 spec 传 true 关掉这个默认行为。
   */
  keepOnboarding?: boolean;
  /** 路径前缀列表；命中这些前缀的请求会被透传给真实 fetch，让 page.route()
   *  在 CDP 网络层接管 mock（适用于需要更丰富 fixture 的场景，如 /healthz/full
   *  / /admin/settings/remote）。 */
  skipPaths?: string[];
}

export interface EchoMock {
  publish(event: Record<string, unknown>): Promise<void>;
  closeWs(code?: number, reason?: string): Promise<void>;
  reopenWs(): Promise<void>;
  wsSent(): Promise<string[]>;
  fetchLog(): Promise<Array<{ url: string; method: string; bodyText?: string }>>;
}

export async function installEchoMock(
  page: Page,
  options: EchoMockOptions = {},
): Promise<EchoMock> {
  const errorPaths = options.errorPaths ?? {};
  const skipPaths = options.skipPaths ?? [];
  const isElectron = options.isElectron ?? true;

  // P3.1：onboarding 默认在 e2e 跳过，避免 Modal 遮挡所有交互；
  // 想专门测引导流程的 spec 用 `disableOnboardingSkip` opt-out（注：放在 addInitScript
  // 之前 page-scope 标志位 + 条件，让默认 spec 不用改）
  if (!options.keepOnboarding) {
    await page.addInitScript(() => {
      try {
        window.localStorage.setItem("echodesk.onboarding.completed", "1");
      } catch {
        /* ignore */
      }
    });
  }

  await page.addInitScript(
    ({ errorPaths, skipPaths, isElectron }: {
      errorPaths: Record<string, number>;
      skipPaths: string[];
      isElectron: boolean;
    }) => {
    type MockWs = {
      readyState: number;
      onopen?: (() => void) | null;
      onmessage?: ((e: MessageEvent) => void) | null;
      onclose?: ((e: CloseEvent) => void) | null;
      onerror?: (() => void) | null;
      send(data: string): void;
      close(code?: number, reason?: string): void;
    };

    const ctrl: {
      ws: MockWs | null;
      wsUrl?: string;
      wsSent: string[];
      wsClosed: boolean;
      fetchLog: Array<{ url: string; method: string; bodyText?: string }>;
      mockArtifactRunningId?: string;
      _seq: number;
    } = {
      ws: null,
      wsSent: [],
      wsClosed: false,
      fetchLog: [],
      _seq: 0,
    };
    (window as unknown as { __echoMock__: typeof ctrl }).__echoMock__ = ctrl;
    const existingEcho = (window as unknown as { echo?: Record<string, unknown> }).echo ?? {};
    (window as unknown as { echo: Record<string, unknown> }).echo = {
      ...existingEcho,
      isElectron,
      getShareBackendHost: async () => "http://192.168.50.10:8769",
    };

    // ── 覆写 WebSocket ─────────────────────────────────
    class MockWebSocket implements MockWs {
      readyState = 0;
      onopen: (() => void) | null = null;
      onmessage: ((e: MessageEvent) => void) | null = null;
      onclose: ((e: CloseEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      private _outbox: string[] = [];
      constructor(_url: string) {
        ctrl.wsUrl = _url;
        ctrl.ws = this;
        setTimeout(() => {
          if (ctrl.wsClosed) return;
          this.readyState = 1;
          this.onopen?.();
          // 自动回 server_hello
          this.dispatch({ type: "server_hello", seq: 0, ts: new Date().toISOString(), payload: { max_seq: 0, version: "1.0" } });
        }, 0);
      }
      send(data: string): void {
        this._outbox.push(data);
        ctrl.wsSent.push(data);
      }
      close(code = 1000, reason = ""): void {
        if (this.readyState === 3) return;
        this.readyState = 3;
        ctrl.wsClosed = true;
        this.onclose?.(new CloseEvent("close", { code, reason }));
      }
      dispatch(payload: Record<string, unknown>): void {
        if (this.readyState !== 1) return;
        this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(payload) }));
      }
    }
    (window as unknown as { WebSocket: typeof WebSocket }).WebSocket = MockWebSocket as unknown as typeof WebSocket;

    // ── 覆写 fetch (/api/*) ─────────────────────────────
    const realFetch = window.fetch.bind(window);
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const method = (init?.method ?? "GET").toUpperCase();
      let bodyText: string | undefined;
      if (init?.body && typeof init.body === "string") bodyText = init.body;
      ctrl.fetchLog.push({ url, method, bodyText });

      const path = url.replace(/^https?:\/\/[^/]+/, "");
      // skipPaths：让 page.route() 在 CDP 网络层接管（适合需要丰富 fixture 的 mock）
      for (const sp of skipPaths) {
        if (path.startsWith(sp) || path.startsWith(`/api${sp}`)) {
          return realFetch(input, init);
        }
      }
      // sad path 注入：匹配 errorPaths 前缀的请求直接返错误码
      for (const prefix of Object.keys(errorPaths)) {
        if (path.startsWith(prefix) || path.startsWith(`/api${prefix}`)) {
          const status = errorPaths[prefix];
          return new Response(
            JSON.stringify({ detail: `mocked failure ${status} for ${prefix}` }),
            { status, headers: { "Content-Type": "application/json" } },
          );
        }
      }
      if (
        (path === "/intent/route" || path === "/api/intent/route") &&
        method === "POST"
      ) {
        const intentMock = (
          window as unknown as {
            __echoIntentRouteMock?: {
              kind: string;
              confidence?: number;
              artifact_type?: string;
            };
          }
        ).__echoIntentRouteMock;
        if (intentMock) {
          const body = JSON.parse(bodyText ?? "{}") as { text?: string };
          const text = body.text ?? "";
          return new Response(
            JSON.stringify({
              kind: intentMock.kind,
              confidence: intentMock.confidence ?? 0.95,
              params: {
                artifact_type: intentMock.artifact_type ?? "html",
                brief: text.replace(/^@\S+\s*/, "") || "测试 HTML 报告",
              },
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
      }
      // 健康检查 / 占位
      if (path === "/healthz/full" || path === "/api/healthz/full") {
        return new Response(
          JSON.stringify({
            backend: { ok: true, version: "0.2.0-mock", port: 8769, uptime_s: 12.3 },
            db: { ok: true },
            remote: {},
            mic: { ok: "unknown" },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path === "/healthz" || path === "/api/healthz") {
        return new Response(JSON.stringify({ status: "ok" }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (path === "/bootstrap" || path === "/api/bootstrap") {
        return new Response(
          JSON.stringify({
            schema_version: 1,
            api_version: "0.3",
            backend_version: "0.3.1-mock",
            session_required: false,
            capabilities: {
              principal_sessions: true,
              owner_isolation: true,
              workflow_kernel: "dispatcher-v1",
              ws_owner_filtering: true,
              server_resync_rehydrate_required: true,
              host_runtime_requires_admin: false,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if ((path === "/session" || path === "/api/session") && method === "POST") {
        return new Response(
          JSON.stringify({
            token: "mock-session-token",
            expires_at: "2099-01-01T00:00:00Z",
            principal: {
              tenant_id: "mock-tenant",
              device_id: "mock-device",
              owner_id: "mock-owner",
              session_id: "mock-session",
              mode: "public",
            },
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path === "/tts/diag" || path === "/api/tts/diag" || path === "/tts/diag?fresh=true" || path === "/api/tts/diag?fresh=true") {
        return new Response(
          JSON.stringify({
            ok: true,
            state: "ok",
            detail: null,
            latency_ms: 42,
            pcm_bytes: 20480,
            rms: 3200,
            peak: 12000,
            voice: "aiden",
            base_url: "http://100.76.3.59:8094",
            checked_at: Date.now() / 1000,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path === "/tts/speak" || path === "/api/tts/speak") {
        return new Response(new Uint8Array(16000), {
          status: 200,
          headers: { "Content-Type": "application/octet-stream" },
        });
      }
      if (path === "/rag/ask" || path === "/api/rag/ask") {
        return new Response(
          JSON.stringify({
            answer: "Echo 已收到，这是 TV 问答文本回复。",
            citations: [],
            arbitration: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path === "/chat" || path === "/api/chat") {
        return new Response(
          JSON.stringify({ answer: "Echo 已收到，这是 TV 闲聊文本回复。" }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path === "/meetings/current" || path === "/api/meetings/current") {
        return new Response(
          JSON.stringify({
            mode: "idle",
            meeting_id: null,
            started_at: null,
            started_by: null,
            elapsed_sec: 0,
            recent_active_speakers: 0,
            recent_active_seconds: 0,
            minutes_status: null,
            minutes_error: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path === "/admin/settings/remote" || path === "/api/admin/settings/remote") {
        return new Response(
          JSON.stringify({
            config_path: "/Users/test/.echodesk/config.json",
            fields: [
              {
                key: "llm_main_base_url",
                value: "https://model.example.com/v1",
                sensitive: false,
                source: "default",
              },
              { key: "llm_main_api_key", value: "", sensitive: true, source: "default" },
              {
                key: "llm_fast_base_url",
                value: "https://model.example.com/v1",
                sensitive: false,
                source: "default",
              },
              {
                key: "stt_firered_url",
                value: "http://100.76.3.59:8090",
                sensitive: false,
                source: "default",
              },
              {
                key: "tts_qwen3_url",
                value: "http://100.76.3.59:8094",
                sensitive: false,
                source: "default",
              },
              { key: "tts_qwen3_voice", value: "aiden", sensitive: false, source: "default" },
              { key: "tavily_api_key", value: "", sensitive: true, source: "default" },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      // P2.5 管理 API：data-dir
      if (path === "/admin/data-dir" || path === "/api/admin/data-dir") {
        return new Response(
          JSON.stringify({
            path: "/Users/test/.echodesk",
            exists: true,
            size_bytes: 4096,
            breakdown: {
              db: 1024,
              storage: 0,
              rag_index: 2048,
              logs: 1024,
              skill_build: 0,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      // M_diag_brake：useEchoCapture 5s 轮询 /capture/stats，未 mock 时 fall
      // through 到 realFetch → vite proxy 报错 + DoorBreakdown 永远 "加载中"。
      // 默认返回 0 计数；spec 如需特定分布可用 page.route 覆盖。
      if (path === "/capture/stats" || path === "/api/capture/stats") {
        return new Response(
          JSON.stringify({
            chunks_total: 0,
            gated_rms: 0,
            gated_low_speech: 0,
            stt_circuit_open: 0,
            stt_failed: 0,
            stt_empty: 0,
            hallu_dropped: 0,
            diarize_failed: 0,
            diarize_returned_none: 0,
            stored: 0,
            last_chunk_at: null,
            last_stored_at: null,
            last_rms: 0,
            last_speech_ratio: 0,
            last_gate_reason: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      // workspace / rag/docs 默认返空（WorkspaceBar 30s 轮询调用）
      if (path.startsWith("/workspace/status") || path.startsWith("/api/workspace/status")) {
        return new Response(
          JSON.stringify({
            configured_dirs: [],
            authorized_dirs: [],
            n_indexed: 0,
            max_file_mb: 100,
            scan_on_startup: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path.startsWith("/rag/docs") || path.startsWith("/api/rag/docs")) {
        if (method === "GET") {
          return new Response(JSON.stringify({ total: 0, by_source: {}, docs: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
      }
      // 通用 ingest 默认 mock：返回伪 doc_id（除非 errorPaths 已拦截）
      if (path.startsWith("/rag/ingest") || path.startsWith("/api/rag/ingest")) {
        return new Response(
          JSON.stringify({ doc_id: `mock-${Date.now()}`, title: "mock" }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if ((path.startsWith("/meetings?") || path.startsWith("/api/meetings?")) && method === "GET") {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if ((path.startsWith("/capture/recent") || path.startsWith("/api/capture/recent")) && method === "GET") {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if ((path.startsWith("/artifacts?") || path.startsWith("/api/artifacts?")) && method === "GET") {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if ((path.startsWith("/agents/tasks?") || path.startsWith("/api/agents/tasks?")) && method === "GET") {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if ((path.startsWith("/workflows/runs?") || path.startsWith("/api/workflows/runs?")) && method === "GET") {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (path.match(/^\/(api\/)?meetings\/[^/]+\/transcript$/) && method === "GET") {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (path.match(/^\/(api\/)?meetings\/[^/]+\/minutes$/) && method === "GET") {
        return new Response(JSON.stringify({ detail: "minutes not generated yet" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (path.match(/^\/(api\/)?meetings\/[^/]+\/artifacts$/) && method === "GET") {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      // 生成产物：先 ack 200，UI 显示生成中；2 步：测试触发 ws artifact.ready
      if ((path === "/artifacts/generate" || path === "/api/artifacts/generate") && method === "POST") {
        ctrl._seq += 1;
        const body = JSON.parse(bodyText ?? "{}");
        const artifactId = `mock-${body.artifact_type}-${Date.now()}`;
        ctrl.mockArtifactRunningId = artifactId;
        const fake = {
          artifact_id: artifactId,
          artifact_type: body.artifact_type === "ppt" ? "pptx" : body.artifact_type,
          title: `mock ${body.artifact_type} 报告`,
          file_path: "/tmp/" + artifactId + ".out",
          mime_type: "application/octet-stream",
          size_bytes: 12345,
          generation_latency_ms: 1234,
          model: "MiniMax-M2.7-mock",
          metadata: { kind: body.artifact_type, model: "MiniMax-M2.7-mock" },
        };
        return new Response(JSON.stringify(fake), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (path.match(/^\/(api\/)?meetings\/[^/]+\/outputs$/) && method === "DELETE") {
        return new Response(
          JSON.stringify({
            meeting_id: "mock-meeting",
            minutes_cleared: true,
            artifact_ids: [],
            artifacts_deleted: 0,
            missing_artifact_ids: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path.match(/^\/(api\/)?meetings\/[^/]+\/share-ticket$/) && method === "POST") {
        const meetingId = path.split("/").at(-2) ?? "mock-meeting";
        return new Response(
          JSON.stringify({
            path: `/meetings/${meetingId}/share?share=mock-narrow-ticket`,
            expires_in_s: 3600,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      // meetings 类操作
      if ((path.startsWith("/meetings/") || path.startsWith("/api/meetings/")) && method === "POST") {
        return new Response(JSON.stringify({ status: "started", meeting_id: "x" }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      // 其它走真实 fetch
      return realFetch(input, init);
    };
  }, { errorPaths, skipPaths, isElectron });

  // 在 Node 上下文返回简单的 controller proxy
  const ctrl: EchoMock = {
    async publish(event) {
      await page.evaluate((e) => {
        const ws = (window as unknown as { __echoMock__: { ws: { dispatch?: (e: unknown) => void } | null } }).__echoMock__.ws;
        if (ws && typeof ws.dispatch === "function") ws.dispatch(e);
      }, event);
    },
    async closeWs(code, reason) {
      await page.evaluate(
        ([c, r]) => {
          const ws = (window as unknown as { __echoMock__: { ws: { close?: (c?: number, r?: string) => void } | null } }).__echoMock__.ws;
          if (ws && typeof ws.close === "function") ws.close(c as number | undefined, r as string | undefined);
        },
        [code, reason]
      );
    },
    async reopenWs() {
      await page.evaluate(() => {
        (window as unknown as { __echoMock__: { wsClosed: boolean } }).__echoMock__.wsClosed = false;
      });
    },
    async wsSent() {
      return await page.evaluate(() =>
        (window as unknown as { __echoMock__: { wsSent: string[] } }).__echoMock__.wsSent
      );
    },
    async fetchLog() {
      return await page.evaluate(() =>
        (window as unknown as { __echoMock__: { fetchLog: Array<{ url: string; method: string; bodyText?: string }> } }).__echoMock__.fetchLog
      );
    },
  };
  return ctrl;
}

export async function publishMeetingStarted(mock: EchoMock, meetingId: string, seq = 1): Promise<void> {
  await mock.publish({
    type: "meeting.started",
    seq,
    ts: new Date().toISOString(),
    meeting_id: meetingId,
    payload: {},
  });
}

export async function publishMinutesReady(mock: EchoMock, meetingId: string, seq = 2): Promise<void> {
  await mock.publish({
    type: "minutes.ready",
    seq,
    ts: new Date().toISOString(),
    meeting_id: meetingId,
    payload: {
      meeting_id: meetingId,
      title: "测试纪要",
      duration_sec: 60,
      speakers: ["说话人1"],
      summary: "这是一段测试纪要，包含 Q3 销售目标拆解的总结。",
      sections: [{ heading: "议题1", bullets: ["要点1", "要点2"] }],
      decisions: ["决议1"],
      action_items: ["行动项1"],
      created_at: new Date().toISOString(),
    },
  });
}

export async function publishMinutesFailed(
  mock: EchoMock,
  meetingId: string,
  error: string,
  seq = 2,
): Promise<void> {
  await mock.publish({
    type: "minutes.failed",
    seq,
    ts: new Date().toISOString(),
    meeting_id: meetingId,
    payload: { error },
  });
}

export async function publishMeetingEnded(
  mock: EchoMock,
  meetingId: string,
  seq = 2,
): Promise<void> {
  await mock.publish({
    type: "meeting.ended",
    seq,
    ts: new Date().toISOString(),
    meeting_id: meetingId,
    payload: { duration_sec: 60 },
  });
}

export async function publishArtifactReady(
  mock: EchoMock,
  artifactType: string,
  seq = 3,
  artifactId?: string,
  title?: string,
  filePath?: string,
  meetingId?: string,
): Promise<string> {
  const id = artifactId ?? `mock-${artifactType}-${Date.now()}`;
  await mock.publish({
    type: "artifact.ready",
    seq,
    ts: new Date().toISOString(),
    meeting_id: meetingId,
    payload: {
      artifact_id: id,
      artifact_type: artifactType,
      title: title ?? `mock ${artifactType} 产物`,
      file_path: filePath ?? `/tmp/${id}.out`,
      mime_type: "application/octet-stream",
      size_bytes: 12345,
      generation_latency_ms: 999,
      model: "MiniMax-M2.7-mock",
      metadata: { kind: artifactType },
    },
  });
  return id;
}
