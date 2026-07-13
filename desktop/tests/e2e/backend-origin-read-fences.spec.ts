import { expect, test, type Page } from "@playwright/test";

import { installEchoMock } from "./_mock";

const ORIGIN_SEED = "https://origin-seed.example";
const ORIGIN_A = "https://origin-a.example";
const ORIGIN_B = "https://origin-b.example";

const A_ARTIFACT_SECRET = "A_SECRET_ARTIFACT_NEVER_VISIBLE";
const A_TASK_SECRET = "A_SECRET_TASK_NEVER_VISIBLE";
const A_WORKSPACE_SECRET = "/A_SECRET_WORKSPACE_NEVER_VISIBLE";
const A_DOC_SECRET = "A_SECRET_DOC_NEVER_VISIBLE";
const A_MEETING_SECRET = "A_SECRET_MEETING_ID_NEVER_REUSED";

const B_ARTIFACT_TITLE = "B_ONLY_ARTIFACT";
const B_TASK_TITLE = "B_ONLY_TASK";
const B_WORKSPACE_PATH = "/B_ONLY_WORKSPACE";
const B_UPLOAD_DOC_TITLE = "B_ONLY_UPLOAD_DOC";
const B_MEETING_DOC_TITLE = "B_ONLY_MEETING_DOC";
const B_STARTED_MEETING_ID = "B_ONLY_STARTED_MEETING";

type DeferredReadKey =
  | "artifacts"
  | "tasks"
  | "workspace"
  | "docs"
  | "current";

interface SeenRequest {
  bodyText: string;
  key: DeferredReadKey | null;
  method: string;
  origin: string;
  path: string;
}

interface OriginReadFenceState {
  completed: Partial<Record<DeferredReadKey, number>>;
  pending: Partial<
    Record<DeferredReadKey, Array<(response: Response) => void>>
  >;
  requests: SeenRequest[];
  resolveAll(
    key: DeferredReadKey,
    payload: unknown,
    status?: number,
  ): void;
}

const bReadPayloads: Record<DeferredReadKey, unknown> = {
  artifacts: [
    {
      artifact_id: "artifact-b",
      artifact_type: "html",
      title: B_ARTIFACT_TITLE,
      file_path: "/tmp/artifact-b.html",
      mime_type: "text/html",
      size_bytes: 2048,
      generation_latency_ms: 12,
      model: "origin-fence-e2e",
      metadata: {},
    },
  ],
  tasks: [
    {
      task_id: "task-b",
      device_id: "device-b",
      title: B_TASK_TITLE,
      intent_text: "Run only on backend B",
      route: "codex",
      task_kind: "agent_task",
      state: "succeeded",
      progress_text: "completed on backend B",
      artifacts: [],
      snapshot: {
        progress_text: "completed on backend B",
        final_text: "B_ONLY_TASK_RESULT",
      },
      last_seq: 1,
      submitted_at: "2026-07-12T01:00:00Z",
      timeout_s: 60,
    },
  ],
  workspace: {
    configured_dirs: [B_WORKSPACE_PATH],
    authorized_dirs: [B_WORKSPACE_PATH],
    n_indexed: 22,
    max_file_mb: 100,
    scan_on_startup: true,
  },
  docs: {
    total: 2,
    by_source: {
      upload: [
        {
          doc_id: "doc-b-upload",
          title: B_UPLOAD_DOC_TITLE,
          kind: "markdown",
          source: "upload",
          source_path: "/b/upload.md",
          n_chunks: 3,
        },
      ],
      meeting: [
        {
          doc_id: "doc-b-meeting",
          title: B_MEETING_DOC_TITLE,
          kind: "text",
          source: "meeting",
          source_path: null,
          n_chunks: 4,
        },
      ],
    },
    docs: [
      {
        doc_id: "doc-b-upload",
        title: B_UPLOAD_DOC_TITLE,
        kind: "markdown",
        source: "upload",
        source_path: "/b/upload.md",
        n_chunks: 3,
      },
      {
        doc_id: "doc-b-meeting",
        title: B_MEETING_DOC_TITLE,
        kind: "text",
        source: "meeting",
        source_path: null,
        n_chunks: 4,
      },
    ],
  },
  current: {
    mode: "idle",
    meeting_id: null,
    started_at: null,
    started_by: null,
  },
};

