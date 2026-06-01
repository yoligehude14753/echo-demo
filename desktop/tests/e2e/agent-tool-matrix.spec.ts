import { expect, test, type Page } from "@playwright/test";
import { installEchoMock } from "./_mock";

function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

async function mockIntent(page: Page, kind = "chat"): Promise<void> {
  await page.route(/\/intent\/route$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind,
        confidence: 0.95,
        params: { question: "综合分析 HY100 并生成报告" },
        rationale: "test",
      }),
    });
  });
}

test("agent 工具矩阵：RAG + Web + 产物都能在对话中落地", async ({ page }) => {
  await mockIntent(page);
  await page.route(/\/capture\/recent(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          text: "刚才讨论了 HY100 的会议转写场景",
          captured_at: "2026-05-29T00:00:00Z",
          speaker_label: "用户",
          speaker_id: "spk-user",
        },
      ]),
    });
  });
  await page.route(/\/agent\/run$/, async (route) => {
    const req = route.request().postDataJSON() as { inline_context?: string };
    expect(req.inline_context).toContain("HY100 的会议转写场景");
    const artifact = {
      artifact_id: "matrix-html-001",
      artifact_type: "html",
      title: "HY100 综合分析报告",
      file_path: "/tmp/matrix-html-001/index.html",
      mime_type: "text/html",
      size_bytes: 12345,
      generation_latency_ms: 1234,
      model: "test",
      metadata: {},
    };
    const ragCitation = {
      kind: "rag",
      doc_id: "doc-hy100",
      chunk_id: "doc-hy100-c1",
      doc_title: "HY100 产品手册",
      title: "HY100 产品手册",
      page: "7",
      source: "upload",
      score: 9.8,
      text: "HY100 支持会议记录和总结。",
    };
    const webCitation = {
      kind: "web",
      title: "HY100 新闻稿",
      url: "https://example.com/hy100",
      source: "web",
      snippet: "HY100 面向企业会议协作。",
    };
    const body = [
      sseFrame("plan", { step: 1, max_steps: 4 }),
      sseFrame("tool_call", { name: "rag_search", reason: "查本地资料", step: 1 }),
      sseFrame("tool_result", { name: "rag_search", ok: true, summary: "检索 1 chunk", step: 1 }),
      sseFrame("tool_call", { name: "web_search", reason: "补充公开信息", step: 2 }),
      sseFrame("tool_result", { name: "web_search", ok: true, summary: "联网 1 条", step: 2 }),
      sseFrame("tool_call", { name: "generate_artifact", reason: "生成 HTML 报告", step: 3 }),
      sseFrame("artifact", artifact),
      sseFrame("tool_result", { name: "generate_artifact", ok: true, summary: "已生成 html 产物", step: 3 }),
      sseFrame("final", {
        answer: "HY100 适合会议记录与企业协作，已生成综合分析报告。",
        artifact_ids: [artifact.artifact_id],
        citations: [ragCitation, webCitation],
      }),
      sseFrame("done", {}),
    ].join("");
    await route.fulfill({ status: 200, contentType: "text/event-stream; charset=utf-8", body });
  });

  await installEchoMock(page, {
    skipPaths: ["/intent/route", "/capture/recent", "/agent/run"],
  });
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await page.getByTestId("command-textarea").fill("@echo 综合分析 HY100 并生成报告");
  await page.getByTestId("command-textarea").press("Enter");

  const bubble = page.getByTestId("conv-bubble-rag_answer");
  await expect(bubble).toContainText("HY100 适合会议记录");
  await expect(page.getByTestId("citation-list-item-1")).toContainText("HY100 产品手册 · p7");
  await expect(page.getByTestId("citation-list-item-2")).toContainText("HY100 新闻稿");
  await expect(page.getByTestId("conversation-artifact-card")).toContainText("HY100 综合分析报告");
  await expect(page.getByTestId("artifact-card").filter({ hasText: "HY100 综合分析报告" })).toBeVisible();
});

test("agent 工具失败：SSE error 会落为失败气泡且保留已生成产物", async ({ page }) => {
  await mockIntent(page);
  await page.route(/\/capture\/recent(?:\?.*)?$/, async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
  });
  await page.route(/\/agent\/run$/, async (route) => {
    const artifact = {
      artifact_id: "partial-html-001",
      artifact_type: "html",
      title: "部分生成报告",
      file_path: "/tmp/partial-html-001/index.html",
      mime_type: "text/html",
      size_bytes: 4567,
      generation_latency_ms: 888,
      model: "test",
      metadata: {},
    };
    const body = [
      sseFrame("tool_call", { name: "generate_artifact", reason: "先生成草稿", step: 1 }),
      sseFrame("artifact", artifact),
      sseFrame("error", { error: "web_search timeout", stage: "web_search" }),
      sseFrame("done", {}),
    ].join("");
    await route.fulfill({ status: 200, contentType: "text/event-stream; charset=utf-8", body });
  });

  await installEchoMock(page, {
    skipPaths: ["/intent/route", "/capture/recent", "/agent/run"],
  });
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await page.getByTestId("command-textarea").fill("@echo 生成后联网补充");
  await page.getByTestId("command-textarea").press("Enter");

  const failed = page.getByTestId("conv-bubble-assistant_reply").filter({ hasText: "web_search timeout" });
  await expect(failed).toContainText("多工具执行失败（web_search）");
  await expect(page.getByTestId("conversation-artifact-card")).toContainText("部分生成报告");
  await expect(page.getByTestId("command-textarea")).not.toBeDisabled();
});

test("agent 无 final 兜底：只返回工具过程也不会卡住输入框", async ({ page }) => {
  await mockIntent(page);
  await page.route(/\/capture\/recent(?:\?.*)?$/, async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
  });
  await page.route(/\/agent\/run$/, async (route) => {
    const body = [
      sseFrame("tool_call", { name: "web_search", reason: "尝试联网", step: 1 }),
      sseFrame("tool_result", { name: "web_search", ok: false, summary: "没有可用结果", step: 1 }),
      sseFrame("done", {}),
    ].join("");
    await route.fulfill({ status: 200, contentType: "text/event-stream; charset=utf-8", body });
  });

  await installEchoMock(page, {
    skipPaths: ["/intent/route", "/capture/recent", "/agent/run"],
  });
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  const textarea = page.getByTestId("command-textarea");
  await textarea.fill("@echo 查一个不存在的问题");
  await textarea.press("Enter");

  await expect(
    page.getByTestId("conv-bubble-assistant_reply").filter({ hasText: "! 联网搜索：没有可用结果" }),
  ).toBeVisible({ timeout: 5_000 });
  await expect(textarea).not.toBeDisabled();
});
