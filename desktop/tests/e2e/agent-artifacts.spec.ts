import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("agent 生成产物同时进入对话气泡和全局 outputs", async ({ page }) => {
  await page.route(/\/intent\/route$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      // 用开放式问题（chat）→ 走 agent 多工具链（/agent/run），由 agent 自行决定
      // 调 generate_artifact。显式 @生成 已改为直连 skill 流，不再经 agent。
      body: JSON.stringify({
        kind: "chat",
        confidence: 0.95,
        params: { question: "研究教育场景大模型一体机招投标，然后生成 PPT" },
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
    const artifact = {
      artifact_id: "pptx-agent-001",
      artifact_type: "pptx",
      title: "教育场景大模型一体机调研",
      file_path: "/tmp/pptx-agent-001/output.pptx",
      mime_type: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      size_bytes: 45678,
      generation_latency_ms: 2345,
      model: "test",
      metadata: {},
    };
    const body = [
      "event: tool_call",
      `data: ${JSON.stringify({ name: "web_search", args: { query: "教育 大模型一体机 招投标" }, reason: "补充市场信息", step: 1 })}`,
      "",
      "event: tool_result",
      `data: ${JSON.stringify({ name: "web_search", ok: true, summary: "联网 3 条", step: 1 })}`,
      "",
      "event: tool_call",
      `data: ${JSON.stringify({ name: "generate_artifact", args: { artifact_type: "pptx" }, reason: "生成 PPT", step: 2 })}`,
      "",
      "event: artifact",
      `data: ${JSON.stringify(artifact)}`,
      "",
      "event: tool_result",
      `data: ${JSON.stringify({ name: "generate_artifact", ok: true, summary: "已生成 pptx 产物", step: 2 })}`,
      "",
      "event: final",
      `data: ${JSON.stringify({ answer: "已生成 PPT 产物。", artifact_ids: ["pptx-agent-001"], citations: [] })}`,
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
  await textarea.fill("@echo 研究教育场景大模型一体机招投标，然后生成 PPT");
  await textarea.press("Enter");

  const conversationArtifact = page.getByTestId("conversation-artifact-card");
  await expect(conversationArtifact).toContainText("教育场景大模型一体机调研");
  await expect(conversationArtifact).toHaveAttribute("data-artifact-id", "pptx-agent-001");

  const globalArtifact = page.getByTestId("artifact-card").filter({ hasText: "教育场景大模型一体机调研" });
  await expect(globalArtifact).toBeVisible();
});

test("失败产物卡片重试会重新调用流式产物生成", async ({ page }) => {
  const mock = await installEchoMock(page);
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await mock.publish({
    type: "artifact.generating",
    seq: 11,
    ts: new Date().toISOString(),
    meeting_id: null,
    payload: {
      artifact_type: "html",
      brief: "生成 HTML 竞品调研报告",
    },
  });
  await mock.publish({
    type: "artifact.failed",
    seq: 12,
    ts: new Date().toISOString(),
    meeting_id: null,
    payload: {
      artifact_type: "html",
      error: "mock failure",
    },
  });

  const failed = page.getByTestId("failed-artifact-card");
  await expect(failed).toContainText("生成 HTML 竞品调研报告");
  await failed.getByRole("button", { name: "重试" }).click();

  await expect
    .poll(
      async () => {
        const log = await mock.fetchLog();
        return log.find(
          (r) =>
            r.method === "POST" &&
            r.url.includes("/artifacts/generate/stream") &&
            r.bodyText?.includes("生成 HTML 竞品调研报告"),
        );
      },
      { timeout: 5_000 },
    )
    .toBeTruthy();

  await expect(page.getByTestId("artifact-card").filter({ hasText: "mock html 报告" })).toBeVisible();
  await expect(failed).toBeHidden();
});
