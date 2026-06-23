import { expect, test } from "@playwright/test";
import {
  installEchoMock,
  publishArtifactReady,
  publishMeetingStarted,
  publishMinutesReady,
} from "./_mock";

test("TV 会后扫码保存：二维码、分享链接和删除输出路径可用", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  const mock = await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const meetingId = "tv-meeting-share-001";
  await publishMeetingStarted(mock, meetingId, 1);
  await publishMinutesReady(mock, meetingId, 2);
  const artifactId = await publishArtifactReady(
    mock,
    "pdf",
    3,
    "tv-share-output-001",
    "电视会议输出",
    "/tmp/tv-share-output-001.pdf",
    meetingId,
  );

  await expect(page.getByTestId("minutes-title")).toContainText("测试纪要");
  await expect(page.locator(`[data-artifact-id="${artifactId}"]`)).toBeVisible();

  await page.getByTestId("open-meeting-share").click();
  await expect(page.getByTestId("meeting-share-modal")).toBeVisible();
  await expect(page.getByTestId("meeting-share-qr")).toBeVisible({ timeout: 8_000 });
  await expect(page.getByTestId("meeting-share-url")).toContainText(
    `/api/meetings/${meetingId}/share`,
  );
  await expect(page.getByTestId("meeting-share-url")).toContainText(artifactId);

  await page.getByTestId("clear-meeting-outputs-btn").click();
  await page.locator(".ant-modal-confirm .ant-btn-dangerous").click();

  await expect(page.getByTestId("meeting-share-modal")).toBeHidden();
  await expect(page.locator(`[data-artifact-id="${artifactId}"]`)).toBeHidden();
  await expect(page.getByText("纪要尚未生成")).toBeVisible();
  await expect
    .poll(
      async () => {
        const log = await mock.fetchLog();
        return log.some(
          (r) =>
            r.method === "DELETE" &&
            r.url.includes(`/meetings/${meetingId}/outputs`) &&
            r.bodyText?.includes(artifactId),
        );
      },
      { timeout: 5_000 },
    )
    .toBe(true);
});

test("会议待办执行：生成产物请求携带 meeting_id 和 todo_id", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 820 });

  await page.route(/\/intent\/route$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "generate_pdf",
        confidence: 0.9,
        params: { artifact_type: "pdf", brief: "生成会后 PDF" },
      }),
    });
  });

  const mock = await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const meetingId = "tv-todo-meta-001";
  const todoId = "todo-pdf-001";
  await publishMeetingStarted(mock, meetingId, 1);
  await mock.publish({
    type: "minutes.ready",
    seq: 2,
    ts: new Date().toISOString(),
    meeting_id: meetingId,
    payload: {
      meeting_id: meetingId,
      title: "待办执行测试",
      duration_sec: 90,
      speakers: ["说话人1"],
      summary: "测试待办执行时能把会议上下文带给后端。",
      sections: [{ heading: "安排", bullets: ["生成会后 PDF"] }],
      decisions: [],
      todos: [
        {
          id: todoId,
          text: "生成会后 PDF",
          assignee: "说话人1",
          kind: "actionable",
          status: "pending",
          done_at: null,
          artifact_id: null,
          suggested_command: "@生成 PDF 会后资料",
        },
      ],
      action_items: [],
      created_at: new Date().toISOString(),
    },
  });

  await page.getByTestId("minutes-todo-execute-btn").click();
  await expect(page.getByTestId("command-textarea")).toHaveValue("@生成 PDF 会后资料");
  await page.getByTestId("command-send-btn").click();

  await expect
    .poll(
      async () => {
        const log = await mock.fetchLog();
        const req = log.find(
          (r) => r.method === "POST" && r.url.includes("/artifacts/generate"),
        );
        return req?.bodyText ?? "";
      },
      { timeout: 5_000 },
    )
    .toContain(`"meeting_id":"${meetingId}"`);
  const log = await mock.fetchLog();
  const req = log.find((r) => r.method === "POST" && r.url.includes("/artifacts/generate"));
  expect(req?.bodyText).toContain(`"todo_id":"${todoId}"`);
});
