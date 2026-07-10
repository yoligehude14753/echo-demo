import { expect, test, _electron as electron, type ElectronApplication, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const APP_BIN =
  process.env.ECHODESK_APP_BIN ?? "/Applications/EchoDesk.app/Contents/MacOS/EchoDesk";
const TEST_FILE_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(TEST_FILE_DIR, "../../..");
const BACKEND_ROOT =
  process.env.ECHODESK_BACKEND_ROOT ?? path.join(REPO_ROOT, "backend");
const PYTHON_BIN = path.join(BACKEND_ROOT, ".venv/bin/python");
const TEST_ROOT = path.join(os.tmpdir(), "echodesk-0.3-installed-e2e");
const DB_PATH = path.join(TEST_ROOT, "echodesk.db");
const STORAGE_DIR = path.join(TEST_ROOT, "storage");
const SKILL_DIR = path.join(TEST_ROOT, "skill_build");
const USER_DIR = path.join(TEST_ROOT, "user");
const ELECTRON_USER_DATA = path.join(TEST_ROOT, "electron-user-data");
const SCREENSHOT_DIR = path.join(REPO_ROOT, "desktop/test-results/installed-local");
const BACKEND_PORT = 8769;
const TODO_ID = "todo-installed-local-e2e";
const DEVICE_ID = "desktop-installed-local-e2e";
const TODO_COMMAND =
  "@生成 TXT 依据以下已提供事实写一份不少于 700 字符的纯文本验收报告，第一行必须包含 ECHODESK_TODO_E2E_OK。已验证事实：EchoDesk 版本为 0.3.0-alpha.1；本地 backend 使用隔离 SQLite；首次 Todo run 因 1 秒超时进入 failed 并写入 workflow_events；应用完全退出并重启后，会议、Todo failed 状态和原 run_id 均从数据库恢复；重试请求携带 retry_of 指向原 run；成功产物必须进入 artifacts 和 artifact_links，并能通过会议 artifacts API 下载。请按执行摘要、状态转换、持久化证据、结论与下一步四节展开，以上内容就是完整事实材料，不要回答资料不足。";

type JsonMap = Record<string, unknown>;

async function isPortOpen(port: number): Promise<boolean> {
  return await new Promise<boolean>((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port });
    const finish = (open: boolean) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(open);
    };
    socket.setTimeout(500);
    socket.once("connect", () => finish(true));
    socket.once("timeout", () => finish(false));
    socket.once("error", () => finish(false));
  });
}

async function waitForPort(open: boolean, timeout = 45_000): Promise<void> {
  await expect
    .poll(() => isPortOpen(BACKEND_PORT), { timeout, intervals: [250, 500, 1000] })
    .toBe(open);
}

async function api<T extends JsonMap | JsonMap[]>(
  win: Page,
  endpoint: string,
  method = "GET",
  body?: JsonMap,
): Promise<T> {
  const result = await win.evaluate(
    async ({ endpoint: apiEndpoint, method: apiMethod, body: apiBody }) => {
      const echo = (window as unknown as {
        echo?: { getBackendHost?: () => Promise<string> };
      }).echo;
      const base = await echo?.getBackendHost?.();
      if (!base) throw new Error("backend host unavailable");
      const response = await fetch(`${base}${apiEndpoint}`, {
        method: apiMethod,
        cache: "no-store",
        headers: apiBody ? { "Content-Type": "application/json" } : undefined,
        body: apiBody ? JSON.stringify(apiBody) : undefined,
      });
      const text = await response.text();
      if (!response.ok) throw new Error(`${response.status}: ${text}`);
      return JSON.parse(text) as JsonMap | JsonMap[];
    },
    { endpoint, method, body },
  );
  return result as T;
}

