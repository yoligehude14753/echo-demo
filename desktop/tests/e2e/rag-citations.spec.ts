import { test, expect } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("RAG 答案把裸 doc token 渲染为角标引用", async ({ page }) => {
  await page.route(/\/intent\/route$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "search_rag",
        confidence: 0.95,
        params: { question: "HY100 是什么" },
        rationale: "test",
      }),
    });
  });
  await page.route(/\/capture\/recent$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });
  await page.route(/\/agent\/run$/, async (route) => {
    const citation = {
      kind: "rag",
      doc_id: "pdf-aa9c2de77e3e",
      chunk_id: "pdf-aa9c2de77e3e-p013-c0000",
      doc_title: "褐蚁产品手册",
      title: "褐蚁产品手册",
      page: "13",
      source: "upload",
      score: 12.34,
      text: "HY100 是褐蚁硬件产品线的核心型号，用于会议记录场景。",
      snippet: "HY100 是褐蚁硬件产品线的核心型号。",
    };
    const body = [
      `event: tool_call`,
      `data: ${JSON.stringify({ name: "rag_search", args: { query: "HY100 是什么" }, reason: "先查本地知识库", step: 0 })}`,
      "",
      `event: tool_result`,
      `data: ${JSON.stringify({ name: "rag_search", ok: true, summary: "检索 1 chunk", step: 0 })}`,
      "",
      `event: delta`,
      `data: ${JSON.stringify({ text: "HY100 [doc:pdf-aa9c2de77e3e-p013-c0000 p13] 是核心型号。" })}`,
      "",
      `event: final`,
      `data: ${JSON.stringify({ answer: "HY100 [doc:pdf-aa9c2de77e3e-p013-c0000 p13] 是核心型号。", artifact_ids: [], citations: [citation] })}`,
      "",
      "event: done",
      "data: {}",
      "",
    ].join("\n");
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream; charset=utf-8",
      body,
    });
  });

  await installEchoMock(page, {
    skipPaths: ["/intent/route", "/capture/recent", "/agent/run"],
  });
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  const textarea = page.getByTestId("command-textarea");
  await textarea.fill("@查 HY100 是什么");
  await textarea.press("Enter");

  const bubble = page.getByTestId("conv-bubble-rag_answer");
  await expect(bubble).toContainText("HY100");
  await expect(bubble).toContainText("1");
  await expect(bubble).not.toContainText("pdf-aa9c2de77e3e");
  await expect(bubble).not.toContainText("[doc:");
  await expect(page.getByTestId("citation-badge-1")).toBeVisible();

  const listItem = page.getByTestId("citation-list-item-1");
  await expect(listItem).toContainText("1 褐蚁产品手册 · p13");
  await listItem.click();

  await expect(page.locator(".ant-popover").filter({ hasText: "褐蚁产品手册 · p13" })).toBeVisible();
  await expect(page.locator(".ant-popover").filter({ hasText: "HY100 是褐蚁硬件产品线" })).toBeVisible();
  await expect(page.locator(".ant-popover").filter({ hasText: "score：12.34" })).toBeVisible();
});
