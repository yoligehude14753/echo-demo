/**
 * E2E #1：会议列表点击切换。
 *
 * 流程：
 * - mock WS 推 2 个 meeting.started 事件
 * - MeetingList 显示 2 项
 * - 点击切换 → currentMeetingId 切换 → header 显示 meeting_id
 */
import { test, expect } from "@playwright/test";
import { installEchoMock, publishMeetingStarted } from "./_mock";

test("点击会议列表切换当前会议", async ({ page }) => {
  const mock = await installEchoMock(page);
  await page.goto("/");

  // 1. 连接成功
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  // 2. 推 2 个 meeting.started 事件
  await publishMeetingStarted(mock, "meeting-A", 1);
  await publishMeetingStarted(mock, "meeting-B", 2);

  // 3. MeetingList 显示 2 项
  const items = page.locator("aside button").filter({ hasText: /meeting-/ });
  await expect(items).toHaveCount(2, { timeout: 5_000 });

  // 4. 当前默认是最后启动的（meeting-B），转写流 header 区显示 meeting-B
  const transcriptHeader = page.locator("text=转写流").locator("..");
  await expect(transcriptHeader.getByText("meeting-B")).toBeVisible({
    timeout: 5_000,
  });

  // 5. 点击 meeting-A，转写流 header 切换为 meeting-A
  await items.filter({ hasText: "meeting-A" }).click();
  await expect(transcriptHeader.getByText("meeting-A")).toBeVisible({
    timeout: 3_000,
  });
});