async function openHarness(
  page: Page,
  deferKeys: DeferredReadKey[],
): Promise<void> {
  await page.addInitScript((origin) => {
    window.localStorage.setItem("echodesk.mobileBackendBase", origin);
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
  }, ORIGIN_SEED);
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  await expect(page.getByTestId("workspace-bar")).toBeVisible();
  await expect(page.getByTestId("meeting-status-bar")).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(async () => {
        const { useStore } = await import("/src/store.ts");
        return useStore.getState().connected;
      }),
    )
    .toBe(true);

  await page.evaluate(
    ({ deferred, originA, originB, payloads, startedMeetingId }) => {
      const originalFetch = window.fetch.bind(window);
      const deferredSet = new Set(deferred);
      const jsonResponse = (payload: unknown, status = 200): Response =>
        new Response(JSON.stringify(payload), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      const normalizePath = (pathname: string): string =>
        pathname.replace(/^\/api(?=\/)/, "");
      const readKey = (
        method: string,
        pathname: string,
      ): DeferredReadKey | null => {
        if (method !== "GET") return null;
        switch (normalizePath(pathname)) {
          case "/artifacts":
            return "artifacts";
          case "/agents/tasks":
            return "tasks";
          case "/workspace/status":
            return "workspace";
          case "/rag/docs":
            return "docs";
          case "/meetings/current":
            return "current";
          default:
            return null;
        }
      };
      const state: OriginReadFenceState = {
        completed: {},
        pending: {},
        requests: [],
        resolveAll(key, payload, status = 200) {
          const resolvers = state.pending[key] ?? [];
          state.pending[key] = [];
          for (const resolve of resolvers) {
            resolve(jsonResponse(payload, status));
          }
        },
      };
      (
        window as unknown as {
          __originReadFence__: OriginReadFenceState;
        }
      ).__originReadFence__ = state;

      window.fetch = async (
        input: RequestInfo | URL,
        init?: RequestInit,
      ): Promise<Response> => {
        const rawUrl =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const url = new URL(rawUrl, window.location.href);
        const requestMethod =
          input instanceof Request ? input.method : undefined;
        const method = (init?.method ?? requestMethod ?? "GET").toUpperCase();
        let bodyText = "";
        if (typeof init?.body === "string") {
          bodyText = init.body;
        } else if (init?.body instanceof URLSearchParams) {
          bodyText = init.body.toString();
        } else if (init?.body instanceof FormData) {
          const entries: string[] = [];
          init.body.forEach((value, name) => {
            entries.push(
              `${name}=${typeof value === "string" ? value : value.name}`,
            );
          });
          bodyText = entries.join("&");
        }
        const key = readKey(method, url.pathname);
        state.requests.push({
          bodyText,
          key,
          method,
          origin: url.origin,
          path: normalizePath(url.pathname),
        });

        if (url.origin === originA && key && deferredSet.has(key)) {
          // 刻意忽略 init.signal：即使 AbortController 已 abort，也让 A 响应返回，
          // 从而验证 generation fence 本身，而不是“恰好取消成功”。
          const response = await new Promise<Response>((resolve) => {
            const pending = state.pending[key] ?? [];
            pending.push(resolve);
            state.pending[key] = pending;
          });
          state.completed[key] = (state.completed[key] ?? 0) + 1;
          return response;
        }

        if (url.origin === originB && key) {
          return jsonResponse(payloads[key]);
        }

        if (
          url.origin === originB &&
          method === "POST" &&
          normalizePath(url.pathname) === "/meetings/manual_start"
        ) {
          return jsonResponse({
            mode: "in_meeting",
            meeting_id: startedMeetingId,
            started_at: "2026-07-12T01:05:00Z",
            started_by: "manual",
          });
        }

        return originalFetch(input, init);
      };
    },
    {
      deferred: deferKeys,
      originA: ORIGIN_A,
      originB: ORIGIN_B,
      payloads: bReadPayloads,
      startedMeetingId: B_STARTED_MEETING_ID,
    },
  );
}

async function switchBackend(page: Page, origin: string): Promise<void> {
  await page.evaluate(async (nextOrigin) => {
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase(nextOrigin);
    // _mock 的显式 close 标志需要在 origin handler 关闭旧 socket 后复位，
    // 才能让新 origin 的 mock socket 正常 open。
    (
      window as unknown as {
        __echoMock__: { wsClosed: boolean };
      }
    ).__echoMock__.wsClosed = false;
  }, origin);
}

async function waitForPending(
  page: Page,
  key: DeferredReadKey,
): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(
        (pendingKey) =>
          (
            window as unknown as {
              __originReadFence__: OriginReadFenceState;
            }
          ).__originReadFence__.pending[pendingKey]?.length ?? 0,
        key,
      ),
    )
    .toBeGreaterThan(0);
}

