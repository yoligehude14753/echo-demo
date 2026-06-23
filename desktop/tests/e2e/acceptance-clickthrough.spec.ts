import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("验收点击流：知识库、设置、eight 状态、移动连接和输入框均可操作", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 820 });

  const docs = [
    {
      doc_id: "doc-acceptance-1",
      title: "EchoDesk 验收清单.md",
      kind: "md",
      source: "workspace",
      source_path: "/Users/test/work/EchoDesk 验收清单.md",
      n_chunks: 5,
    },
  ];
  let savedMobileBase = "";

  await page.route(/\/(api\/)?healthz\/full$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        backend: { ok: true, version: "0.2.4-mock", port: 8769, uptime_s: 12.3 },
        db: { ok: true },
        remote: {
          heyi_stt_firered: { ok: true, latency_ms: 18, checked_at: Date.now() / 1000 },
          heyi_tts_qwen3: { ok: true, latency_ms: 22, checked_at: Date.now() / 1000 },
          heyi_llm_fast: { ok: true, latency_ms: 35, checked_at: Date.now() / 1000 },
          yunwu_llm_main: { ok: true, latency_ms: 60, checked_at: Date.now() / 1000 },
          tavily: { ok: null, reason: "no_api_key", checked_at: Date.now() / 1000 },
        },
        mic: { ok: "unknown" },
      }),
    });
  });

  await page.route(/\/(api\/)?admin\/settings\/remote$/, async (route) => {
    const req = route.request();
    if (req.method() === "PATCH") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "ok", restart_required: true, updated: 1 }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        config_path: "/Users/test/.echodesk/config.json",
        fields: [
          { key: "llm_main_base_url", value: "https://yunwu.ai/v1", sensitive: false, source: "default" },
          { key: "yunwu_open_key", value: "", sensitive: true, source: "default" },
          {
            key: "llm_fast_base_url",
            value: "http://100.76.3.59:7860/v1",
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
    });
  });

  await page.route(/\/(api\/)?admin\/data-dir$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
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
    });
  });

  await page.route(/\/(api\/)?workspace\/status$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        configured_dirs: ["/Users/test/work"],
        authorized_dirs: ["/Users/test/work"],
        n_indexed: 1,
        max_file_mb: 100,
        scan_on_startup: true,
      }),
    });
  });

  await page.route(/\/(api\/)?rag\/docs(\/[^?]+)?$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ total: docs.length, by_source: { workspace: docs }, docs }),
    });
  });

  await installEchoMock(page, {
    skipPaths: [
      "/healthz/full",
      "/admin/settings/remote",
      "/admin/data-dir",
      "/workspace/status",
      "/rag/docs",
    ],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect(page.getByTestId("open-about")).toContainText("v0.2.4");
  await page.getByTestId("pill-heyi").click();
  await expect(page.getByText("eight 远端服务")).toBeVisible();
  await page.keyboard.press("Escape");

  await page.getByTestId("workspace-dirs-tag").click();
  await expect(page.getByText("知识库 / 工作区文件")).toBeVisible();
  await expect(
    page.getByTestId("knowledge-doc-row").getByText("EchoDesk 验收清单.md", { exact: true }),
  ).toBeVisible();

  await page.getByTestId("workspace-open-settings").click();
  await expect(page.getByTestId("remote-settings-form")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("激活码");

  await expect(page.locator('input[value="http://100.76.3.59:8090"]')).toBeVisible();
  await expect(page.locator('input[value="http://100.76.3.59:8094"]')).toBeVisible();
  await expect(page.locator('input[value="http://100.76.3.59:7860/v1"]')).toBeVisible();

  const mobileBase = page.getByTestId("mobile-backend-base");
  await mobileBase.fill("http://10.0.2.2:8769");
  await page.getByTestId("save-mobile-backend-base").click();
  savedMobileBase = await page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase") ?? "");
  expect(savedMobileBase).toBe("http://10.0.2.2:8769");

  await page.keyboard.press("Escape");
  await page.getByTestId("command-textarea").fill("帮我总结当前知识库");
  await expect(page.getByTestId("command-textarea")).toHaveValue("帮我总结当前知识库");
});
