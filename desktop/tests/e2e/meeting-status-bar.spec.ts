/**
 * E2E：MeetingStatusBar 区分 auto / manual 文案与样式
 * （2026-05 phase4-meeting-deadlock 修复）。
 *
 * 验收点：
 *  - idle：「待机」文案；按钮样式不带 rose / amber 色
 *  - in_meeting + started_by=manual：「会议中」+ mm:ss 计时（含 ":" 字符）
 *  - in_meeting + started_by=auto：「自动记录中」+ Mic 图标，不显示计时（不含 ":"）
 *    — 这是核心回归点：旧 UI 会显示「会议中 562:53」给用户假象
 */
import { test, expect } from "@playwright/test";
import { installEchoMock } from "./_mock";

type Snap = {
  mode: "idle" | "in_meeting";
  meeting_id: string | null;
  started_at: string | null;
  started_by: "auto" | "manual" | null;
};

async function mockCurrentMeeting(
  page: import("@playwright/test").Page,
  snap: Snap,
): Promise<void> {
  // /api/meetings/current → 通过 Vite proxy 落到 /meetings/current；
  // 这里两条都覆盖，避免 base URL 切换时漏掉
  await page.route(/\/meetings\/current$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(snap),
    });
  });
}

test("MeetingStatusBar · idle 显示「待机」", async ({ page }) => {
  await mockCurrentMeeting(page, {
    mode: "idle",
    meeting_id: null,
    started_at: null,
    started_by: null,
  });
  await installEchoMock(page, { skipPaths: ["/meetings/current"] });
  await page.goto("/");

  const bar = page.getByTestId("meeting-status-bar");
  await expect(bar).toBeVisible({ timeout: 5_000 });
  await expect(bar).toContainText("待机");
});

test("MeetingStatusBar · manual 显示「会议中 mm:ss」(含计时 ':')", async ({ page }) => {
  const startedAt = new Date(Date.now() - 90_000).toISOString(); // 90s 前
  await mockCurrentMeeting(page, {
    mode: "in_meeting",
    meeting_id: "m-test123",
    started_at: startedAt,
    started_by: "manual",
  });
  await installEchoMock(page, { skipPaths: ["/meetings/current"] });
  await page.goto("/");

  const bar = page.getByTestId("meeting-status-bar");
  await expect(bar).toBeVisible({ timeout: 5_000 });
  await expect(bar).toContainText("会议中");

  // manual 必须显示 mm:ss 计时（含 ":"）
  const text = (await bar.textContent()) ?? "";
  expect(text).toContain(":");
  // 不应误显示 auto 文案
  expect(text).not.toContain("自动记录中");
});

test("MeetingStatusBar · auto 显示「自动记录中」(不含计时 ':')", async ({ page }) => {
  // 模拟 auto-meeting 已开始 6 小时（旧 UI 会显 360:00 这种假象）
  const startedAt = new Date(Date.now() - 6 * 3600_000).toISOString();
  await mockCurrentMeeting(page, {
    mode: "in_meeting",
    meeting_id: "auto-1716800000",
    started_at: startedAt,
    started_by: "auto",
  });
  await installEchoMock(page, { skipPaths: ["/meetings/current"] });
  await page.goto("/");

  const bar = page.getByTestId("meeting-status-bar");
  await expect(bar).toBeVisible({ timeout: 5_000 });
  await expect(bar).toContainText("自动记录中");

  // 核心回归断言：auto 状态下顶栏不显示 mm:ss 计时（不含 ":"）
  const text = (await bar.textContent()) ?? "";
  expect(text).not.toContain(":");
  // 不应同时显示「会议中」
  expect(text).not.toContain("会议中");
});

test("MeetingStatusBar · 点击区域铺满顶栏高度", async ({ page }) => {
  await mockCurrentMeeting(page, {
    mode: "idle",
    meeting_id: null,
    started_at: null,
    started_by: null,
  });
  await installEchoMock(page, { skipPaths: ["/meetings/current"] });
  await page.goto("/");

  const box = await page.getByTestId("meeting-status-bar").boundingBox();
  expect(box?.height).toBeGreaterThanOrEqual(44);
  expect(box?.width).toBeGreaterThanOrEqual(112);
});
