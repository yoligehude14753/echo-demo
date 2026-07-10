import { expect, test } from "@playwright/test";
import { installScenarioMock } from "./_helpers";

const MEETING_ID = "m-workflow-restore-001";
const TODO_ID = "todo-workflow-restore-001";

test("S08 · 重启后从 workflow history 恢复 Todo 失败态", async ({ page }) => {
  await page.route(/\/(api\/)?meetings(\?|$)/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          meeting_id: MEETING_ID,
          title: "Workflow 恢复验收",
          display_title: "Workflow 恢复验收",
          state: "finalized",
          started_at: "2026-07-10T01:00:00Z",
          ended_at: "2026-07-10T01:10:00Z",
          finalized_at: "2026-07-10T01:10:10Z",
          n_segments: 0,
          n_speakers: 0,
          has_minutes: true,
        },
      ]),
    });
  });
  await page.route(
    new RegExp(`/(api/)?meetings/${MEETING_ID}/transcript$`),
    async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    },
  );
  await page.route(
    new RegExp(`/(api/)?meetings/${MEETING_ID}/artifacts$`),
    async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    },
  );
  await page.route(
    new RegExp(`/(api/)?meetings/${MEETING_ID}/minutes$`),
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          meeting_id: MEETING_ID,
          title: "Workflow 恢复验收",
          duration_sec: 600,
          summary: "验证桌面端从后端 workflow history 恢复 Todo 执行态。",
          sections: [],
          decisions: [],
          todos: [
            {
              id: TODO_ID,
              text: "生成恢复验收 TXT",
              kind: "actionable",
              status: "pending",
              suggested_command: "@生成 TXT 恢复验收",
            },
          ],
          action_items: [],
          created_at: "2026-07-10T01:10:10Z",
        }),
      });
    },
  );
  await page.route(/\/(api\/)?workflows\/runs\?/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          run_id: "run-workflow-restore-001",
          kind: "artifact_generation",
          source: "todo",
          state: "failed",
          title: "生成恢复验收 TXT",
          intent_text: "@生成 TXT 恢复验收",
          meeting_id: MEETING_ID,
          todo_id: TODO_ID,
          agent_task_id: null,
          input: { artifact_type: "txt" },
          output: {},
          error: "fixture failure",
          timeout_s: 300,
          created_at: "2026-07-10T01:11:00Z",
          started_at: "2026-07-10T01:11:01Z",
          finished_at: "2026-07-10T01:11:02Z",
          updated_at: "2026-07-10T01:11:02Z",
        },
      ]),
    });
  });

  const mock = await installScenarioMock(page, {
    skipPaths: ["/meetings", "/workflows/runs"],
  });

  await page.goto("/");
  await page.locator(`[data-meeting-id="${MEETING_ID}"]`).click();

  const row = page.locator(`[data-todo-id="${TODO_ID}"]`);
  await expect(row).toHaveAttribute("data-todo-status", "failed", { timeout: 8_000 });
  await expect(row).toHaveAttribute("data-workflow-run-id", "run-workflow-restore-001");
  await expect(row).toContainText("失败，可重试");
  const retryButton = row.getByTestId("minutes-todo-execute-btn");
  await expect(retryButton).toHaveText("重试");
  await retryButton.click();

  const textarea = page.getByTestId("command-textarea");
  await expect(textarea).toHaveValue("@生成 TXT 恢复验收");
  await textarea.press("Enter");

  await expect
    .poll(async () => {
      const log = await mock.fetchLog();
      return log.some(
        (entry) => entry.method === "POST" && entry.url.includes("/artifacts/generate"),
      );
    })
    .toBe(true);
  const request = (await mock.fetchLog()).find(
    (entry) => entry.method === "POST" && entry.url.includes("/artifacts/generate"),
  );
  const body = JSON.parse(request?.bodyText ?? "{}") as { retry_of_run_id?: string };
  expect(body.retry_of_run_id).toBe("run-workflow-restore-001");
});
