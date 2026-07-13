import { expect, test, type Page } from "@playwright/test";
import {
  installEchoMock,
  publishArtifactReady,
  publishMeetingStarted,
  publishMinutesReady,
} from "./_mock";

const ORIGIN_A = "http://127.0.0.1:19881";

async function configureInitialOrigin(page: Page, origin = ORIGIN_A): Promise<void> {
  await page.addInitScript((base) => {
    window.localStorage.setItem("echodesk.mobileBackendBase", base);
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
  }, origin);
}

async function switchBackend(page: Page, origin: string): Promise<void> {
  await page.evaluate(async (base) => {
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase(base);
  }, origin);
}

async function addArtifact(
  page: Page,
  artifactId: string,
  artifactType: string,
  filePath = `/tmp/${artifactId}.out`,
): Promise<void> {
  await page.evaluate(
    async ({ id, type, path }) => {
      const { useStore } = await import("/src/store.ts");
      useStore.getState().addArtifact({
        artifact_id: id,
        artifact_type: type,
        title: `origin fence ${type}`,
        file_path: path,
        mime_type: "application/octet-stream",
        size_bytes: 16,
        generation_latency_ms: 1,
        model: "origin-fence-test",
        metadata: {},
      });
    },
    { id: artifactId, type: artifactType, path: filePath },
  );
}

test("About 切换 origin 后丢弃延迟版本和数据目录", async ({ page }) => {
  await configureInitialOrigin(page);
  await installEchoMock(page);
  await page.goto("/");

  await page.evaluate((originA) => {
    const originalFetch = window.fetch.bind(window);
    const state: BackendLocalUiState = {
      counts: {},
      releases: {},
      openCalls: [],
    };
    const pending: Array<{
      url: string;
      resolve: (response: Response) => void;
    }> = [];
    state.releases.about = () => {
      for (const request of pending.splice(0)) {
        const body = request.url.endsWith("/healthz/full")
          ? { backend: { version: "A-SECRET-VERSION" } }
          : { path: "/A-SECRET-DATA-DIR" };
        request.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
    };
    window.__backendLocalUiState = state;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (
        url === `${originA}/healthz/full` ||
        url === `${originA}/admin/data-dir`
      ) {
        state.counts.about = (state.counts.about ?? 0) + 1;
        return new Promise<Response>((resolve) => pending.push({ url, resolve }));
      }
      return originalFetch(input, init);
    };
  }, ORIGIN_A);

  await page.getByTestId("open-about").click();
  await expect
    .poll(() => page.evaluate(() => window.__backendLocalUiState?.counts.about ?? 0))
    .toBe(2);

  await switchBackend(page, "http://127.0.0.1:19882");
  await page.evaluate(() => window.__backendLocalUiState?.releases.about?.());
  await expect(page.getByTestId("about-modal-body")).toBeHidden();
  await expect(page.getByText(/A-SECRET/)).toHaveCount(0);

  await page.getByTestId("open-about").click();
  await expect(page.getByTestId("about-backend-version")).toContainText(
    "0.2.0-mock",
  );
  await expect(page.getByTestId("about-modal-body")).not.toContainText(
    "A-SECRET",
  );
});

test("Onboarding 切换 origin 后关闭并丢弃延迟目录和权限结果", async ({
  page,
}) => {
  await configureInitialOrigin(page);
  await page.addInitScript(() => {
    const state: BackendLocalUiState = {
      counts: {},
      releases: {},
      openCalls: [],
    };
    window.__backendLocalUiState = state;
    window.echo = {
      getMicStatus: async () => "not-determined",
      requestMic: () => {
        state.counts.mic = (state.counts.mic ?? 0) + 1;
        return new Promise<boolean>((resolve) => {
          state.releases.mic = () => resolve(true);
        });
      },
    };
  });

  let releaseADataDir: (() => void) | undefined;
  const aDataDirGate = new Promise<void>((resolve) => {
    releaseADataDir = resolve;
  });
  await page.route(/\/admin\/data-dir$/, async (route) => {
    const requestUrl = new URL(route.request().url());
    const corsHeaders = {
      "Access-Control-Allow-Origin": "https://localhost:5174",
      "Access-Control-Allow-Headers": "x-echodesk-client-version, content-type",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Content-Type": "application/json",
    };
    if (route.request().method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: corsHeaders });
      return;
    }
    if (requestUrl.origin === ORIGIN_A) await aDataDirGate;
    try {
      await route.fulfill({
        status: 200,
        headers: corsHeaders,
        body: JSON.stringify({
          path:
            requestUrl.origin === ORIGIN_A
              ? "/A-SECRET-ONBOARDING-DIR"
              : "/B-CURRENT-ONBOARDING-DIR",
        }),
      });
    } catch {
      // Origin change aborts A at the transport layer; releasing the route may race it.
    }
  });

  await installEchoMock(page, {
    keepOnboarding: true,
    skipPaths: ["/admin/data-dir"],
  });
  await page.goto("/");
  await expect(page.getByText("欢迎来到 EchoDesk")).toBeVisible();
  await page.getByTestId("onboarding-next").click();
  await expect(page.getByTestId("onboarding-mic-request")).toBeVisible();
  await page.getByTestId("onboarding-mic-request").click();
  await expect
    .poll(() => page.evaluate(() => window.__backendLocalUiState?.counts.mic ?? 0))
    .toBe(1);

  await switchBackend(page, "http://127.0.0.1:19883");
  releaseADataDir?.();
  await page.evaluate(() => window.__backendLocalUiState?.releases.mic?.());
  await expect(page.getByText("欢迎来到 EchoDesk")).toBeHidden();
  await expect(page.getByText(/A-SECRET/)).toHaveCount(0);
  await expect(page.getByText("已允许")).toHaveCount(0);
});

