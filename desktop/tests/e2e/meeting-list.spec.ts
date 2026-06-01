/**
 * E2E #1：会议列表点击切换。
 *
 * 流程：
 * - 通过 GET /meetings 注入 2 个历史会议（hydrate 路径，渲染稳定）
 * - MeetingList 显示这 2 项
 * - 点击切换 → 选中项 aria-current="true"，另一项不再选中
 *
 * 注：顶栏转写流不再显示原始 meeting_id（已改为「当前会议」静态文案），
 * 因此用 aria-current 断言选中态，比依赖渲染出 id 更稳。
 */
import { test, expect } from "@playwright/test";
import { installEchoMock } from "./_mock";

const HOUR_AGO = new Date(Date.now() - 3_600_000).toISOString();
const HALF_HOUR_AGO = new Date(Date.now() - 1_800_000).toISOString();

test("点击会议列表切换当前会议", async ({ page }) => {
  // 用真实 hydrate 路径注入两个会议（覆盖 _mock 默认的空 /meetings）
  await page.route(/\/meetings(\?.*)?$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          meeting_id: "meeting-A",
          title: "会议 A",
          display_title: "会议 A",
          state: "ended",
          started_at: HOUR_AGO,
          ended_at: HALF_HOUR_AGO,
          finalized_at: null,
          n_segments: 12,
          n_speakers: 2,
          has_minutes: false,
        },
        {
          meeting_id: "meeting-B",
          title: "会议 B",
          display_title: "会议 B",
          state: "ended",
          started_at: HOUR_AGO,
          ended_at: HALF_HOUR_AGO,
          finalized_at: null,
          n_segments: 8,
          n_speakers: 3,
          has_minutes: false,
        },
      ]),
    });
  });

  await installEchoMock(page, { skipPaths: ["/meetings"] });
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  const itemA = page.locator('[data-testid="meeting-item"][data-meeting-id="meeting-A"]');
  const itemB = page.locator('[data-testid="meeting-item"][data-meeting-id="meeting-B"]');
  await expect(itemA).toBeVisible({ timeout: 8_000 });
  await expect(itemB).toBeVisible({ timeout: 8_000 });

  // 点击 meeting-A → 选中态切到 A
  await itemA.click();
  await expect(itemA).toHaveAttribute("aria-current", "true", { timeout: 3_000 });
  await expect(itemB).not.toHaveAttribute("aria-current", "true");

  // 点击 meeting-B → 选中态切到 B
  await itemB.click();
  await expect(itemB).toHaveAttribute("aria-current", "true", { timeout: 3_000 });
  await expect(itemA).not.toHaveAttribute("aria-current", "true");
});