async function waitForRequest(
  page: Page,
  origin: string,
  key: DeferredReadKey,
): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(
        ({ expectedOrigin, expectedKey }) => {
          const state = (
            window as unknown as {
              __originReadFence__: OriginReadFenceState;
            }
          ).__originReadFence__;
          return state.requests.some(
            (request) =>
              request.origin === expectedOrigin && request.key === expectedKey,
          );
        },
        { expectedOrigin: origin, expectedKey: key },
      ),
    )
    .toBe(true);
}

async function resolveAll(
  page: Page,
  key: DeferredReadKey,
  payload: unknown,
): Promise<void> {
  const expected = await page.evaluate(
    (pendingKey) =>
      (
        window as unknown as {
          __originReadFence__: OriginReadFenceState;
        }
      ).__originReadFence__.pending[pendingKey]?.length ?? 0,
    key,
  );
  expect(expected).toBeGreaterThan(0);
  const completedBefore = await page.evaluate(
    (pendingKey) =>
      (
        window as unknown as {
          __originReadFence__: OriginReadFenceState;
        }
      ).__originReadFence__.completed[pendingKey] ?? 0,
    key,
  );
  await page.evaluate(
    ({ pendingKey, responsePayload }) => {
      (
        window as unknown as {
          __originReadFence__: OriginReadFenceState;
        }
      ).__originReadFence__.resolveAll(pendingKey, responsePayload);
    },
    { pendingKey: key, responsePayload: payload },
  );
  await expect
    .poll(() =>
      page.evaluate(
        (pendingKey) =>
          (
            window as unknown as {
              __originReadFence__: OriginReadFenceState;
            }
          ).__originReadFence__.completed[pendingKey] ?? 0,
        key,
      ),
    )
    .toBeGreaterThanOrEqual(completedBefore + expected);
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        window.requestAnimationFrame(() =>
          window.requestAnimationFrame(() => resolve()),
        ),
      ),
  );
}

async function storeSnapshot(page: Page): Promise<string> {
  return page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    const state = useStore.getState();
    return JSON.stringify({
      agentTasks: state.agentTasks,
      artifacts: state.artifacts,
      currentMeetingId: state.currentMeetingId,
      meetings: state.meetings,
    });
  });
}

test("A artifacts/tasks restore 延迟返回时不能污染 B store 或 DOM", async ({
  page,
}) => {
  await openHarness(page, ["artifacts", "tasks"]);
  await switchBackend(page, ORIGIN_A);
  await waitForPending(page, "artifacts");
  await waitForPending(page, "tasks");

  await switchBackend(page, ORIGIN_B);
  await page.getByTestId("inspector-tab-artifacts").click();
  await expect.poll(() => storeSnapshot(page)).toContain(B_ARTIFACT_TITLE);
  await expect.poll(() => storeSnapshot(page)).toContain(B_TASK_TITLE);
  await expect(
    page.getByTestId("artifact-title").filter({ hasText: B_ARTIFACT_TITLE }),
  ).toBeVisible();
  await expect(
    page.getByTestId("agent-task-card").filter({ hasText: B_TASK_TITLE }),
  ).toBeVisible();

  await resolveAll(page, "artifacts", [
    {
      artifact_id: "artifact-a-secret",
      artifact_type: "html",
      title: A_ARTIFACT_SECRET,
      file_path: "/tmp/artifact-a-secret.html",
      mime_type: "text/html",
      size_bytes: 4096,
      generation_latency_ms: 99,
      model: "origin-a",
      metadata: {},
    },
  ]);
  await resolveAll(page, "tasks", [
    {
      task_id: "task-a-secret",
      device_id: "device-a",
      title: A_TASK_SECRET,
      intent_text: A_TASK_SECRET,
      route: "codex",
      task_kind: "agent_task",
      state: "running",
      progress_text: A_TASK_SECRET,
      artifacts: [],
      snapshot: { progress_text: A_TASK_SECRET },
      last_seq: 0,
      submitted_at: "2026-07-12T00:00:00Z",
      timeout_s: 60,
    },
  ]);

  const snapshot = await storeSnapshot(page);
  expect(snapshot).toContain(B_ARTIFACT_TITLE);
  expect(snapshot).toContain(B_TASK_TITLE);
  expect(snapshot).not.toContain(A_ARTIFACT_SECRET);
  expect(snapshot).not.toContain(A_TASK_SECRET);
  await expect(page.locator("body")).not.toContainText(A_ARTIFACT_SECRET);
  await expect(page.locator("body")).not.toContainText(A_TASK_SECRET);
});

