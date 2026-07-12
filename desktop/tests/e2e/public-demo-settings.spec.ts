import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

const MOCK_UPDATE_VERSION = "9.9.9";

test("公共 Electron 的远端 PPTX 不走本机路径 IPC，改用身份绑定下载", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const state = window as unknown as Window & {
      __publicArtifactOpenCalls?: number;
      echo?: Record<string, unknown>;
    };
    state.__publicArtifactOpenCalls = 0;
    state.echo = {
      isElectron: true,
      isPublicDemo: true,
      backendHost: "https://localhost:5174",
      ensurePublicSession: async () => ({
        token: "public-artifact-session",
        expires_at: "2099-01-01T00:00:00Z",
        backend_origin: "https://localhost:5174",
      }),
      renewPublicSession: async () => ({
        token: "public-artifact-session-renewed",
        expires_at: "2099-01-01T00:00:00Z",
        backend_origin: "https://localhost:5174",
      }),
      openArtifactInSystem: async () => {
        state.__publicArtifactOpenCalls =
          (state.__publicArtifactOpenCalls ?? 0) + 1;
      },
    };
  });
  await installEchoMock(page);
  await page.goto("/");
  await page.evaluate(async () => {
    const originalFetch = window.fetch.bind(window);
    const state = window as unknown as Window & {
      __publicArtifactAuthorization?: string;
      __publicArtifactDownloads?: number;
      __publicPptxObjectUrls?: string[];
      __publicPptxRevokedUrls?: string[];
    };
    state.__publicArtifactDownloads = 0;
    state.__publicPptxObjectUrls = [];
    state.__publicPptxRevokedUrls = [];
    const originalCreateObjectUrl = URL.createObjectURL.bind(URL);
    const originalRevokeObjectUrl = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = (blob: Blob) => {
      const objectUrl = originalCreateObjectUrl(blob);
      state.__publicPptxObjectUrls?.push(objectUrl);
      return objectUrl;
    };
    URL.revokeObjectURL = (objectUrl: string) => {
      state.__publicPptxRevokedUrls?.push(objectUrl);
      originalRevokeObjectUrl(objectUrl);
    };
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (/\/(?:api\/)?bootstrap$/.test(url)) {
        return new Response(
          JSON.stringify({
            schema_version: 1,
            api_version: "0.3",
            minimum_client_version: "0.3.1",
            session_required: true,
            capabilities: { principal_sessions: true },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (/\/artifacts\/public-pptx\/download$/.test(url)) {
        state.__publicArtifactDownloads =
          (state.__publicArtifactDownloads ?? 0) + 1;
        const headers = new Headers(
          init?.headers ?? (input instanceof Request ? input.headers : undefined),
        );
        state.__publicArtifactAuthorization = headers.get("Authorization") ?? "";
        return new Response(new Uint8Array([80, 75, 3, 4]), {
          status: 200,
          headers: { "Content-Type": "application/vnd.openxmlformats-officedocument.presentationml.presentation" },
        });
      }
      return originalFetch(input, init);
    };
    const session = await import("/src/session.ts");
    session.resetSessionForTest();
  });
  await page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    useStore.getState().addArtifact({
      artifact_id: "public-pptx",
      artifact_type: "pptx",
      title: "public presentation",
      file_path: null,
      mime_type:
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      size_bytes: 4,
      generation_latency_ms: 1,
      model: "public-test",
      metadata: {},
    });
  });

  const downloadPromise = page.waitForEvent("download");
  await page.locator('[data-artifact-id="public-pptx"]').click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("public presentation.pptx");
  const state = await page.evaluate(() => {
    const current = window as unknown as Window & {
      __publicArtifactOpenCalls?: number;
      __publicArtifactAuthorization?: string;
      __publicArtifactDownloads?: number;
      __publicPptxObjectUrls?: string[];
      __publicPptxRevokedUrls?: string[];
    };
    return {
      openCalls: current.__publicArtifactOpenCalls ?? 0,
      authorization: current.__publicArtifactAuthorization ?? "",
      downloads: current.__publicArtifactDownloads ?? 0,
      objectUrls: current.__publicPptxObjectUrls ?? [],
      revokedUrls: current.__publicPptxRevokedUrls ?? [],
    };
  });
  expect(state.openCalls).toBe(0);
  expect(state.authorization).toBe("Bearer public-artifact-session");
  expect(state.downloads).toBe(1);
  expect(state.objectUrls).toHaveLength(1);
  expect(state.revokedUrls).toEqual([]);

  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide")));
  await expect
    .poll(() =>
      page.evaluate(
        (objectUrl) =>
          (window as unknown as { __publicPptxRevokedUrls?: string[] })
            .__publicPptxRevokedUrls?.includes(objectUrl) ?? false,
        state.objectUrls[0],
      ),
    )
    .toBe(true);
});