test("MeetingShare 切换 origin 会关闭分享和确认框且旧删除不能清 B 产物", async ({
  page,
}) => {
  await configureInitialOrigin(page);
  const mock = await installEchoMock(page);
  await page.goto("/");

  const meetingId = "origin-share-a";
  const sharedArtifactId = "same-artifact-id-across-origins";
  await publishMeetingStarted(mock, meetingId, 1);
  await publishMinutesReady(mock, meetingId, 2);
  await publishArtifactReady(
    mock,
    "pdf",
    3,
    sharedArtifactId,
    "A artifact",
    "/tmp/a.pdf",
    meetingId,
  );

  await page.evaluate((originA) => {
    const originalFetch = window.fetch.bind(window);
    const state: BackendLocalUiState = {
      counts: {},
      releases: {},
      openCalls: [],
    };
    const pending: Array<{
      kind: "share" | "delete";
      resolve: (response: Response) => void;
    }> = [];
    state.releases.share = () => {
      for (const request of pending.splice(0)) {
        request.resolve(
          new Response(
            JSON.stringify(
              request.kind === "share"
                ? {
                    path: "/meetings/origin-share-a/share?share=A-SECRET-TICKET",
                    expires_in_s: 3600,
                  }
                : {
                    meeting_id: "origin-share-a",
                    minutes_cleared: true,
                    artifact_ids: ["same-artifact-id-across-origins"],
                    artifacts_deleted: 1,
                    missing_artifact_ids: [],
                  },
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
    };
    window.__backendLocalUiState = state;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const method = (init?.method ?? "GET").toUpperCase();
      const isShare =
        url === `${originA}/meetings/origin-share-a/share-ticket` &&
        method === "POST";
      const isDelete =
        url === `${originA}/meetings/origin-share-a/outputs` &&
        method === "DELETE";
      if (isShare || isDelete) {
        const kind = isShare ? "share" : "delete";
        state.counts[kind] = (state.counts[kind] ?? 0) + 1;
        return new Promise<Response>((resolve) => pending.push({ kind, resolve }));
      }
      return originalFetch(input, init);
    };
  }, ORIGIN_A);

  await page.getByTestId("inspector-tab-minutes").click();
  await page.getByTestId("open-meeting-share").click();
  await expect(page.getByTestId("meeting-share-modal")).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => window.__backendLocalUiState?.counts.share ?? 0))
    .toBe(1);
  await page.getByTestId("clear-meeting-outputs-btn").click();
  await page.locator(".ant-modal-confirm .ant-btn-dangerous").click();
  await expect
    .poll(() => page.evaluate(() => window.__backendLocalUiState?.counts.delete ?? 0))
    .toBe(1);

  await switchBackend(page, "http://127.0.0.1:19884");
  await addArtifact(page, sharedArtifactId, "pdf", "/tmp/b.pdf");
  await page.evaluate(() => window.__backendLocalUiState?.releases.share?.());
  await page.waitForTimeout(100);

  await expect(page.getByTestId("meeting-share-modal")).toBeHidden();
  await expect(page.locator(".ant-modal-confirm")).toHaveCount(0);
  await expect(page.getByText(/A-SECRET/)).toHaveCount(0);
  const remainingArtifactIds = await page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    return useStore.getState().artifacts.map((artifact) => artifact.artifact_id);
  });
  expect(remainingArtifactIds).toContain(sharedArtifactId);
});

