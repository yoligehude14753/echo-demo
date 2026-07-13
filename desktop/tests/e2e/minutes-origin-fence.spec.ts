import { expect, test, type Page } from "@playwright/test";

import { installEchoMock } from "./_mock";

const ORIGIN_A = "https://minutes-a.example";
const ORIGIN_B = "https://minutes-b.example";
const MEETING_ID = "shared-minutes-id";
const TODO_ID = "shared-todo-id";
const A_ARTIFACT_SECRET = "A_MINUTES_ARTIFACT_MUST_NOT_ENTER_B";
const B_ARTIFACT_TITLE = "B_MINUTES_ARTIFACT_ONLY";

interface MutationHarnessState {
  completedArtifact: number;
  completedFinalize: number;
  pendingArtifact: Array<(response: Response) => void>;
  pendingFinalize: Array<(response: Response) => void>;
  requests: Array<{ method: string; origin: string; path: string }>;
  resolveArtifact(payload: unknown): void;
  resolveFinalize(payload: unknown): void;
}

async function openHarness(page: Page): Promise<void> {
  await page.addInitScript((origin) => {
    window.localStorage.setItem("echodesk.mobileBackendBase", origin);
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
  }, ORIGIN_A);
  await installEchoMock(page);
  await page.goto("/");
  await expect(page.getByTestId("inspector-tab-minutes")).toBeVisible();

  await page.evaluate(
    ({ originA, originB, bArtifactTitle, meetingId }) => {
      const originalFetch = window.fetch.bind(window);
      const jsonResponse = (payload: unknown): Response =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      const state: MutationHarnessState = {
        completedArtifact: 0,
        completedFinalize: 0,
        pendingArtifact: [],
        pendingFinalize: [],
        requests: [],
        resolveArtifact(payload) {
          const resolvers = state.pendingArtifact.splice(0);
          for (const resolve of resolvers) resolve(jsonResponse(payload));
        },
        resolveFinalize(payload) {
          const resolvers = state.pendingFinalize.splice(0);
          for (const resolve of resolvers) resolve(jsonResponse(payload));
        },
      };
      (
        window as unknown as { __minutesMutationHarness__: MutationHarnessState }
      ).__minutesMutationHarness__ = state;

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
        const method = (
          init?.method ?? (input instanceof Request ? input.method : "GET")
        ).toUpperCase();
        const path = url.pathname.replace(/^\/api(?=\/)/, "");
        state.requests.push({ method, origin: url.origin, path });

        if (
          url.origin === originA &&
          method === "POST" &&
          path === `/meetings/${meetingId}/finalize`
        ) {
          // Deliberately ignore init.signal so generation fencing, not a lucky
          // network cancellation, protects the B state.
          const response = await new Promise<Response>((resolve) => {
            state.pendingFinalize.push(resolve);
          });
          state.completedFinalize += 1;
          return response;
        }
        if (
          url.origin === originA &&
          method === "POST" &&
          path === "/artifacts/generate"
        ) {
          const response = await new Promise<Response>((resolve) => {
            state.pendingArtifact.push(resolve);
          });
          state.completedArtifact += 1;
          return response;
        }
        if (
          url.origin === originB &&
          method === "POST" &&
          path === "/artifacts/generate"
        ) {
          return jsonResponse({
            artifact_id: "artifact-b-minutes",
            artifact_type: "pdf",
            title: bArtifactTitle,
            file_path: "/tmp/artifact-b-minutes.pdf",
            mime_type: "application/pdf",
            size_bytes: 2048,
            generation_latency_ms: 10,
            model: "origin-b",
            metadata: {},
          });
        }
        return originalFetch(input, init);
      };
    },
    {
      originA: ORIGIN_A,
      originB: ORIGIN_B,
      bArtifactTitle: B_ARTIFACT_TITLE,
      meetingId: MEETING_ID,
    },
  );
}

async function switchToB(page: Page): Promise<void> {
  await page.evaluate(async (origin) => {
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase(origin);
    (
      window as unknown as { __echoMock__: { wsClosed: boolean } }
    ).__echoMock__.wsClosed = false;
  }, ORIGIN_B);
}

async function setFailedMeeting(page: Page, title: string): Promise<void> {
  await page.evaluate(
    async ({ meetingId, meetingTitle }) => {
      const { useStore } = await import("/src/store.ts");
      const store = useStore.getState();
      store.reset();
      store.upsertMeeting(meetingId, {
        title: meetingTitle,
        state: "ended",
        minutes_status: "generation_failed",
        minutes_error: "timeout",
      });
      store.selectMeeting(meetingId);
    },
    { meetingId: MEETING_ID, meetingTitle: title },
  );
}

