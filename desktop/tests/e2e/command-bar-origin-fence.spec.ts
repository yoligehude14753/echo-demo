import { expect, test, type Page } from "@playwright/test";
import { installEchoMock } from "./_mock";

const ORIGIN_A = "https://command-a.example";
const ORIGIN_B = "https://command-b.example";

type DeferredKey = "intent" | "ingest" | "artifact" | "chat" | "rag" | "task";

interface DeferredFetchState {
  completed: DeferredKey[];
  pending: Partial<Record<DeferredKey, (response: Response) => void>>;
  routeKind: string | null;
  seen: Array<{ method: string; url: string }>;
  resolve(key: DeferredKey, payload: Record<string, unknown>, status?: number): void;
}

async function openCommandBarHarness(
  page: Page,
  options: { defer: DeferredKey; routeKind?: string },
): Promise<void> {
  await page.addInitScript((origin) => {
    window.localStorage.setItem("echodesk.mobileBackendBase", origin);
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
  }, ORIGIN_A);
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  await page.evaluate(
    ({ defer, originA, routeKind }) => {
      const originalFetch = window.fetch.bind(window);
      const state: DeferredFetchState = {
        completed: [],
        pending: {},
        routeKind,
        seen: [],
        resolve(key, payload, status = 200) {
          const resolve = state.pending[key];
          if (!resolve) throw new Error(`no pending ${key} request`);
          delete state.pending[key];
          resolve(
            new Response(JSON.stringify(payload), {
              status,
              headers: { "Content-Type": "application/json" },
            }),
          );
        },
      };
      (
        window as unknown as { __commandBarOriginFence__: DeferredFetchState }
      ).__commandBarOriginFence__ = state;

      const requestKey = (url: URL, method: string): DeferredKey | null => {
        if (method !== "POST" || url.origin !== originA) return null;
        if (url.pathname === "/intent/route") return "intent";
        if (url.pathname === "/rag/ingest") return "ingest";
        if (url.pathname === "/artifacts/generate") return "artifact";
        if (url.pathname === "/chat") return "chat";
        if (url.pathname === "/rag/ask") return "rag";
        if (url.pathname === "/agents/tasks") return "task";
        return null;
      };

      window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const raw =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const url = new URL(raw, window.location.href);
        const method = (init?.method ?? "GET").toUpperCase();
        state.seen.push({ method, url: url.toString() });
        const key = requestKey(url, method);

        if (key === "intent" && defer !== "intent" && state.routeKind) {
          const body = JSON.parse(String(init?.body ?? "{}")) as { text?: string };
          const text = body.text ?? "";
          const params =
            state.routeKind === "chat_no_rag"
              ? { text }
              : state.routeKind === "agent_task"
                ? { text, title: text }
                : { question: text };
          return new Response(
            JSON.stringify({
              kind: state.routeKind,
              confidence: 0.99,
              params,
              rationale: "origin fence e2e",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }

        if (key === defer) {
          const response = await new Promise<Response>((resolve) => {
            state.pending[key] = resolve;
          });
          state.completed.push(key);
          return response;
        }
        return originalFetch(input, init);
      };
    },
    {
      defer: options.defer,
      originA: ORIGIN_A,
      routeKind: options.routeKind ?? null,
    },
  );
}

async function waitForPending(page: Page, key: DeferredKey): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(
        (pendingKey) =>
          Boolean(
            (
              window as unknown as {
                __commandBarOriginFence__: DeferredFetchState;
              }
            ).__commandBarOriginFence__.pending[pendingKey],
          ),
        key,
      ),
    )
    .toBe(true);
}

async function resolveDeferred(
  page: Page,
  key: DeferredKey,
  payload: Record<string, unknown>,
): Promise<void> {
  await page.evaluate(
    ({ pendingKey, responsePayload }) => {
      (
        window as unknown as { __commandBarOriginFence__: DeferredFetchState }
      ).__commandBarOriginFence__.resolve(pendingKey, responsePayload);
    },
    { pendingKey: key, responsePayload: payload },
  );
  await expect
    .poll(() =>
      page.evaluate(
        (pendingKey) =>
          (
            window as unknown as { __commandBarOriginFence__: DeferredFetchState }
          ).__commandBarOriginFence__.completed.includes(pendingKey),
        key,
      ),
    )
    .toBe(true);
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve())),
      ),
  );
}

async function switchToOriginB(page: Page): Promise<void> {
  await page.evaluate(async (origin) => {
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase(origin);
  }, ORIGIN_B);
}