test("公共 PPTX 下载失败会取消响应流并释放 transport lease", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { echo?: Record<string, unknown> }).echo = {
      isElectron: true,
      isPublicDemo: true,
      backendHost: "https://localhost:5174",
      ensurePublicSession: async () => ({
        token: "public-artifact-error-session",
        expires_at: "2099-01-01T00:00:00Z",
        backend_origin: "https://localhost:5174",
      }),
      renewPublicSession: async () => null,
    };
  });
  await installEchoMock(page);
  await page.goto("/");
  await page.evaluate(async () => {
    const originalFetch = window.fetch.bind(window);
    const state = window as unknown as Window & {
      __publicPptxCancelledBodies?: number;
    };
    state.__publicPptxCancelledBodies = 0;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (/\/(?:api\/)?bootstrap$/.test(url)) {
        return new Response(
          JSON.stringify({
            schema_version: 1,
            api_version: "0.3",
            minimum_client_version: "0.3.1",
            session_required: true,
            capabilities: { principal_sessions: true },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (/\/artifacts\/public-pptx-error\/download$/.test(url)) {
        return new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(new TextEncoder().encode("upstream failure"));
            },
            cancel() {
              state.__publicPptxCancelledBodies =
                (state.__publicPptxCancelledBodies ?? 0) + 1;
            },
          }),
          { status: 502, headers: { "Content-Type": "text/plain" } },
        );
      }
      return originalFetch(input, init);
    };
    const session = await import("/src/session.ts");
    session.resetSessionForTest();
    const { useStore } = await import("/src/store.ts");
    useStore.getState().addArtifact({
      artifact_id: "public-pptx-error",
      artifact_type: "pptx",
      title: "failing presentation",
      file_path: null,
      mime_type:
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      size_bytes: 16,
      generation_latency_ms: 1,
      model: "public-test",
      metadata: {},
    });
  });

  await page.locator('[data-artifact-id="public-pptx-error"]').click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as { __publicPptxCancelledBodies?: number })
            .__publicPptxCancelledBodies ?? 0,
      ),
    )
    .toBe(1);
});

test("公共 HTML 预览与下载只使用 authenticated bounded blob URL", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const host = window as unknown as {
      echo?: Record<string, unknown>;
      __htmlElectronDownloads?: Array<{
        blobUrl: string;
        suggestedFilename?: string;
        content: string;
      }>;
    };
    host.__htmlElectronDownloads = [];
    host.echo = {
      isElectron: true,
      isPublicDemo: true,
      backendHost: "https://localhost:5174",
      ensurePublicSession: async () => ({
        token: "public-html-session",
        expires_at: "2099-01-01T00:00:00Z",
        backend_origin: "https://localhost:5174",
      }),
      renewPublicSession: async () => null,
      downloadArtifactBlob: async (blobUrl: string, suggestedFilename?: string) => {
        const response = await fetch(blobUrl);
        host.__htmlElectronDownloads?.push({
          blobUrl,
          suggestedFilename,
          content: await response.text(),
        });
        return { ok: true, cancelled: false, filename: "public-html.html" };
      },
    };
  });
  await installEchoMock(page);
  await page.goto("/");
  await page.evaluate(async () => {
    const originalFetch = window.fetch.bind(window);
    const state = window as unknown as Window & {
      __htmlArtifactRequests?: Array<{ authorization: string; version: string }>;
      __htmlArtifactRevoked?: string[];
    };
    state.__htmlArtifactRequests = [];
    state.__htmlArtifactRevoked = [];
    const originalRevoke = URL.revokeObjectURL.bind(URL);
    URL.revokeObjectURL = (value: string) => {
      state.__htmlArtifactRevoked?.push(value);
      originalRevoke(value);
    };
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (/\/(?:api\/)?bootstrap$/.test(url)) {
        return new Response(
          JSON.stringify({
            schema_version: 1,
            api_version: "0.3",
            minimum_client_version: "0.3.1",
            session_required: true,
            capabilities: { principal_sessions: true },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (/\/artifacts\/public-html\/download$/.test(url)) {
        const headers = new Headers(
          init?.headers ?? (input instanceof Request ? input.headers : undefined),
        );
        state.__htmlArtifactRequests?.push({
          authorization: headers.get("Authorization") ?? "",
          version: headers.get("X-EchoDesk-Client-Version") ?? "",
        });
        return new Response("<script>window.top.__pwned = true</script><h1>safe</h1>", {
          status: 200,
          headers: { "Content-Type": "text/html" },
        });
      }
      return originalFetch(input, init);
    };
    const session = await import("/src/session.ts");
    session.resetSessionForTest();
    const { useStore } = await import("/src/store.ts");
    useStore.getState().addArtifact({
      artifact_id: "public-html",
      artifact_type: "html",
      title: "public html",
      file_path: null,
      mime_type: "text/html",
      size_bytes: 64,
      generation_latency_ms: 1,
      model: "public-test",
      metadata: {},
    });
  });

  await page.locator('[data-artifact-id="public-html"]').click();
  const frame = page.getByTestId("preview-iframe-html");
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute("src", /^blob:/);
  await expect(frame).toHaveAttribute("sandbox", "");
  const frameUrl = await frame.getAttribute("src");
  expect(await page.evaluate(() => (window as unknown as { __pwned?: boolean }).__pwned)).not.toBe(
    true,
  );
  await page.locator(".ant-modal-close").click();
  await expect(frame).toHaveCount(0);
  await expect.poll(() =>
    page.evaluate(
      (url) =>
        (window as unknown as { __htmlArtifactRevoked?: string[] })
          .__htmlArtifactRevoked?.includes(url ?? "") ?? false,
      frameUrl,
    ),
  ).toBe(true);

  await page.getByRole("button", { name: "下载public html" }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as { __htmlElectronDownloads?: unknown[] })
            .__htmlElectronDownloads?.length ?? 0,
      ),
    )
    .toBe(1);
  const [electronDownload] = await page.evaluate(
    () =>
      (window as unknown as {
        __htmlElectronDownloads?: Array<{
          blobUrl: string;
          suggestedFilename?: string;
          content: string;
        }>;
      }).__htmlElectronDownloads ?? [],
  );
  expect(electronDownload.blobUrl).toMatch(/^blob:/);
  expect(electronDownload.suggestedFilename).toBe("public html");
  expect(electronDownload.content).toContain("<h1>safe</h1>");
  expect(electronDownload).not.toHaveProperty("authorization");
  await expect
    .poll(() =>
      page.evaluate(
        (url) =>
          (window as unknown as { __htmlArtifactRevoked?: string[] })
            .__htmlArtifactRevoked?.includes(url) ?? false,
        electronDownload.blobUrl,
      ),
    )
    .toBe(true);
  const requests = await page.evaluate(
    () =>
      (window as unknown as {
        __htmlArtifactRequests?: Array<{ authorization: string; version: string }>;
      }).__htmlArtifactRequests ?? [],
  );
  expect(requests).toHaveLength(2);
  for (const request of requests) {
    expect(request.authorization).toBe("Bearer public-html-session");
    expect(request.version).toBe("0.3.1");
  }
});

