/**
 * 场景 7：纪要生成失败 → 用户点「重试」恢复
 *
 * 覆盖业务目标（19-quality-detail.mdc 三问）：
 *   主路径：minutes.ready 到达 → MinutesView 渲染纪要详情
 *   失败路径：minutes.failed 到达 → MinutesView 显示可读原因和重试操作
 *   重试路径：点「重新生成纪要」→ POST /meetings/{id}/finalize 触发；
 *             ready 再次到达 → UI 切换到纪要详情
 *
 * 回归 bug：echo-demo backend.log 2026-05-28 10:39:04
 *   manual_end finalize failed (missing title) → 用户卡在「纪要尚未生成」永远没有重试入口
 */
import { test, expect } from "@playwright/test";
import {
  installScenarioMock,
  publishMeetingEnded,
  publishMeetingStarted,
  publishMinutesFailed,
  publishMinutesReady,
} from "./_helpers";

test("S07 · LLM 失败 → 「生成失败 · 重试」→ 重试成功 → 渲染纪要", async ({
  page,
}) => {
  const meetingId = "m-test-failure";

  const mock = await installScenarioMock(page);

  await test.step("打开主界面，等连接 OK", async () => {
    await page.goto("/");
    await expect(page.getByTestId("pill-backend")).toBeVisible({ timeout: 5_000 });
  });

  await test.step("会议开始 → 结束 → 后端 LLM 失败 (publish minutes.failed)", async () => {
    await publishMeetingStarted(mock, meetingId, 1);
    await publishMeetingEnded(mock, meetingId, 2);
    await publishMinutesFailed(
      mock,
      meetingId,
      "model service 502 bad gateway (mocked)",
      3,
    );
  });

  await test.step("MinutesView 显示可读错误 + 「重新生成纪要」按钮", async () => {
    await expect(page.getByTestId("minutes-retry-btn")).toBeVisible({
      timeout: 5_000,
    });
    await expect(page.getByTestId("minutes-error-headline")).toHaveText(
      "纪要生成失败",
    );
    await expect(
      page.getByText("点击下方按钮重新生成；如反复失败，请检查网络或服务设置"),
    ).toBeVisible();
    await expect(page.getByText(/502 bad gateway/)).toHaveCount(0);
  });

  await test.step("点击重试 → 触发 POST /meetings/{id}/finalize", async () => {
    await page.getByTestId("minutes-retry-btn").click();
    await expect
      .poll(
        async () => {
          const log = await mock.fetchLog();
          return log.find(
            (r) =>
              r.method === "POST" &&
              r.url.includes(`/meetings/${meetingId}/finalize`),
          );
        },
        { timeout: 5_000 },
      )
      .toBeTruthy();
  });

  await test.step("后端重试成功 → publish minutes.ready → 切换到纪要详情视图", async () => {
    await publishMinutesReady(mock, meetingId, 4);
    // 失败 UI 消失
    await expect(page.getByTestId("minutes-retry-btn")).toHaveCount(0, {
      timeout: 5_000,
    });
    // 纪要 summary 出现
    await expect(page.getByTestId("minutes-title")).toHaveText("测试纪要", {
      timeout: 5_000,
    });
    await expect(page.locator("text=Q3 销售目标拆解")).toBeVisible();
  });
});