test("origin switch clears pending upload and prefill while stale upload stays silent", async ({
  page,
}) => {
  await openCommandBarHarness(page, { defer: "ingest" });
  await page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    useStore.getState().prefillCommandBar("A 后端待办", {
      meeting_id: "meeting-a",
      todo_id: "todo-a",
    });
  });
  await expect(page.getByTestId("command-textarea")).toHaveValue("A 后端待办");

  await page.getByTestId("command-file-input").setInputFiles({
    name: "origin-a.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("origin A private document"),
  });
  await waitForPending(page, "ingest");
  await expect(page.getByText("正在添加 1 个文件…")).toBeVisible();

  await switchToOriginB(page);
  await expect(page.getByTestId("command-textarea")).toHaveValue("");
  await expect(page.getByText("正在添加 1 个文件…")).toHaveCount(0);
  await expect(page.getByTestId("pending-docs")).toHaveCount(0);
  await expect(page.getByTestId("command-attach-btn")).toBeEnabled();

  await resolveDeferred(page, "ingest", {
    doc_id: "doc-from-origin-a",
    title: "origin-a.md",
  });
  await expect(page.getByTestId("pending-docs")).toHaveCount(0);
  await expect(
    page.locator(".ant-message-success").filter({ hasText: "origin-a.md" }),
  ).toHaveCount(0);
  await expect(
    page.locator(".ant-message-error").filter({ hasText: "origin-a.md" }),
  ).toHaveCount(0);
});

test("stale A intent result cannot dispatch a mutation to B and busy is released", async ({
  page,
}) => {
  await openCommandBarHarness(page, { defer: "intent" });
  await page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    useStore.getState().prefillCommandBar("执行 A 后端任务", {
      meeting_id: "meeting-a",
      todo_id: "todo-a",
    });
  });
  await page.getByTestId("command-send-btn").click();
  await waitForPending(page, "intent");
  await expect(page.getByTestId("command-textarea")).toBeDisabled();

  await switchToOriginB(page);
  await expect(page.getByTestId("command-textarea")).toBeEnabled();
  await expect(page.getByTestId("command-textarea")).toHaveValue("");

  await resolveDeferred(page, "intent", {
    kind: "agent_task",
    confidence: 0.99,
    params: { text: "A response must not execute on B" },
    rationale: "stale origin A route",
  });
  const bMutations = await page.evaluate((originB) =>
    (
      window as unknown as { __commandBarOriginFence__: DeferredFetchState }
    ).__commandBarOriginFence__.seen.filter(
      (request) => request.method !== "GET" && request.url.startsWith(originB),
    ),
  ORIGIN_B);
  expect(bMutations).toEqual([]);
  await expect(
    page.locator(".ant-message-error").filter({ hasText: "发送失败" }),
  ).toHaveCount(0);
});

const staleResponseScenarios = [
  {
    name: "artifact",
    defer: "artifact" as const,
    input: "@生成 html origin-fence-artifact",
    routeKind: undefined,
    payload: {
      artifact_id: "artifact-from-a",
      artifact_type: "html",
      title: "stale-artifact-A",
      file_path: "/tmp/stale-a.html",
      mime_type: "text/html",
      size_bytes: 100,
      generation_latency_ms: 10,
      model: "test",
      metadata: {},
    },
    forbiddenToast: "已生成 html",
    marker: "stale-artifact-A",
  },
  {
    name: "chat",
    defer: "chat" as const,
    input: "stale-chat-question-A",
    routeKind: "chat_no_rag",
    payload: { answer: "stale-chat-answer-A" },
    forbiddenToast: "已回复",
    marker: "stale-chat-answer-A",
  },
  {
    name: "RAG",
    defer: "rag" as const,
    input: "stale-rag-question-A",
    routeKind: "search_rag",
    payload: {
      answer: "stale-rag-answer-A",
      citations: [],
      arbitration: "rag",
    },
    forbiddenToast: "已回答",
    marker: "stale-rag-answer-A",
  },
  {
    name: "agent task",
    defer: "task" as const,
    input: "stale-agent-task-A",
    routeKind: "agent_task",
    payload: {
      task_id: "task-from-a",
      device_id: "device-a",
      title: "stale-agent-task-A",
      intent_text: "stale-agent-task-A",
      route: "codex",
      task_kind: "agent_task",
      state: "running",
      progress_text: "running on A",
      artifacts: [],
      snapshot: {},
      last_seq: 0,
      submitted_at: "2026-07-12T00:00:00Z",
      timeout_s: 60,
    },
    forbiddenToast: "已开始后台执行",
    marker: "stale-agent-task-A",
  },
];

for (const scenario of staleResponseScenarios) {
  test(`stale A ${scenario.name} response cannot write B store, DOM or toast`, async ({
    page,
  }) => {
    await openCommandBarHarness(page, {
      defer: scenario.defer,
      routeKind: scenario.routeKind,
    });
    await page.getByTestId("command-textarea").fill(scenario.input);
    await page.getByTestId("command-send-btn").click();
    await waitForPending(page, scenario.defer);

    await switchToOriginB(page);
    await resolveDeferred(page, scenario.defer, scenario.payload);

    const snapshot = await page.evaluate(async () => {
      const { useStore } = await import("/src/store.ts");
      const state = useStore.getState();
      return {
        agentTasks: state.agentTasks,
        artifacts: state.artifacts,
        events: state.events,
      };
    });
    expect(JSON.stringify(snapshot)).not.toContain(scenario.marker);
    await expect(page.locator("body")).not.toContainText(scenario.marker);
    await expect(
      page
        .locator(".ant-message-notice")
        .filter({ hasText: scenario.forbiddenToast }),
    ).toHaveCount(0);
  });
}