test("公共演示 About 与设置页不探测主机管理接口，也不显示不可达噪声", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { echo?: Record<string, unknown> }).echo = {
      isElectron: true,
      isPublicDemo: true,
    };
  });
  await page.route(
    "https://api.github.com/repos/yoligehude14753/echo-demo/releases/latest",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          tag_name: `v${MOCK_UPDATE_VERSION}`,
          name: `EchoDesk v${MOCK_UPDATE_VERSION}`,
          html_url: `https://github.com/yoligehude14753/echo-demo/releases/tag/v${MOCK_UPDATE_VERSION}`,
          assets: [],
        }),
      });
    },
  );
  await page.route(/\/(api\/)?workspace\/status$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        configured_dirs: [],
        authorized_dirs: [],
        n_indexed: 0,
        max_file_mb: 100,
        scan_on_startup: true,
      }),
    });
  });

  const mock = await installEchoMock(page, { skipPaths: ["/workspace/status"] });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.getByTestId("open-about").click();
  await expect(page.getByTestId("about-backend-version")).toHaveText("公共服务");
  await expect(page.getByTestId("about-modal-body")).not.toContainText("暂时无法连接");
  await page.locator(".ant-modal-close").first().focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".ant-modal-wrap")).toBeHidden();

  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("settings-host-data")).toHaveCount(0);
  await expect(page.getByTestId("settings-host-model")).toHaveCount(0);
  await expect(page.getByTestId("remote-settings-form")).toHaveCount(0);
  await expect(page.getByText("公共演示服务不开放本机数据目录")).toHaveCount(0);
  await expect(page.getByTestId("mobile-backend-base")).toHaveValue(
    "https://echodesk.yoliyoli.uk",
  );
  await page.getByTestId("check-updates").click();
  await expect(page.getByTestId("update-status-tag")).toContainText("暂无适用安装包");

  const hostAdminPaths = new Set([
    "/healthz/full",
    "/api/healthz/full",
    "/admin/data-dir",
    "/api/admin/data-dir",
    "/admin/settings/remote",
    "/api/admin/settings/remote",
  ]);
  const requestedPaths = (await mock.fetchLog()).map(
    (entry) => new URL(entry.url, page.url()).pathname,
  );
  expect(requestedPaths.filter((path) => hostAdminPaths.has(path))).toEqual([]);
});