test("A workspace status/docs 延迟返回时只能展示 B 数据", async ({ page }) => {
  await openHarness(page, ["workspace", "docs"]);
  await switchBackend(page, ORIGIN_A);
  await waitForPending(page, "workspace");
  await waitForPending(page, "docs");

  await switchBackend(page, ORIGIN_B);
  const workspaceBar = page.getByTestId("workspace-bar");
  await expect(workspaceBar).toContainText("1 目录");
  await expect(workspaceBar).toContainText("22 文档");
  await expect(page.getByTestId("workspace-upload-count")).toContainText(
    "上传 1 · 会议 1 · 总计 2",
  );
  await page.getByTestId("workspace-config-btn").click();
  await expect(page.getByText(B_WORKSPACE_PATH, { exact: true })).toBeVisible();
  await expect(page.getByText(B_UPLOAD_DOC_TITLE, { exact: true })).toBeVisible();
  await expect(page.getByText(B_MEETING_DOC_TITLE, { exact: true })).toBeVisible();

  await resolveAll(page, "workspace", {
    configured_dirs: [A_WORKSPACE_SECRET],
    authorized_dirs: [A_WORKSPACE_SECRET],
    n_indexed: 91,
    max_file_mb: 100,
    scan_on_startup: true,
  });
  await resolveAll(page, "docs", {
    total: 1,
    by_source: {
      workspace: [
        {
          doc_id: "doc-a-secret",
          title: A_DOC_SECRET,
          kind: "text",
          source: "workspace",
          source_path: A_WORKSPACE_SECRET,
          n_chunks: 99,
        },
      ],
    },
    docs: [
      {
        doc_id: "doc-a-secret",
        title: A_DOC_SECRET,
        kind: "text",
        source: "workspace",
        source_path: A_WORKSPACE_SECRET,
        n_chunks: 99,
      },
    ],
  });

  await expect(workspaceBar).toContainText("1 目录");
  await expect(workspaceBar).toContainText("22 文档");
  await expect(page.getByText(B_WORKSPACE_PATH, { exact: true })).toBeVisible();
  await expect(page.getByText(B_UPLOAD_DOC_TITLE, { exact: true })).toBeVisible();
  await expect(page.locator("body")).not.toContainText(A_WORKSPACE_SECRET);
  await expect(page.locator("body")).not.toContainText(A_DOC_SECRET);
});

test("A current meeting 延迟返回时 B 保持 idle 且不会携 A id mutation", async ({
  page,
}) => {
  await openHarness(page, ["current"]);
  await switchBackend(page, ORIGIN_A);
  await waitForPending(page, "current");

  await switchBackend(page, ORIGIN_B);
  await waitForRequest(page, ORIGIN_B, "current");
  const statusBar = page.getByTestId("meeting-status-bar");
  await expect(statusBar).toContainText("待机");
  await expect(statusBar).toHaveAttribute("aria-pressed", "false");

  await resolveAll(page, "current", {
    mode: "in_meeting",
    meeting_id: A_MEETING_SECRET,
    started_at: "2026-07-12T00:00:00Z",
    started_by: "manual",
  });

  await expect(statusBar).toContainText("待机");
  await expect(statusBar).not.toContainText("会议中");
  await expect(statusBar).toHaveAttribute("aria-pressed", "false");
  const beforeClickMutations = await page.evaluate((origin) => {
    const state = (
      window as unknown as {
        __originReadFence__: OriginReadFenceState;
      }
    ).__originReadFence__;
    return state.requests.filter(
      (request) =>
        request.origin === origin &&
        request.method !== "GET" &&
        request.path.startsWith("/meetings/"),
    );
  }, ORIGIN_B);
  expect(beforeClickMutations).toEqual([]);

  // B 仍为 idle，因此下一次用户操作必须走 B 的 manual_start，而非沿用 A snapshot。
  await statusBar.click();
  await expect
    .poll(() =>
      page.evaluate((origin) => {
        const state = (
          window as unknown as {
            __originReadFence__: OriginReadFenceState;
          }
        ).__originReadFence__;
        return state.requests.some(
          (request) =>
            request.origin === origin &&
            request.method === "POST" &&
            request.path === "/meetings/manual_start",
        );
      }, ORIGIN_B),
    )
    .toBe(true);
  await expect(statusBar).toContainText("会议中");

  const bMeetingMutations = await page.evaluate((origin) => {
    const state = (
      window as unknown as {
        __originReadFence__: OriginReadFenceState;
      }
    ).__originReadFence__;
    return state.requests.filter(
      (request) =>
        request.origin === origin &&
        request.method !== "GET" &&
        request.path.startsWith("/meetings/"),
    );
  }, ORIGIN_B);
  expect(bMeetingMutations).toHaveLength(1);
  expect(bMeetingMutations[0]?.path).toBe("/meetings/manual_start");
  expect(JSON.stringify(bMeetingMutations)).not.toContain(A_MEETING_SECRET);
  expect(await storeSnapshot(page)).not.toContain(A_MEETING_SECRET);
});
