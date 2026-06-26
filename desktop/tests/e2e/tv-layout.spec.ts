import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("电视视口：横屏布局和遥控器确认键路径可用", async ({ page }) => {
  await page.setViewportSize({ width: 960, height: 540 });
  await page.addInitScript(() => {
    window.localStorage.setItem("echodesk.forceTvUi", "1");
    window.localStorage.setItem(
      "echodesk.localCaptureState.v1",
      JSON.stringify({
        schema: 1,
        appVersion: "0.2.19",
        savedAt: "2026-06-01T00:00:00.000Z",
        currentMeetingId: "old-local-meeting",
        meetings: [
          {
            meeting_id: "old-local-meeting",
            title: "不该继承的旧会议",
            state: "ended",
            segments: [],
            speakers: [],
            artifacts: [],
          },
        ],
        ambientSegments: [
          {
            text: "不该继承的旧转写",
            captured_at: "2026-06-01T00:00:00.000Z",
            speaker_id: null,
            speaker_label: null,
            duration_ms: 1000,
          },
        ],
        artifacts: [],
      }),
    );
    (window as unknown as { Capacitor?: { isNativePlatform: () => boolean } }).Capacitor = {
      isNativePlatform: () => true,
    };
  });

  await page.route(/\/(api\/)?admin\/settings\/remote$/, async (route) => {
    if (route.request().method() === "PATCH") {
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
          { key: "llm_fast_base_url", value: "http://100.76.3.59:7860/v1", sensitive: false, source: "default" },
          { key: "stt_firered_url", value: "http://100.76.3.59:8090", sensitive: false, source: "default" },
          { key: "tts_qwen3_url", value: "http://100.76.3.59:8094", sensitive: false, source: "default" },
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
        breakdown: { db: 1024, storage: 0, rag_index: 2048, logs: 1024, skill_build: 0 },
      }),
    });
  });

  const mock = await installEchoMock(page, {
    skipPaths: ["/admin/settings/remote", "/admin/data-dir"],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect(page.getByText("转写流")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("会议纪要", { exact: true })).toBeVisible();
  await expect(page.getByTestId("command-bar")).toBeVisible();
  await expect(page.getByTestId("pill-backend")).toContainText("backend 外部", {
    timeout: 10_000,
  });
  await expect(page.getByText("不该继承的旧会议")).toHaveCount(0);
  await expect(page.getByText("不该继承的旧转写")).toHaveCount(0);
  await expect(page.getByText("EchoDesk 启动失败")).toHaveCount(0);
  await expect(page.getByTestId("tv-quick-commands")).toBeVisible();

  const boxes = await page.evaluate(() => {
    const pick = (selector: string) => {
      const el = document.querySelector(selector);
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    };
    const textarea = document.querySelector(
      "textarea[data-testid='command-textarea'], [data-testid='command-textarea'] textarea, [data-testid='command-textarea']",
    );
    return {
      shell: pick(".echodesk-shell"),
      sider: pick(".echodesk-meeting-sider"),
      transcript: pick(".echodesk-transcript-pane"),
      output: pick(".echodesk-output-pane"),
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      commandTextarea: textarea ? textarea.getBoundingClientRect().height : 0,
      header: pick(".app-header"),
      workspace: pick("[data-testid='workspace-bar']"),
      brandSize: Number.parseFloat(getComputedStyle(document.querySelector(".brand")!).fontSize),
      bubbleFontSize: textarea ? Number.parseFloat(getComputedStyle(textarea).fontSize) : 0,
    };
  });

  expect(boxes.shell?.width).toBe(960);
  expect(boxes.documentWidth).toBeLessThanOrEqual(boxes.viewportWidth + 1);
  expect(boxes.header?.height).toBeGreaterThanOrEqual(49);
  expect(boxes.header?.height).toBeLessThanOrEqual(51);
  expect(boxes.workspace?.height).toBeGreaterThanOrEqual(45);
  expect(boxes.workspace?.height).toBeLessThanOrEqual(47);
  expect(boxes.sider?.width ?? 0).toBe(0);
  const expectedOutputWidth = boxes.viewportWidth * 0.26;
  expect(boxes.transcript?.width).toBeGreaterThanOrEqual(700);
  expect(boxes.output?.width).toBeGreaterThanOrEqual(expectedOutputWidth - 2);
  expect(boxes.output?.width).toBeLessThanOrEqual(expectedOutputWidth + 2);
  expect(boxes.commandTextarea).toBeGreaterThanOrEqual(52);
  expect(boxes.brandSize).toBeGreaterThanOrEqual(19);
  expect(boxes.bubbleFontSize).toBeGreaterThanOrEqual(18);

  const fetchLog = await mock.fetchLog();
  expect(fetchLog.some((r) => /\/(api\/)?meetings\?/.test(r.url))).toBe(false);
  expect(fetchLog.some((r) => /\/(api\/)?capture\/recent/.test(r.url))).toBe(false);

  const workspaceTag = page.getByTestId("workspace-dirs-tag");
  await workspaceTag.focus();
  await expect(workspaceTag).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByText("知识库 / 工作区文件")).toBeVisible();
  await page.locator(".ant-modal-close").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("知识库 / 工作区文件")).toBeHidden();

  const settingsButton = page.getByTestId("open-settings");
  await settingsButton.focus();
  await expect(settingsButton).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("mobile-backend-base")).toBeVisible();

  await page.keyboard.press("Escape");
  if (await page.getByTestId("mobile-backend-base").isVisible()) {
    await page.locator(".ant-modal-close").last().click();
  }
  await expect(page.getByTestId("mobile-backend-base")).toBeHidden();

  await page.evaluate(() => {
    (
      window as unknown as {
        __echoIntentRouteMock?: { kind: string; confidence: number };
      }
    ).__echoIntentRouteMock = { kind: "chat", confidence: 0.92 };
  });
  await page.getByRole("button", { name: "@查 当前会议要点" }).click();
  await expect(page.getByTestId("user-message")).toContainText("@查 当前会议要点");
  await expect(page.getByTestId("assistant-message")).toContainText(
    "Echo 已收到，这是 TV 问答文本回复。",
  );
});
