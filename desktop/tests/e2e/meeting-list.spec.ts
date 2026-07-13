/**
 * E2E #1：会议列表点击切换。
 *
 * 流程：
 * - mock WS 推 2 个 meeting.started 事件
 * - MeetingList 显示 2 项
 * - 点击切换 → currentMeetingId 切换 → 选中态更新
 * - 内部 meeting_id 不作为用户可见标题
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
  const items = page.getByTestId("meeting-item");
  await expect(items).toHaveCount(2, { timeout: 5_000 });

  const meetingA = page.locator('[data-testid="meeting-item"][data-meeting-id="meeting-A"]');
  const meetingB = page.locator('[data-testid="meeting-item"][data-meeting-id="meeting-B"]');

  // 4. 当前默认是最后启动的 meeting-B，但页面不显示内部 ID。
  await expect(meetingB).toHaveAttribute("aria-current", "page", {
    timeout: 5_000,
  });
  await expect(page.getByTestId("meeting-item-title").filter({ hasText: /meeting-/ })).toHaveCount(0);

  // 5. 点击 meeting-A，可访问选中态随之切换。
  await meetingA.click();
  await expect(meetingA).toHaveAttribute("aria-current", "page");
  await expect(meetingB).not.toHaveAttribute("aria-current", "page");
});