async function launchInstalled(skillTimeoutSeconds: number): Promise<{
  app: ElectronApplication;
  win: Page;
}> {
  await waitForPort(false, 15_000);
  const env = {
    ...process.env,
    ECHO_BACKEND_CWD: BACKEND_ROOT,
    ECHO_PYTHON: PYTHON_BIN,
    ECHO_BACKEND_PORT: String(BACKEND_PORT),
    ECHO_BACKEND_BIND_HOST: "127.0.0.1",
    ECHODESK_DISABLE_AUTO_UPDATE_DOWNLOAD: "1",
    ECHO_USER_DIR: USER_DIR,
    DB_PATH,
    STORAGE_DIR,
    SKILL_EXECUTOR_BUILD_DIR: SKILL_DIR,
    SKILL_EXECUTOR_TIMEOUT_S: String(skillTimeoutSeconds),
    LLM_MAIN_PROVIDER: "yunwu",
    LLM_MAIN_MODEL: "claude-sonnet-4-6",
    LLM_MAIN_BASE_URL: "http://127.0.0.1:4190/v1",
    YUNWU_OPEN_KEY: "local-claude-direct-e2e",
    AGENT_OS_ENABLED: "true",
    AGENT_OS_URL: "http://127.0.0.1:4128",
    AGENT_TASK_TIMEOUT_S: "300",
  };
  delete env.ECHO_PUBLIC_DEMO;
  delete env.ECHO_FORCE_LOCAL_BACKEND;

  const app = await electron.launch({
    executablePath: APP_BIN,
    cwd: path.dirname(APP_BIN),
    args: [`--user-data-dir=${ELECTRON_USER_DATA}`],
    env,
    timeout: 60_000,
  });
  const child = app.process();
  child.stdout?.on("data", (chunk) => process.stdout.write(`[installed-app] ${String(chunk)}`));
  child.stderr?.on("data", (chunk) => process.stderr.write(`[installed-app] ${String(chunk)}`));
  const win = await app.firstWindow({ timeout: 60_000 });
  win.on("console", (msg) => process.stdout.write(`[installed-renderer:${msg.type()}] ${msg.text()}\n`));
  win.on("pageerror", (error) =>
    process.stderr.write(`[installed-renderer:error] ${error.stack ?? error.message}\n`),
  );
  await win.waitForLoadState("domcontentloaded");
  await win.evaluate((deviceId) => {
    window.localStorage.setItem("echodesk.onboarding.completed", "1");
    window.localStorage.setItem("echodesk.agentDeviceId", deviceId);
  }, DEVICE_ID);
  await win.reload({ waitUntil: "domcontentloaded" });

  await expect(win.getByText("EchoDesk", { exact: true }).first()).toBeVisible({
    timeout: 30_000,
  });
  expect(
    await win.evaluate(
      () =>
        (window as unknown as { __ECHODESK_REACT_MOUNT_COUNT__?: number })
          .__ECHODESK_REACT_MOUNT_COUNT__,
    ),
  ).toBe(1);
  await expect
    .poll(
      async () =>
        await win.evaluate(
          () => (window as unknown as { echo?: { isPublicDemo?: boolean } }).echo?.isPublicDemo,
        ),
      { timeout: 10_000 },
    )
    .toBe(false);
  await expect
    .poll(
      async () =>
        await win.evaluate(async () =>
          (window as unknown as { echo?: { getBackendHost?: () => Promise<string> } }).echo
            ?.getBackendHost?.(),
        ),
      { timeout: 10_000 },
    )
    .toBe(`http://127.0.0.1:${BACKEND_PORT}`);
  await expect
    .poll(
      async () =>
        await win.evaluate(async () => {
          try {
            const base = await (window as unknown as {
              echo?: { getBackendHost?: () => Promise<string> };
            }).echo?.getBackendHost?.();
            if (!base) return false;
            const response = await fetch(`${base}/healthz`);
            return response.ok;
          } catch {
            return false;
          }
        }),
      { timeout: 60_000, intervals: [500, 1000, 2000] },
    )
    .toBe(true);
  await expect(win.locator(".app-connection-status")).toHaveText("已连接", {
    timeout: 30_000,
  });
  return { app, win };
}