test("公共桌面包工作区目录读取本机 IPC 而不是远端后端", async ({ page }) => {
  await page.addInitScript(() => {
    const state = window as unknown as Window & {
      __localWorkspaceScanCalls?: number;
      __localWorkspaceOrigins?: string[];
      echo?: Record<string, unknown>;
    };
    state.__localWorkspaceScanCalls = 0;
    state.__localWorkspaceOrigins = [];
    const recordOrigin = (context: { expectedBackendOrigin?: string }): void => {
      state.__localWorkspaceOrigins?.push(context.expectedBackendOrigin ?? "missing");
    };
    state.echo = {
      isElectron: true,
      isPublicDemo: true,
      backendHost: window.location.origin,
      getLocalWorkspaceStatus: async (context: {
        expectedBackendOrigin?: string;
      }) => {
        recordOrigin(context);
        return {
          configured_dirs: ["/Users/test/Knowledge"],
          authorized_dirs: ["/Users/test/Knowledge"],
          n_indexed: 7,
          max_file_mb: 100,
          scan_on_startup: false,
        };
      },
      scanLocalWorkspaces: async (context: {
        expectedBackendOrigin?: string;
      }) => {
        recordOrigin(context);
        state.__localWorkspaceScanCalls = (state.__localWorkspaceScanCalls ?? 0) + 1;
        return {
          n_total: 7,
          n_added: 0,
          n_updated: 0,
          n_removed: 0,
          n_skipped: 7,
          n_failed: 0,
          duration_s: 0.01,
          errors: [],
        };
      },
      clearLocalWorkspaceDocs: async (context: {
        expectedBackendOrigin?: string;
      }) => {
        recordOrigin(context);
        return { n_removed: 0 };
      },
      addLocalWorkspaceDir: async (
        context: { expectedBackendOrigin?: string },
        dir: string,
      ) => {
        recordOrigin(context);
        return {
          added: true,
          path: dir,
          configured_dirs: [dir],
        };
      },
      removeLocalWorkspaceDir: async (
        context: { expectedBackendOrigin?: string },
        dir: string,
      ) => {
        recordOrigin(context);
        return {
          removed: true,
          path: dir,
          configured_dirs: [],
        };
      },
      cancelLocalWorkspaceOperations: async () => ({ cancelled: 0 }),
    };
  });
  const mock = await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect(page.getByTestId("workspace-dirs-tag")).toContainText("1 目录");
  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("workspace-settings-section")).toBeVisible();
  await expect(page.getByTestId("workspace-dir-row")).toContainText("/Users/test/Knowledge");
  await expect(page.getByTestId("workspace-settings-section")).toContainText(
    "超过 100 MB 的单文件会跳过",
  );

  await page.getByTestId("workspace-rescan").click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as Window & { __localWorkspaceScanCalls?: number })
            .__localWorkspaceScanCalls ?? 0,
      ),
    )
    .toBe(1);

  const fetchLog = await mock.fetchLog();
  expect(fetchLog.some((r) => /\/(api\/)?workspace\/status/.test(r.url))).toBe(false);
  const origins = await page.evaluate(
    () =>
      (window as unknown as Window & { __localWorkspaceOrigins?: string[] })
        .__localWorkspaceOrigins ?? [],
  );
  expect(origins.length).toBeGreaterThan(0);
  expect(new Set(origins)).toEqual(new Set([new URL(page.url()).origin]));
});

test("默认公共服务客户端隐藏 host-only 工作区，但保留知识库文档管理", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "echodesk.mobileBackendBase",
      "https://echodesk.yoliyoli.uk",
    );
    window.localStorage.setItem(
      "echodesk.publicDataBoundary.v2",
      JSON.stringify({ schema: 3, appVersion: "0.3.1" }),
    );
  });
  const mock = await installEchoMock(page, { isElectron: false });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const runtimeCapability = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const api = await import("/src/api.ts");
    return {
      publicRuntime: runtime.isPublicRuntime(),
      workspace: api.workspaceCapability(),
      isElectron: window.echo?.isElectron,
      isPublicDemo: window.echo?.isPublicDemo,
    };
  });
  expect(runtimeCapability).toEqual({
    publicRuntime: true,
    workspace: "unavailable",
    isElectron: false,
    isPublicDemo: undefined,
  });

  await expect(page.getByTestId("workspace-bar")).toBeVisible();
  await expect(page.getByTestId("knowledge-docs-tag")).toBeVisible();
  await expect(page.getByTestId("workspace-dirs-tag")).toHaveCount(0);

  await page.getByTestId("workspace-config-btn").click();
  const modal = page.locator(".ant-modal-content").filter({ hasText: "管理知识库" });
  await expect(modal).toBeVisible();
  await expect(modal.getByTestId("workspace-scan-btn")).toHaveCount(0);
  await expect(modal.getByTestId("workspace-open-settings")).toHaveCount(0);
  await expect(modal.getByTestId("workspace-clear-btn")).toHaveCount(0);
  await expect(modal).not.toContainText("目录配置");
  await expect(modal).toContainText("可将文件直接拖到输入框");
  await modal.locator(".ant-modal-close").click();

  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("workspace-settings-section")).toHaveCount(0);

  const workspaceRequests = (await mock.fetchLog()).filter((entry) =>
    /\/(?:api\/)?workspace(?:\/|$)/.test(
      new URL(entry.url, page.url()).pathname,
    ),
  );
  expect(workspaceRequests).toEqual([]);
});

