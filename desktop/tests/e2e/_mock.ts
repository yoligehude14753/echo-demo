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
  /** 让指定路径返回非 2xx，触发前端 sad path 处理。
   *  键为 path 前缀（如 "/artifacts/generate"、"/rag/ask"），值为 HTTP 状态码。
   *  匹配规则：path.startsWith(key)。
   */
  errorPaths?: Record<string, number>;
}

export interface EchoMock {
  publish(event: Record<string, unknown>): Promise<void>;
  closeWs(code?: number, reason?: string): Promise<void>;
  reopenWs(): Promise<void>;
  fetchLog(): Promise<Array<{ url: string; method: string; bodyText?: string }>>;
}

export async function installEchoMock(
  page: Page,
  options: EchoMockOptions = {},
): Promise<EchoMock> {
  const errorPaths = options.errorPaths ?? {};
  await page.addInitScript((errorPaths: Record<string, number>) => {
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
      wsClosed: boolean;
      fetchLog: Array<{ url: string; method: string; bodyText?: string }>;
      mockArtifactRunningId?: string;
      _seq: number;
    } = {
      ws: null,
      wsClosed: false,
      fetchLog: [],
      _seq: 0,
    };
    (window as unknown as { __echoMock__: typeof ctrl }).__echoMock__ = ctrl;

    // ── 覆写 WebSocket ─────────────────────────────────
    class MockWebSocket implements MockWs {
      readyState = 0;
      onopen: (() => void) | null = null;
      onmessage: ((e: MessageEvent) => void) | null = null;
      onclose: ((e: CloseEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      private _outbox: string[] = [];
      constructor(_url: string) {
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
      // 健康检查 / 占位
      if (path.startsWith("/healthz")) {
        return new Response(JSON.stringify({ status: "ok" }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      // workspace / rag/docs 默认返空（WorkspaceBar 30s 轮询调用）
      if (path.startsWith("/workspace/status") || path.startsWith("/api/workspace/status")) {
        return new Response(
          JSON.stringify({
            configured_dirs: [],
            authorized_dirs: [],
            n_indexed: 0,
            max_file_mb: 20,
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
      // 生成产物：先 ack 200，UI 显示生成中；2 步：测试触发 ws artifact.ready
      if (path === "/artifacts/generate" && method === "POST") {
        ctrl._seq += 1;
        const body = JSON.parse(bodyText ?? "{}");
        const artifactId = `mock-${body.artifact_type}-${Date.now()}`;
        ctrl.mockArtifactRunningId = artifactId;
        const fake = {
          artifact_id: artifactId,
          artifact_type: body.artifact_type === "ppt" ? "pptx" : body.artifact_type,
          file_path: "/tmp/" + artifactId + ".out",
          mime_type: "application/octet-stream",
          size_bytes: 12345,
          generation_latency_ms: 1234,
          model: "MiniMax-M2.7-mock",
          metadata: { kind: body.artifact_type, model: "MiniMax-M2.7-mock" },
        };
        return new Response(JSON.stringify(fake), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      // meetings 类操作
      if (path.startsWith("/meetings/") && method === "POST") {
        return new Response(JSON.stringify({ status: "started", meeting_id: "x" }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      // 其它走真实 fetch
      return realFetch(input, init);
    };
  }, errorPaths);

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

export async function publishArtifactReady(
  mock: EchoMock,
  artifactType: string,
  seq = 3,
  artifactId?: string,
): Promise<string> {
  const id = artifactId ?? `mock-${artifactType}-${Date.now()}`;
  await mock.publish({
    type: "artifact.ready",
    seq,
    ts: new Date().toISOString(),
    payload: {
      artifact_id: id,
      artifact_type: artifactType,
      file_path: `/tmp/${id}.out`,
      mime_type: "application/octet-stream",
      size_bytes: 12345,
      generation_latency_ms: 999,
      model: "MiniMax-M2.7-mock",
      metadata: { kind: artifactType },
    },
  });
  return id;
}