test("Artifact preview 的文本、文档、表格、iframe 和系统打开均绑定 origin", async ({
  page,
}) => {
  await configureInitialOrigin(page, "http://127.0.0.1:19890");
  await installEchoMock(page);
  await page.goto("/");
  await page.getByTestId("inspector-tab-artifacts").click();

  await page.evaluate(() => {
    const originalFetch = window.fetch.bind(window);
    const state: BackendLocalUiState = {
      counts: {},
      releases: {},
      openCalls: [],
    };
    window.__backendLocalUiState = state;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const match = url.match(/\/artifacts\/([^/]+)\/download$/);
      if (match) {
        const artifactId = decodeURIComponent(match[1]);
        state.counts[artifactId] = (state.counts[artifactId] ?? 0) + 1;
        return new Promise<Response>((resolve) => {
          state.releases[artifactId] = () =>
            resolve(
              new Response(`A-SECRET-PREVIEW-${artifactId}`, {
                status: 200,
                headers: { "Content-Type": "application/octet-stream" },
              }),
            );
        });
      }
      return originalFetch(input, init);
    };
    const echo = window.echo ?? {};
    echo.openArtifactInSystem = (filePath: string) => {
      state.openCalls.push(filePath);
      state.counts.system = (state.counts.system ?? 0) + 1;
      return new Promise<void>((resolve) => {
        state.releases.system = resolve;
      });
    };
    window.echo = echo;
  });

  const asyncCases = [
    { type: "markdown", id: "origin-md" },
    { type: "txt", id: "origin-txt" },
    { type: "word", id: "origin-docx" },
    { type: "xlsx", id: "origin-xlsx" },
  ];
  let nextPort = 19891;
  for (const item of asyncCases) {
    const filePath = `/tmp/A-${item.id}.out`;
    await addArtifact(page, item.id, item.type, filePath);
    await page.locator(`[data-artifact-id="${item.id}"]`).click();
    await expect(page.getByTestId("preview-loading")).toBeVisible();
    await expect
      .poll(() =>
        page.evaluate(
          (id) => window.__backendLocalUiState?.counts[id] ?? 0,
          item.id,
        ),
      )
      .toBe(1);

    if (item.type === "xlsx") {
      await page.getByTestId("preview-open-in-system-btn").click();
      await expect
        .poll(() =>
          page.evaluate(() => window.__backendLocalUiState?.counts.system ?? 0),
        )
        .toBe(1);
    }

    await switchBackend(page, `http://127.0.0.1:${nextPort}`);
    nextPort += 1;
    await expect(page.getByTestId("preview-body")).toBeHidden();
    await page.evaluate((id) => {
      window.__backendLocalUiState?.releases[id]?.();
      window.__backendLocalUiState?.releases.system?.();
    }, item.id);
    await page.waitForTimeout(50);
    await expect(page.getByText(new RegExp(`A-SECRET-PREVIEW-${item.id}`))).toHaveCount(
      0,
    );
    await expect(page.getByText("已用系统应用打开")).toHaveCount(0);
  }

  const iframeId = "origin-pdf-iframe";
  await addArtifact(page, iframeId, "pdf", "/tmp/A-origin.pdf");
  await page.locator(`[data-artifact-id="${iframeId}"]`).click();
  await expect
    .poll(() =>
      page.evaluate((id) => window.__backendLocalUiState?.counts[id] ?? 0, iframeId),
    )
    .toBe(1);
  await page.evaluate((id) => window.__backendLocalUiState?.releases[id]?.(), iframeId);
  const frame = page.getByTestId("preview-iframe-pdf");
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute("src", /^blob:/);
  await switchBackend(page, `http://127.0.0.1:${nextPort}`);
  nextPort += 1;
  await expect(frame).toHaveCount(0);

  const pptxId = "origin-pptx-system";
  await addArtifact(page, pptxId, "pptx", "/tmp/A-origin.pptx");
  await page.locator(`[data-artifact-id="${pptxId}"]`).click();
  await expect
    .poll(() =>
      page.evaluate(() => window.__backendLocalUiState?.openCalls ?? []),
    )
    .toContain("/tmp/A-origin.pptx");
  await switchBackend(page, `http://127.0.0.1:${nextPort}`);
  await addArtifact(page, pptxId, "pptx", "/tmp/B-origin.pptx");
  await page.evaluate(() => window.__backendLocalUiState?.releases.system?.());
  await page.waitForTimeout(100);
  await expect(page.getByText("已用系统应用打开")).toHaveCount(0);
  const openCalls = await page.evaluate(
    () => window.__backendLocalUiState?.openCalls ?? [],
  );
  expect(openCalls).toEqual([
    "/tmp/A-origin-xlsx.out",
    "/tmp/A-origin.pptx",
  ]);
});

interface BackendLocalUiState {
  counts: Record<string, number>;
  releases: Record<string, (() => void) | undefined>;
  openCalls: string[];
}

declare global {
  interface Window {
    __backendLocalUiState?: BackendLocalUiState;
  }
}