test("公共 native 显式连接自建 backend 时保留服务器工作区能力", async ({
  page,
}) => {
  const privateBackend = "https://private-workspace.example";
  await page.addInitScript((backend) => {
    window.localStorage.setItem("echodesk.mobileBackendBase", backend);
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
    window.localStorage.setItem(
      "echodesk.publicDataBoundary.v2",
      JSON.stringify({ schema: 3, appVersion: "0.3.1", explicitBackend: true }),
    );
    (
      window as unknown as {
        Capacitor?: { isNativePlatform: () => boolean };
      }
    ).Capacitor = { isNativePlatform: () => true };
  }, privateBackend);
  const mock = await installEchoMock(page, { isElectron: false });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect(page.getByTestId("workspace-dirs-tag")).toBeVisible();
  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("workspace-settings-section")).toBeVisible();

  await expect
    .poll(async () =>
      (await mock.fetchLog()).some((entry) => {
        const url = new URL(entry.url, page.url());
        return (
          url.origin === privateBackend &&
          /\/(?:api\/)?workspace\/status$/.test(url.pathname)
        );
      }),
    )
    .toBe(true);
});

test("公共演示启动会清理旧历史状态和非显式服务地址", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { echo?: Record<string, unknown> }).echo = {
      isElectron: true,
      isPublicDemo: true,
    };
    window.localStorage.setItem("echodesk.mobileBackendBase", "http://10.10.12.32:8769");
    window.localStorage.setItem("echodesk.currentMeetingId", "m-old");
    window.localStorage.setItem("echodesk.capture.recent", "[{\"text\":\"old\"}]");
  });
  const mock = await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase")),
  ).toBeNull();
  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.currentMeetingId")),
  ).toBeNull();
  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.capture.recent")),
  ).toBeNull();
  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.publicDataBoundary.v2")),
  ).toContain('"schema":3');

  const fetchLog = await mock.fetchLog();
  // Public sessions are server-isolated in 0.3, so full REST hydrate is required.
  expect(fetchLog.some((r) => /\/(api\/)?meetings\?/.test(r.url))).toBe(true);
  expect(fetchLog.some((r) => /\/(api\/)?capture\/recent/.test(r.url))).toBe(false);
});

test("公共演示已完成数据边界迁移后不会在每次启动清空本机历史", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { echo?: Record<string, unknown> }).echo = {
      isElectron: true,
      isPublicDemo: true,
    };
    window.localStorage.setItem(
      "echodesk.publicDataBoundary.v2",
      JSON.stringify({ schema: 3, appVersion: "0.2.22" }),
    );
    window.localStorage.setItem("echodesk.currentMeetingId", "m-local-after-migration");
    window.localStorage.setItem(
      "echodesk.capture.recent",
      JSON.stringify([{ text: "迁移后的本机转写" }]),
    );
    window.localStorage.setItem(
      "echodesk.localCaptureState.v1",
      JSON.stringify({
        schema: 1,
        appVersion: "0.2.19",
        savedAt: new Date().toISOString(),
        currentMeetingId: "m-local-after-migration",
        meetings: [],
        ambientSegments: [{ text: "迁移后的本机转写", captured_at: new Date().toISOString(), speaker_id: null, speaker_label: null, duration_ms: 0 }],
        artifacts: [],
      }),
    );
  });
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.currentMeetingId")),
  ).toBe("m-local-after-migration");
  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.capture.recent")),
  ).toContain("迁移后的本机转写");
  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.localCaptureState.v1")),
  ).toContain("迁移后的本机转写");
});

