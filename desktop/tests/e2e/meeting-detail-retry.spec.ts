import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

const MEETING_ID = "meeting-detail-retry";

test("会议详情首次失败后保持可重试，再次点击成功恢复", async ({ page }) => {
  let transcriptRequests = 0;
  await page.route(/\/(api\/)?meetings\/current$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ mode: "idle", meeting_id: null }),
    }),
  );
  await page.route(/\/(api\/)?meetings(\?|$)/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          meeting_id: MEETING_ID,
          title: "可重试会议",
          state: "finalized",
          started_at: "2026-07-11T01:00:00Z",
          ended_at: "2026-07-11T01:10:00Z",
          finalized_at: "2026-07-11T01:10:05Z",
          n_segments: 1,
          n_speakers: 1,
          has_minutes: false,
        },
      ]),
    });
  });
  await page.route(
    new RegExp(`/(api/)?meetings/${MEETING_ID}/transcript$`),
    async (route) => {
      transcriptRequests += 1;
      if (transcriptRequests === 1) {
        await route.fulfill({ status: 500, body: "temporary failure" });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            text: "重试后恢复的转写",
            start_ms: 0,
            end_ms: 1000,
            speaker_id: "speaker-1",
            speaker_label: "说话人1",
          },
        ]),
      });
    },
  );
  await page.route(/\/(api\/)?meetings\/[^/]+\/minutes$/, (route) =>
    route.fulfill({ status: 404, body: "not generated" }),
  );
  await page.route(/\/(api\/)?meetings\/[^/]+\/artifacts$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route(/\/(api\/)?workflows\/runs/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );

  await installEchoMock(page, {
    skipPaths: [
      "/meetings?",
      `/meetings/${MEETING_ID}`,
      "/workflows/runs",
    ],
  });
  await page.goto("/");

  const item = page.locator(`[data-meeting-id="${MEETING_ID}"]`);
  await expect(item).toBeVisible();
  await item.click();
  await expect(item).toContainText("加载失败 · 点击重试");

  await item.click();
  await expect(page.getByText("重试后恢复的转写")).toBeVisible();
  expect(transcriptRequests).toBe(2);
});