async function closeInstalled(app: ElectronApplication): Promise<void> {
  const child = app.process();
  try {
    await app.evaluate(async ({ app: electronApp }) => electronApp.quit());
  } catch {
    // The Playwright transport can close before evaluate resolves when quit succeeds.
  }
  await expect
    .poll(() => child.exitCode !== null || child.signalCode !== null, {
      timeout: 30_000,
      intervals: [100, 250, 500],
    })
    .toBe(true);
  await waitForPort(false, 30_000);
}

function sqlQuote(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

function seedFinalizedMinutes(meetingId: string): void {
  const now = new Date().toISOString();
  const minutes = {
    meeting_id: meetingId,
    title: "0.3 本机 Workflow 验收",
    duration_sec: 120,
    speakers: [],
    summary: "通过已安装 EchoDesk 验证 Todo workflow 的失败、重试、产物关联与重启恢复。",
    sections: [
      {
        heading: "验收要求",
        bullets: ["真实 backend", "真实 SQLite", "真实 Claude 模型输出"],
      },
    ],
    decisions: ["Todo 失败后必须保留 retry lineage"],
    todos: [
      {
        id: TODO_ID,
        text: "生成包含 ECHODESK_TODO_E2E_OK 的 TXT Workflow 验收报告",
        assignee: "EchoDesk",
        kind: "actionable",
        status: "pending",
        done_at: null,
        artifact_id: null,
        suggested_command: TODO_COMMAND,
      },
    ],
    action_items: [],
    raw_transcript_ref: null,
    created_at: now,
  };
  const sql = `
    UPDATE meetings
       SET title = ${sqlQuote(minutes.title)},
           display_title = ${sqlQuote(minutes.title)},
           state = 'finalized',
           ended_at = ${sqlQuote(now)},
           finalized_at = ${sqlQuote(now)},
           minutes_json = ${sqlQuote(JSON.stringify(minutes))},
           minutes_status = 'ok',
           minutes_error = NULL
     WHERE id = ${sqlQuote(meetingId)};
  `;
  execFileSync("/usr/bin/sqlite3", [DB_PATH, sql], { stdio: "inherit" });
}

async function selectMeeting(win: Page, meetingId: string): Promise<ReturnType<Page["locator"]>> {
  const meetingItem = win.locator(`[data-meeting-id="${meetingId}"]`);
  await expect(meetingItem).toBeVisible({ timeout: 30_000 });
  await meetingItem.click();
  const row = win.locator(`[data-todo-id="${TODO_ID}"]`);
  await expect(row).toBeVisible({ timeout: 30_000 });
  return row;
}

test.describe.serial("installed EchoDesk 0.3 local workflow", () => {
  test.skip(!existsSync(APP_BIN), `installed app missing: ${APP_BIN}`);
  test.skip(!existsSync(PYTHON_BIN), `backend python missing: ${PYTHON_BIN}`);

  test("installed app: failure → restart restore → retry success → AgentOS", async () => {
    test.setTimeout(900_000);
    rmSync(TEST_ROOT, { recursive: true, force: true });
    mkdirSync(SCREENSHOT_DIR, { recursive: true });

    let first = await launchInstalled(180);
    const appVersion = await first.app.evaluate(async ({ app }) => app.getVersion());
    expect(appVersion).toBe("0.3.0-alpha.1");
    expect(
      await first.win.evaluate(
        () => (window as unknown as { echo?: { isPublicDemo?: boolean } }).echo?.isPublicDemo,
      ),
    ).toBe(false);
    await expect
      .poll(
        async () =>
          await first.win.evaluate(async () =>
            (window as unknown as { echo?: { getBackendHost?: () => Promise<string> } })
              .echo?.getBackendHost?.(),
          ),
      )
      .toBe("http://127.0.0.1:8769");

    const health = await api<JsonMap>(first.win, "/healthz/full");
    expect((health.backend as JsonMap).version).toBe("0.3.0-alpha.1");
    const meetingBar = first.win.getByTestId("meeting-status-bar");
    await expect(meetingBar).toBeEnabled({ timeout: 30_000 });
    await meetingBar.click();
    await expect(meetingBar).toContainText("会议中", { timeout: 20_000 });
    const current = await api<JsonMap>(first.win, "/meetings/current");
    const meetingId = String(current.meeting_id);
    expect(meetingId).toMatch(/^m-/);
    await first.win.evaluate(
      ({ meetingId: id, todoId }) => {
        window.localStorage.setItem(`echodesk:auto-exec:v1:${id}:${todoId}`, "1");
      },
      { meetingId, todoId: TODO_ID },
    );
    await first.win.screenshot({
      path: path.join(SCREENSHOT_DIR, "01-installed-meeting-started.png"),
      fullPage: true,
    });
    await closeInstalled(first.app);
    seedFinalizedMinutes(meetingId);

    const failing = await launchInstalled(1);
    const restoredMeetings = await api<JsonMap[]>(failing.win, "/meetings?limit=50");
    expect(restoredMeetings.some((meeting) => meeting.meeting_id === meetingId)).toBe(true);
    let row = await selectMeeting(failing.win, meetingId);
    await expect(row).toHaveAttribute("data-todo-status", "pending");
    await row.getByTestId("minutes-todo-execute-btn").click();
    const textarea = failing.win.getByTestId("command-textarea");
    await expect(textarea).toHaveValue(TODO_COMMAND);
    await textarea.press("Enter");
    await expect(row).toHaveAttribute("data-todo-status", "failed", {
      timeout: 45_000,
    });
    await expect(row.getByTestId("minutes-todo-execute-btn")).toHaveText("重试");
    await expect(failing.win.getByTestId("failed-artifact-card").first()).toBeVisible();
    const failedRuns = await api<JsonMap[]>(
      failing.win,
      `/workflows/runs?meeting_id=${meetingId}&todo_id=${TODO_ID}&limit=20`,
    );
    const failedRun = failedRuns.find((run) => run.state === "failed");
    expect(failedRun).toBeDefined();
    const failedRunId = String(failedRun!.run_id);
    const failedEventResponse = await api<JsonMap>(
      failing.win,
      `/workflows/runs/${failedRunId}/events`,
    );
    const failedEvents = (failedEventResponse.events as JsonMap[]) ?? [];
    expect(failedEvents.some((event) => event.state === "failed")).toBe(true);
    await failing.win.screenshot({
      path: path.join(SCREENSHOT_DIR, "02-installed-todo-failed.png"),
      fullPage: true,
    });
    await closeInstalled(failing.app);

    const working = await launchInstalled(180);
    row = await selectMeeting(working.win, meetingId);
    await expect(row).toHaveAttribute("data-todo-status", "failed", { timeout: 30_000 });
    await expect(row).toHaveAttribute("data-workflow-run-id", failedRunId);
    await row.getByTestId("minutes-todo-execute-btn").click();
    await working.win.getByTestId("command-textarea").press("Enter");
    await expect(row).toHaveAttribute("data-todo-status", "done", {
      timeout: 240_000,
    });
    await expect(row.getByTestId("minutes-todo-artifact-link")).toBeVisible();

    const runs = await api<JsonMap[]>(
      working.win,
      `/workflows/runs?meeting_id=${meetingId}&todo_id=${TODO_ID}&limit=20`,
    );
    const succeededRun = runs.find((run) => run.state === "succeeded");
    expect(succeededRun).toBeDefined();
    expect((succeededRun!.input as JsonMap).retry_of).toBe(failedRunId);
    const meetingArtifacts = await api<JsonMap[]>(
      working.win,
      `/meetings/${meetingId}/artifacts`,
    );
    expect(meetingArtifacts).toHaveLength(1);
    const todoArtifactId = String(meetingArtifacts[0].artifact_id);
    const downloadText = await working.win.evaluate(async (artifactId) => {
      const base = await (window as unknown as {
        echo?: { getBackendHost?: () => Promise<string> };
      }).echo?.getBackendHost?.();
      const response = await fetch(`${base}/artifacts/${artifactId}/download`);
      return await response.text();
    }, todoArtifactId);
    expect(downloadText).toContain("ECHODESK_TODO_E2E_OK");

    const agentTask = await api<JsonMap>(working.win, "/agents/tasks", "POST", {
      device_id: DEVICE_ID,
      text: "Create agent_e2e_result.txt in the current workspace containing exactly ECHODESK_AGENT_E2E_OK. Do not do anything else.",
      title: "Installed AgentOS artifact import",
      task_kind: "agent_task",
      timeout_s: 180,
    });
    const agentTaskId = String(agentTask.task_id);
    const agentCard = working.win.locator(`[data-task-id="${agentTaskId}"]`);
    await expect(agentCard).toContainText("等待授权", { timeout: 30_000 });
    await agentCard.getByRole("button", { name: "允许并开始" }).click();
    await expect(agentCard).toContainText("已完成", { timeout: 300_000 });

    await expect
      .poll(
        async () => {
          const artifacts = await api<JsonMap[]>(working.win, "/artifacts?limit=120");
          return artifacts.some((artifact) =>
            String(artifact.title ?? "").includes("agent_e2e_result"),
          );
        },
        { timeout: 60_000, intervals: [1000, 2000] },
      )
      .toBe(true);
    const agentRuns = await api<JsonMap[]>(
      working.win,
      `/workflows/runs?agent_task_id=${agentTaskId}&limit=20`,
    );
    expect(agentRuns.some((run) => run.state === "succeeded")).toBe(true);

    const cancelTask = await api<JsonMap>(working.win, "/agents/tasks", "POST", {
      device_id: DEVICE_ID,
      text: "Inspect every available file in this workspace and write a long report named cancel_me.md.",
      title: "Installed AgentOS cancel",
      task_kind: "agent_task",
      timeout_s: 300,
    });
    const cancelTaskId = String(cancelTask.task_id);
    const cancelCard = working.win.locator(`[data-task-id="${cancelTaskId}"]`);
    await expect(cancelCard.getByRole("button", { name: "取消" })).toBeVisible({
      timeout: 30_000,
    });
    await cancelCard.getByRole("button", { name: "取消" }).click();
    await expect(cancelCard).toContainText("已取消", { timeout: 30_000 });
    const cancelledRuns = await api<JsonMap[]>(
      working.win,
      `/workflows/runs?agent_task_id=${cancelTaskId}&limit=20`,
    );
    expect(cancelledRuns.some((run) => run.state === "cancelled")).toBe(true);

    const timeoutTask = await api<JsonMap>(working.win, "/agents/tasks", "POST", {
      device_id: DEVICE_ID,
      text: "Create timeout_probe.txt containing timeout probe.",
      title: "Installed AgentOS timeout",
      task_kind: "agent_task",
      timeout_s: 0.05,
    });
    const timeoutTaskId = String(timeoutTask.task_id);
    const timeoutCard = working.win.locator(`[data-task-id="${timeoutTaskId}"]`);
    await expect(timeoutCard).toContainText("已超时", { timeout: 45_000 });
    const timeoutRuns = await api<JsonMap[]>(
      working.win,
      `/workflows/runs?agent_task_id=${timeoutTaskId}&limit=20`,
    );
    expect(timeoutRuns.some((run) => run.state === "timeout")).toBe(true);

    await working.win.screenshot({
      path: path.join(SCREENSHOT_DIR, "03-installed-workflows-complete.png"),
      fullPage: true,
    });
    await closeInstalled(working.app);

    first = await launchInstalled(180);
    row = await selectMeeting(first.win, meetingId);
    await expect(row).toHaveAttribute("data-todo-status", "done", { timeout: 30_000 });
    await expect(first.win.locator(`[data-task-id="${agentTaskId}"]`)).toContainText("已完成");
    await expect(first.win.locator(`[data-task-id="${cancelTaskId}"]`)).toContainText("已取消");
    await expect(first.win.locator(`[data-task-id="${timeoutTaskId}"]`)).toContainText("已超时");
    await first.win.screenshot({
      path: path.join(SCREENSHOT_DIR, "04-installed-restart-restored.png"),
      fullPage: true,
    });
    await closeInstalled(first.app);
  });
});