test("公共演示显式保存过的自定义服务地址会保留，并允许加载私有历史", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { echo?: Record<string, unknown> }).echo = {
      isElectron: true,
      isPublicDemo: true,
    };
    window.localStorage.setItem("echodesk.mobileBackendBase", "http://10.10.12.32:8769");
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
  });
  const mock = await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect.poll(
    () => page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase")),
  ).toBe("http://10.10.12.32:8769");

  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("mobile-backend-base")).toHaveValue("http://10.10.12.32:8769");

  const fetchLog = await mock.fetchLog();
  expect(fetchLog.some((r) => /\/(api\/)?meetings\?/.test(r.url))).toBe(true);
});

test("服务端版本落后时顶部和设置页都显式警告", async ({ page }) => {
  await page.route(/\/(api\/)?healthz\/full$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        backend: { ok: true, version: "0.2.5", port: 8769, uptime_s: 12.3 },
        db: { ok: true },
        remote: {},
        mic: { ok: "unknown" },
      }),
    });
  });
  await installEchoMock(page, { skipPaths: ["/healthz/full"] });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.getByTestId("pill-backend").click();
  await expect(page.getByTestId("backend-version-warning")).toContainText(
    "服务端还是 v0.2.5",
  );
  await page.keyboard.press("Escape");

  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("settings-backend-version")).toContainText(
    "落后于客户端",
  );
});

test("设置页：检查更新会展示当前平台优选 release 资产", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window.navigator, "userAgent", {
      value: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      configurable: true,
    });
  });
  await page.route(
    "https://api.github.com/repos/yoligehude14753/echo-demo/releases/latest",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          tag_name: `v${MOCK_UPDATE_VERSION}`,
          name: `EchoDesk v${MOCK_UPDATE_VERSION}`,
          html_url: `https://github.com/yoligehude14753/echo-demo/releases/tag/v${MOCK_UPDATE_VERSION}`,
          assets: [
            {
              name: `EchoDesk.Setup.${MOCK_UPDATE_VERSION}.exe`,
              size: 123,
              browser_download_url:
                `https://github.com/yoligehude14753/echo-demo/releases/download/v${MOCK_UPDATE_VERSION}/EchoDesk.Setup.${MOCK_UPDATE_VERSION}.exe`,
            },
            {
              name: `EchoDesk-${MOCK_UPDATE_VERSION}-smart-tv.apk`,
              size: 456,
              browser_download_url:
                `https://github.com/yoligehude14753/echo-demo/releases/download/v${MOCK_UPDATE_VERSION}/EchoDesk-${MOCK_UPDATE_VERSION}-smart-tv.apk`,
            },
            {
              name: `EchoDesk-${MOCK_UPDATE_VERSION}-arm64.dmg`,
              size: 789,
              browser_download_url:
                `https://github.com/yoligehude14753/echo-demo/releases/download/v${MOCK_UPDATE_VERSION}/EchoDesk-${MOCK_UPDATE_VERSION}-arm64.dmg`,
            },
          ],
        }),
      });
    },
  );
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("updates-section")).toBeVisible();
  await page.getByTestId("check-updates").click();

  await expect(page.getByTestId("update-status-tag")).toContainText("发现新版本");
  await expect(
    page.getByTestId("updates-section").getByText(`v${MOCK_UPDATE_VERSION}`),
  ).toBeVisible();
  await expect(page.getByTestId("update-asset-name")).toContainText(
    `EchoDesk.Setup.${MOCK_UPDATE_VERSION}.exe`,
  );
  await expect(page.getByTestId("install-update")).toBeEnabled();
});

test("桌面 Web 遇到仅 Android 的新版时不显示可用更新", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window.navigator, "userAgent", {
      value: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      configurable: true,
    });
  });
  await page.route(
    "https://api.github.com/repos/yoligehude14753/echo-demo/releases/latest",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          tag_name: `v${MOCK_UPDATE_VERSION}`,
          name: `EchoDesk v${MOCK_UPDATE_VERSION}`,
          html_url: `https://github.com/yoligehude14753/echo-demo/releases/tag/v${MOCK_UPDATE_VERSION}`,
          assets: [
            {
              name: `EchoDesk-${MOCK_UPDATE_VERSION}-android.apk`,
              size: 456,
              browser_download_url:
                `https://github.com/yoligehude14753/echo-demo/releases/download/v${MOCK_UPDATE_VERSION}/EchoDesk-${MOCK_UPDATE_VERSION}-android.apk`,
            },
            {
              name: `EchoDesk-${MOCK_UPDATE_VERSION}-smart-tv.apk`,
              size: 789,
              browser_download_url:
                `https://github.com/yoligehude14753/echo-demo/releases/download/v${MOCK_UPDATE_VERSION}/EchoDesk-${MOCK_UPDATE_VERSION}-smart-tv.apk`,
            },
          ],
        }),
      });
    },
  );
  await installEchoMock(page, { isElectron: false });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const update = await page.evaluate(async () => {
    const { checkAppUpdate } = await import("/src/runtime.ts");
    return checkAppUpdate();
  });
  expect(update).toMatchObject({
    status: "checked",
    latestVersion: MOCK_UPDATE_VERSION,
    updateAvailable: false,
    assetName: null,
    assetUrl: null,
  });

  await expect(page.getByTestId("app-update-button")).toHaveCount(0);
  await page.getByTestId("open-settings").click();
  await page.getByTestId("check-updates").click();
  await expect(page.getByTestId("update-status-tag")).toHaveText("暂无适用安装包");
  await expect(page.getByTestId("update-asset-name")).toHaveCount(0);
  await expect(page.getByTestId("install-update")).toBeDisabled();
  await expect(page.getByTestId("install-update")).toHaveText("暂无适用安装包");
});