async function setActionableMinutes(page: Page, title: string): Promise<void> {
  await page.evaluate(
    async ({ meetingId, meetingTitle, todoId }) => {
      const { useStore } = await import("/src/store.ts");
      const store = useStore.getState();
      store.reset();
      store.upsertMeeting(meetingId, {
        title: meetingTitle,
        state: "ended",
        minutes_status: "ok",
        minutes: {
          meeting_id: meetingId,
          title: meetingTitle,
          duration_sec: 60,
          speakers: [],
          summary: `${meetingTitle} summary`,
          sections: [{ heading: "安排", bullets: ["生成 PDF"] }],
          decisions: [],
          todos: [
            {
              id: todoId,
              text: "生成 PDF 会后材料",
              kind: "actionable",
              status: "pending",
              assignee: null,
              done_at: null,
              artifact_id: null,
              suggested_command: "@生成 PDF 会后材料",
            },
          ],
          action_items: [],
          created_at: "2026-07-12T08:00:00Z",
        },
      });
      store.selectMeeting(meetingId);
    },
    { meetingId: MEETING_ID, meetingTitle: title, todoId: TODO_ID },
  );
}

async function waitForPending(
  page: Page,
  key: "pendingArtifact" | "pendingFinalize",
): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(
        (pendingKey) =>
          (
            window as unknown as {
              __minutesMutationHarness__: MutationHarnessState;
            }
          ).__minutesMutationHarness__[pendingKey].length,
        key,
      ),
    )
    .toBeGreaterThan(0);
}

test("A minutes retry cannot leave toast or busy state in B", async ({ page }) => {
  await openHarness(page);
  await setFailedMeeting(page, "A failed minutes");
  await page.getByTestId("inspector-tab-minutes").click();
  await page.getByTestId("minutes-retry-btn").click();
  await waitForPending(page, "pendingFinalize");

  await switchToB(page);
  await setFailedMeeting(page, "B failed minutes");
  await page.getByTestId("inspector-tab-minutes").click();
  await expect(page.getByTestId("minutes-retry-btn")).toBeEnabled();

  await page.evaluate((meetingId) => {
    (
      window as unknown as {
        __minutesMutationHarness__: MutationHarnessState;
      }
    ).__minutesMutationHarness__.resolveFinalize({
      meeting_id: meetingId,
      title: "A late minutes",
      duration_sec: 60,
      speakers: [],
      summary: "A secret late summary",
      sections: [],
      decisions: [],
      todos: [],
      action_items: [],
      created_at: "2026-07-12T08:00:00Z",
    });
  }, MEETING_ID);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __minutesMutationHarness__: MutationHarnessState;
            }
          ).__minutesMutationHarness__.completedFinalize,
      ),
    )
    .toBe(1);
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );

  await expect(page.getByText("已重新提交，等待 LLM 返回…")).toHaveCount(0);
  const snapshot = await page.evaluate(async (meetingId) => {
    const { useStore } = await import("/src/store.ts");
    const meeting = useStore.getState().meetings[meetingId];
    return {
      title: meeting?.title,
      minutesStatus: meeting?.minutes_status,
      minutes: meeting?.minutes ?? null,
    };
  }, MEETING_ID);
  expect(snapshot).toEqual({
    title: "B failed minutes",
    minutesStatus: "generation_failed",
    minutes: null,
  });
});

test("automatic todo is origin-scoped and late A artifact cannot enter B", async ({
  page,
}) => {
  await openHarness(page);
  await setActionableMinutes(page, "A minutes");
  await page.getByTestId("inspector-tab-minutes").click();
  await waitForPending(page, "pendingArtifact");

  await switchToB(page);
  await setActionableMinutes(page, "B minutes");
  await page.getByTestId("inspector-tab-minutes").click();
  await expect
    .poll(() =>
      page.evaluate(async () => {
        const { useStore } = await import("/src/store.ts");
        return useStore.getState().artifacts.map((artifact) => artifact.title);
      }),
    )
    .toContain(B_ARTIFACT_TITLE);

  await page.evaluate((secretTitle) => {
    (
      window as unknown as {
        __minutesMutationHarness__: MutationHarnessState;
      }
    ).__minutesMutationHarness__.resolveArtifact({
      artifact_id: "artifact-a-minutes-secret",
      artifact_type: "pdf",
      title: secretTitle,
      file_path: "/tmp/artifact-a-minutes-secret.pdf",
      mime_type: "application/pdf",
      size_bytes: 4096,
      generation_latency_ms: 99,
      model: "origin-a",
      metadata: {},
    });
  }, A_ARTIFACT_SECRET);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __minutesMutationHarness__: MutationHarnessState;
            }
          ).__minutesMutationHarness__.completedArtifact,
      ),
    )
    .toBe(1);
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );

  const result = await page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    return {
      artifactTitles: useStore.getState().artifacts.map((artifact) => artifact.title),
      autoExecKeys: Object.keys(window.localStorage).filter((key) =>
        key.startsWith("echodesk:auto-exec:v1:"),
      ),
      body: document.body.innerText,
    };
  });
  expect(result.artifactTitles).toContain(B_ARTIFACT_TITLE);
  expect(result.artifactTitles).not.toContain(A_ARTIFACT_SECRET);
  expect(result.body).not.toContain(A_ARTIFACT_SECRET);
  expect(result.autoExecKeys).toHaveLength(2);
  expect(result.autoExecKeys.some((key) => key.includes(encodeURIComponent(ORIGIN_A)))).toBe(
    true,
  );
  expect(result.autoExecKeys.some((key) => key.includes(encodeURIComponent(ORIGIN_B)))).toBe(
    true,
  );
});