test("TV 模式检查更新优先展示 smart-tv APK", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window.navigator, "userAgent", {
      value: "Mozilla/5.0 (Linux; Android 11; SmartTV) AppleWebKit/537.36",
      configurable: true,
    });
    window.localStorage.setItem("echodesk.forceTvUi", "1");
    (window as unknown as { Capacitor?: { isNativePlatform: () => boolean } }).Capacitor = {
      isNativePlatform: () => true,
    };
  });
  await page.route(
    "https://api.github.com/repos/yoligehude14753/echo-demo/releases/latest",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          tag_name: `v${MOCK_UPDATE_VERSION}`,
          name: `EchoDesk v${MOCK_UPDATE_VERSION}`,
          html_url: `https://github.com/yoligehude14753/echo-demo/releases/tag/v${MOCK_UPDATE_VERSION}`,
          assets: [
            {
              name: `EchoDesk.Setup.${MOCK_UPDATE_VERSION}.exe`,
              size: 123,
              browser_download_url:
                `https://github.com/yoligehude14753/echo-demo/releases/download/v${MOCK_UPDATE_VERSION}/EchoDesk.Setup.${MOCK_UPDATE_VERSION}.exe`,
            },
            {
              name: `EchoDesk-${MOCK_UPDATE_VERSION}-smart-tv.apk`,
              size: 456,
              browser_download_url:
                `https://github.com/yoligehude14753/echo-demo/releases/download/v${MOCK_UPDATE_VERSION}/EchoDesk-${MOCK_UPDATE_VERSION}-smart-tv.apk`,
            },
            {
              name: `EchoDesk-${MOCK_UPDATE_VERSION}-android.apk`,
              size: 789,
              browser_download_url:
                `https://github.com/yoligehude14753/echo-demo/releases/download/v${MOCK_UPDATE_VERSION}/EchoDesk-${MOCK_UPDATE_VERSION}-android.apk`,
            },
          ],
        }),
      });
    },
  );
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.getByTestId("open-settings").click();
  await page.getByTestId("check-updates").click();

  await expect(page.getByTestId("update-status-tag")).toContainText("发现新版本");
  await expect(page.getByTestId("update-asset-name")).toContainText(
    `EchoDesk-${MOCK_UPDATE_VERSION}-smart-tv.apk`,
  );
});

test("Android 横屏非 TV 包检查更新仍优先展示 android APK", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.addInitScript(() => {
    Object.defineProperty(window.navigator, "userAgent", {
      value: "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36",
      configurable: true,
    });
    (window as unknown as { Capacitor?: { isNativePlatform: () => boolean } }).Capacitor = {
      isNativePlatform: () => true,
    };
  });
  await page.route(
    "https://api.github.com/repos/yoligehude14753/echo-demo/releases/latest",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          tag_name: `v${MOCK_UPDATE_VERSION}`,
          name: `EchoDesk v${MOCK_UPDATE_VERSION}`,
          html_url: `https://github.com/yoligehude14753/echo-demo/releases/tag/v${MOCK_UPDATE_VERSION}`,
          assets: [
            {
              name: `EchoDesk-${MOCK_UPDATE_VERSION}-smart-tv.apk`,
              size: 456,
              browser_download_url:
                `https://github.com/yoligehude14753/echo-demo/releases/download/v${MOCK_UPDATE_VERSION}/EchoDesk-${MOCK_UPDATE_VERSION}-smart-tv.apk`,
            },
            {
              name: `EchoDesk-${MOCK_UPDATE_VERSION}-android.apk`,
              size: 789,
              browser_download_url:
                `https://github.com/yoligehude14753/echo-demo/releases/download/v${MOCK_UPDATE_VERSION}/EchoDesk-${MOCK_UPDATE_VERSION}-android.apk`,
            },
          ],
        }),
      });
    },
  );
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.getByTestId("open-settings").click();
  await page.getByTestId("check-updates").click();

  await expect(page.getByTestId("update-status-tag")).toContainText("发现新版本");
  await expect(page.getByTestId("update-asset-name")).toContainText(
    `EchoDesk-${MOCK_UPDATE_VERSION}-android.apk`,
  );
});

test("本机版本高于公开版本时不会提供降级下载或安装", async ({ page }) => {
  await page.addInitScript(() => {
    const state = window as unknown as Window & {
      __installUpdateCalls?: number;
      __openExternalCalls?: number;
      echo?: Record<string, unknown>;
    };
    state.__installUpdateCalls = 0;
    state.__openExternalCalls = 0;
    state.echo = {
      ...(state.echo ?? {}),
      isElectron: true,
      getUpdateStatus: async () => ({
        status: "available",
        currentVersion: "0.2.0",
        latestVersion: "0.2.50",
        // 即使旧主进程上报的 currentVersion 落后、并错误标记 available，
        // 目标版本低于当前 0.3.1 前端构建时仍必须阻止降级。
        updateAvailable: true,
        canAutoInstall: true,
        assetName: "EchoDesk-0.2.50.dmg",
        assetUrl: "https://example.invalid/EchoDesk-0.2.50.dmg",
        releaseUrl: "https://example.invalid/releases/0.2.50",
      }),
      installUpdate: async () => {
        state.__installUpdateCalls = (state.__installUpdateCalls ?? 0) + 1;
        return { ok: true };
      },
      openExternal: async () => {
        state.__openExternalCalls = (state.__openExternalCalls ?? 0) + 1;
        return { ok: true };
      },
    };
  });
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect(page.getByTestId("app-update-button")).toHaveCount(0);
  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("update-status-tag")).toHaveText("本机版本较新");
  await expect(page.getByTestId("update-version-note")).toContainText(
    "为避免降级，下载与安装已停用",
  );
  await expect(page.getByTestId("install-update")).toHaveText("无需更新");
  await expect(page.getByTestId("install-update")).toBeDisabled();
  await expect(page.getByTestId("update-asset-name")).toBeHidden();
  await page.getByTestId("install-update").evaluate((button) => {
    (button as HTMLButtonElement).click();
  });
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as Window & { __installUpdateCalls?: number })
            .__installUpdateCalls ?? 0,
      ),
    )
    .toBe(0);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as Window & { __openExternalCalls?: number })
            .__openExternalCalls ?? 0,
      ),
    )
    .toBe(0);
});

test("桌面端发现新版本后会在顶栏显示更新入口并可点击安装", async ({ page }) => {
  await page.addInitScript(() => {
    type UpdateStatus = {
      status: string;
      currentVersion: string;
      latestVersion?: string;
      updateAvailable?: boolean;
      releaseUrl?: string;
      canAutoInstall?: boolean;
    };
    const listeners: Array<(status: UpdateStatus) => void> = [];
    const state = window as unknown as Window & {
      __emitUpdateStatus?: (status: UpdateStatus) => void;
      __installUpdateCalls?: number;
      echo?: Record<string, unknown>;
    };
    state.__installUpdateCalls = 0;
    state.__emitUpdateStatus = (status: UpdateStatus) => {
      for (const listener of listeners) listener(status);
    };
    state.echo = {
      ...(state.echo ?? {}),
      isElectron: true,
      isPublicDemo: true,
      getUpdateStatus: async () => ({
        status: "idle",
        currentVersion: "0.2.50",
        releaseUrl: "https://github.com/yoligehude14753/echo-demo/releases/latest",
      }),
      onUpdateStatus: (cb: (status: UpdateStatus) => void) => {
        listeners.push(cb);
        return () => {
          const index = listeners.indexOf(cb);
          if (index >= 0) listeners.splice(index, 1);
        };
      },
      installUpdate: async () => {
        state.__installUpdateCalls = (state.__installUpdateCalls ?? 0) + 1;
        return { ok: true };
      },
      openExternal: async () => ({ ok: true }),
    };
  });
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("app-update-button")).toBeHidden();

  await page.evaluate((version) => {
    const state = window as unknown as Window & {
      __emitUpdateStatus?: (status: Record<string, unknown>) => void;
    };
    state.__emitUpdateStatus?.({
      status: "available",
      currentVersion: "0.2.50",
      latestVersion: version,
      updateAvailable: true,
      canAutoInstall: true,
      releaseUrl: `https://github.com/yoligehude14753/echo-demo/releases/tag/v${version}`,
    });
  }, MOCK_UPDATE_VERSION);

  await expect(page.getByTestId("app-update-button")).toBeVisible();
  await expect(page.getByTestId("app-update-button")).toContainText("更新");
  await page.getByTestId("app-update-button").click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as Window & { __installUpdateCalls?: number })
            .__installUpdateCalls ?? 0,
      ),
    )
    .toBe(1);
});
