import { expect, test, _electron as electron, type ElectronApplication, type Page } from "@playwright/test";
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import {
  accessSync,
  constants,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
} from "node:fs";
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
const EXPECTED_BACKEND_BIN =
  process.env.ECHODESK_EXPECTED_BACKEND_BIN ??
  path.resolve(path.dirname(APP_BIN), "../Resources/backend/echodesk-backend");
const AGENTOS_ROOT =
  process.env.ECHODESK_AGENTOS_ROOT ?? path.resolve(REPO_ROOT, "../agentos");
const MODEL_CONFIG_PATH =
  process.env.ECHODESK_REAL_CONFIG ?? path.join(os.homedir(), ".echodesk/config.json");
const TEST_ROOT = path.join(os.tmpdir(), "echodesk-0.3-installed-e2e");
const DB_PATH = path.join(TEST_ROOT, "echodesk.db");
const STORAGE_DIR = path.join(TEST_ROOT, "storage");
const SKILL_DIR = path.join(TEST_ROOT, "skill_build");
const USER_DIR = path.join(TEST_ROOT, "user");
const ELECTRON_USER_DATA = path.join(TEST_ROOT, "electron-user-data");
const SCREENSHOT_DIR = path.join(REPO_ROOT, "desktop/test-results/installed-local");
const BACKEND_PORT = 8769;
const AGENTOS_PROXY_PORT = 14127;
const AGENTOS_SERVER_PORT = 14128;
const DEVICE_ID = "desktop-installed-local-e2e";
const TODO_MARKER = "ECHODESK_TODO_E2E_OK";
const TRANSCRIPT_SEGMENTS = [
  "本次是 EchoDesk 0.3.2 安装态工作流验收。我们确认桌面客户端连接隔离的本地 backend 和 SQLite。",
  "明确决议：会议纪要、待办、产物和 AgentOS 都必须经过真实产品 API 和 Workflow Kernel，禁止直接修改数据库或使用 mock。",
  `行动项：请 EchoDesk 生成 TXT 纯文本验收报告，报告第一行必须原样包含 ${TODO_MARKER}，正文按执行摘要、状态转换、持久化证据、结论与下一步四节展开。`,
  "报告需要说明首次待办运行因执行超时失败，应用重启后恢复 failed 状态和原 run_id，随后重试成功并通过 retry_of 建立谱系，产物可从会议详情下载。",
];

type JsonMap = Record<string, unknown>;

type ModelConfig = {
  provider: string;
  baseUrl: string;
  model: string;
  apiKey: string;
};

type AgentServices = {
  proxy: ChildProcess;
  server: ChildProcess;
};

const activeApps = new Set<ElectronApplication>();
let activeAgentServices: AgentServices | null = null;

function loadModelConfig(): ModelConfig {
  if (!existsSync(MODEL_CONFIG_PATH) && !process.env.ECHODESK_REAL_BASE_URL) {
    throw new Error(`real main-model config missing: ${MODEL_CONFIG_PATH}`);
  }
  const config = existsSync(MODEL_CONFIG_PATH)
    ? (JSON.parse(readFileSync(MODEL_CONFIG_PATH, "utf8")) as JsonMap)
    : {};
  const provider = String(
    process.env.ECHODESK_REAL_PROVIDER ?? config.llm_main_provider ?? "openai-compatible",
  ).trim();
  const model = String(
    process.env.ECHODESK_REAL_MODEL ?? config.llm_main_model ?? "",
  ).trim();
  const baseUrl = String(
    process.env.ECHODESK_REAL_BASE_URL ?? config.llm_main_base_url ?? "",
  )
    .trim()
    .replace(/\/$/, "");
  const apiKey = String(
    process.env.ECHODESK_REAL_API_KEY ??
      config.llm_main_api_key ??
      config.yunwu_open_key ??
      "EMPTY",
  ).trim();
  if (!model || !baseUrl) {
    throw new Error(`real E2E requires a non-empty model and base URL in ${MODEL_CONFIG_PATH}`);
  }
  return { provider, apiKey: apiKey || "EMPTY", model, baseUrl };
}

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

async function waitForPortState(
  port: number,
  open: boolean,
  timeout = 45_000,
): Promise<void> {
  await expect
    .poll(() => isPortOpen(port), { timeout, intervals: [250, 500, 1000] })
    .toBe(open);
}

async function waitForPort(open: boolean, timeout = 45_000): Promise<void> {
  await waitForPortState(BACKEND_PORT, open, timeout);
}

function descendantCommands(parentPid: number): string[] {
  const output = execFileSync("/bin/ps", ["-axo", "ppid=,pid=,command="], {
    encoding: "utf8",
  });
  const rows = output
    .split("\n")
    .map((line) => line.match(/^\s*(\d+)\s+(\d+)\s+(.*)$/))
    .filter((match): match is RegExpMatchArray => match !== null)
    .map((match) => ({
      ppid: Number(match[1]),
      pid: Number(match[2]),
      command: match[3],
    }));
  const descendants = new Set([parentPid]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const row of rows) {
      if (descendants.has(row.ppid) && !descendants.has(row.pid)) {
        descendants.add(row.pid);
        changed = true;
      }
    }
  }
  return rows
    .filter((row) => descendants.has(row.pid))
    .map((row) => row.command);
}

function pipeProcessOutput(child: ChildProcess, label: string): void {
  child.stdout?.on("data", (chunk) => process.stdout.write(`[${label}] ${String(chunk)}`));
  child.stderr?.on("data", (chunk) => process.stderr.write(`[${label}] ${String(chunk)}`));
}

async function stopProcess(process: ChildProcess | undefined): Promise<void> {
  if (!process || process.exitCode !== null || process.signalCode !== null) return;
  process.kill("SIGTERM");
  try {
    await expect
      .poll(() => process.exitCode !== null || process.signalCode !== null, {
        timeout: 10_000,
        intervals: [100, 250, 500],
      })
      .toBe(true);
  } catch {
    process.kill("SIGKILL");
  }
}

async function startRealAgentServices(config: ModelConfig): Promise<AgentServices> {
  await waitForPortState(AGENTOS_PROXY_PORT, false, 5_000);
  await waitForPortState(AGENTOS_SERVER_PORT, false, 5_000);

  const agentRoot = path.join(TEST_ROOT, "agentos");
  const workspaces = path.join(agentRoot, "workspaces");
  mkdirSync(workspaces, { recursive: true });
  const commonEnv = {
    ...process.env,
    PYTHONPATH: AGENTOS_ROOT,
    HTTP_PROXY: "",
    HTTPS_PROXY: "",
    ALL_PROXY: "",
    NO_PROXY: "*",
  };
  const proxy = spawn(PYTHON_BIN, ["-m", "agentos.proxy.anthropic_to_openai"], {
    cwd: AGENTOS_ROOT,
    env: {
      ...commonEnv,
      PORT: String(AGENTOS_PROXY_PORT),
      AGENTOS_PROXY_AUTOCONFIG_ECHO: "false",
      AGENTOS_PROXY_UPSTREAM_BASE_URL: config.baseUrl,
      AGENTOS_PROXY_UPSTREAM_MODEL: config.model,
      AGENTOS_PROXY_UPSTREAM_API_KEY: config.apiKey,
      AGENTOS_PROXY_REASONING_TOKEN_BUDGET: "0",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  pipeProcessOutput(proxy, "agentos-proxy");
  await waitForPortState(AGENTOS_PROXY_PORT, true, 30_000);

  const server = spawn(
    PYTHON_BIN,
    [
      "-m",
      "agentos.server",
      "--host",
      "127.0.0.1",
      "--port",
      String(AGENTOS_SERVER_PORT),
      "--workspaces",
      workspaces,
      "--proxy-url",
      `http://127.0.0.1:${AGENTOS_PROXY_PORT}`,
      "--log-level",
      "info",
    ],
    {
      cwd: AGENTOS_ROOT,
      env: commonEnv,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  pipeProcessOutput(server, "agentos-server");
  try {
    await waitForPortState(AGENTOS_SERVER_PORT, true, 30_000);
    const response = await fetch(`http://127.0.0.1:${AGENTOS_SERVER_PORT}/api/v1/health`);
    if (!response.ok) throw new Error(`AgentOS health failed: ${response.status}`);
  } catch (error) {
    await stopProcess(server);
    await stopProcess(proxy);
    throw error;
  }
  const services = { proxy, server };
  activeAgentServices = services;
  return services;
}

async function stopRealAgentServices(services: AgentServices | null): Promise<void> {
  if (!services) return;
  await stopProcess(services.server);
  await stopProcess(services.proxy);
  await waitForPortState(AGENTOS_SERVER_PORT, false, 15_000);
  await waitForPortState(AGENTOS_PROXY_PORT, false, 15_000);
  if (activeAgentServices === services) activeAgentServices = null;
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

async function postForm<T extends JsonMap>(
  win: Page,
  endpoint: string,
  fields: Record<string, string>,
): Promise<T> {
  const result = await win.evaluate(
    async ({ endpoint: apiEndpoint, fields: formFields }) => {
      const base = await (window as unknown as {
        echo?: { getBackendHost?: () => Promise<string> };
      }).echo?.getBackendHost?.();
      if (!base) throw new Error("backend host unavailable");
      const response = await fetch(`${base}${apiEndpoint}`, {
        method: "POST",
        body: new URLSearchParams(formFields),
      });
      const text = await response.text();
      if (!response.ok) throw new Error(`${response.status}: ${text}`);
      return JSON.parse(text) as JsonMap;
    },
    { endpoint, fields },
  );
  return result as T;
}

async function ssePost(
  win: Page,
  endpoint: string,
  body: JsonMap,
): Promise<{ text: string; events: JsonMap[] }> {
  const raw = await win.evaluate(
    async ({ endpoint: apiEndpoint, body: apiBody }) => {
      const base = await (window as unknown as {
        echo?: { getBackendHost?: () => Promise<string> };
      }).echo?.getBackendHost?.();
      if (!base) throw new Error("backend host unavailable");
      const response = await fetch(`${base}${apiEndpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(apiBody),
      });
      const text = await response.text();
      if (!response.ok) throw new Error(`${response.status}: ${text}`);
      return text;
    },
    { endpoint, body },
  );
  let text = "";
  const events: JsonMap[] = [];
  let completed = false;
  for (const block of raw.split("\n\n")) {
    const event = block
      .split("\n")
      .find((line) => line.startsWith("event: "))
      ?.slice(7)
      .trim();
    const payload = block
      .split("\n")
      .find((line) => line.startsWith("data: "))
      ?.slice(6);
    if (!payload) continue;
    if (payload === "[DONE]") {
      completed = true;
      continue;
    }
    const decoded = JSON.parse(payload) as JsonMap;
    events.push(decoded);
    const frameType = typeof decoded.type === "string" ? decoded.type : event;
    if (frameType === "error" || typeof decoded.error === "string") {
      throw new Error(`SSE error from ${endpoint}: ${String(decoded.error ?? payload)}`);
    }
    if (frameType === "done") {
      completed = true;
      if (typeof decoded.answer === "string") text = decoded.answer;
      continue;
    }
    if (typeof decoded.delta === "string") text += decoded.delta;
  }
  if (!completed) throw new Error(`SSE stream from ${endpoint} ended before a terminal frame`);
  if (!text.trim()) throw new Error(`SSE stream from ${endpoint} returned an empty answer`);
  return { text, events };
}

async function launchInstalled(skillTimeoutSeconds: number, modelConfig: ModelConfig): Promise<{
  app: ElectronApplication;
  win: Page;
}> {
  await waitForPort(false, 15_000);
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    ECHO_BACKEND_PORT: String(BACKEND_PORT),
    ECHO_BACKEND_BIND_HOST: "127.0.0.1",
    ECHODESK_DISABLE_AUTO_UPDATE_DOWNLOAD: "1",
    ECHO_USER_DIR: USER_DIR,
    DB_PATH,
    STORAGE_DIR,
    SKILL_EXECUTOR_BUILD_DIR: SKILL_DIR,
    SKILL_EXECUTOR_TIMEOUT_S: String(skillTimeoutSeconds),
    LLM_MAIN_PROVIDER: modelConfig.provider,
    LLM_MAIN_MODEL: modelConfig.model,
    LLM_MAIN_BASE_URL: modelConfig.baseUrl,
    LLM_MAIN_API_KEY: modelConfig.apiKey,
    LLM_FAST_PROVIDER: modelConfig.provider,
    LLM_FAST_MODEL: modelConfig.model,
    LLM_FAST_BASE_URL: modelConfig.baseUrl,
    LLM_LOCAL_API_KEY: modelConfig.apiKey,
    TTS_ENABLED: "false",
    AMBIENT_CAPTURE_ENABLED: "false",
    AGENT_OS_ENABLED: "true",
    AGENT_OS_URL: `http://127.0.0.1:${AGENTOS_SERVER_PORT}`,
    AGENT_TASK_TIMEOUT_S: "300",
  };
  for (const name of [
    "ECHO_BACKEND_CWD",
    "ECHO_PYTHON",
    "ECHO_ALLOW_PACKAGED_SOURCE_BACKEND",
    "ECHO_PUBLIC_DEMO",
    "ELECTRON_DEV",
    "VITE_DEV_URL",
    "PYTHONPATH",
  ]) {
    delete env[name];
  }
  env.ECHO_FORCE_LOCAL_BACKEND = "1";

  const app = await electron.launch({
    executablePath: APP_BIN,
    cwd: path.dirname(APP_BIN),
    args: [`--user-data-dir=${ELECTRON_USER_DATA}`],
    env,
    timeout: 60_000,
  });
  activeApps.add(app);
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
  const electronPid = app.process().pid;
  expect(electronPid).toBeTruthy();
  await expect
    .poll(
      () =>
        descendantCommands(electronPid ?? -1).some((command) =>
          command.includes(EXPECTED_BACKEND_BIN),
        ),
      { timeout: 30_000 },
    )
    .toBe(true);
  const backendCommands = descendantCommands(electronPid ?? -1);
  expect(
    backendCommands.some(
      (command) =>
        command.includes(PYTHON_BIN) ||
        command.includes("app.main:app") ||
        command.includes(" -m uvicorn "),
    ),
    `installed app must not launch source backend: ${backendCommands.join(" | ")}`,
  ).toBe(false);
  return { app, win };
}

async function closeInstalled(app: ElectronApplication): Promise<void> {
  const child = app.process();
  try {
    await app.evaluate(async ({ app: electronApp }) => electronApp.quit());
  } catch {
    // The Playwright transport can close before evaluate resolves when quit succeeds.
  }
  try {
    await expect
      .poll(() => child.exitCode !== null || child.signalCode !== null, {
        timeout: 30_000,
        intervals: [100, 250, 500],
      })
      .toBe(true);
    await waitForPort(false, 30_000);
  } finally {
    activeApps.delete(app);
  }
}

async function selectMeeting(
  win: Page,
  meetingId: string,
  todoId: string,
): Promise<ReturnType<Page["locator"]>> {
  const row = win.locator(`[data-todo-id="${todoId}"]`);
  if (!(await row.isVisible())) {
    const meetingItem = win.locator(`[data-meeting-id="${meetingId}"]`);
    await expect(meetingItem).toBeVisible({ timeout: 30_000 });
    await meetingItem.click();
  }
  if (!(await row.isVisible())) {
    const minutesTab = win.getByRole("tab", { name: "会议纪要" });
    await expect(minutesTab).toBeVisible({ timeout: 30_000 });
    await minutesTab.click();
  }
  await expect(row).toBeVisible({ timeout: 30_000 });
  return row;
}

test.describe.serial("installed EchoDesk 0.3 local workflow", () => {
  test.skip(!existsSync(APP_BIN), `installed app missing: ${APP_BIN}`);
  test.skip(!existsSync(PYTHON_BIN), `backend python missing: ${PYTHON_BIN}`);
  test.skip(
    !existsSync(path.join(AGENTOS_ROOT, "agentos/server/__main__.py")),
    `AgentOS source missing: ${AGENTOS_ROOT}`,
  );

  test.afterEach(async () => {
    for (const app of [...activeApps]) {
      try {
        await closeInstalled(app);
      } catch {
        app.process().kill("SIGKILL");
        activeApps.delete(app);
      }
    }
    await stopRealAgentServices(activeAgentServices);
  });

  test("installed app: GLM chat/RAG → failure/restart/retry → real AgentOS", async () => {
    test.setTimeout(900_000);
    expect(existsSync(EXPECTED_BACKEND_BIN), `bundled backend missing: ${EXPECTED_BACKEND_BIN}`).toBe(
      true,
    );
    expect(() => accessSync(EXPECTED_BACKEND_BIN, constants.X_OK)).not.toThrow();
    rmSync(TEST_ROOT, { recursive: true, force: true });
    mkdirSync(SCREENSHOT_DIR, { recursive: true });
    const modelConfig = loadModelConfig();
    const agentServices = await startRealAgentServices(modelConfig);

    let first = await launchInstalled(1, modelConfig);
    const appVersion = await first.app.evaluate(async ({ app }) => app.getVersion());
    expect(appVersion).toBe("0.3.2");
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
    expect((health.backend as JsonMap).version).toBe("0.3.2");
    const chat = await ssePost(first.win, "/chat", {
      question: "只回复 ECHODESK_CHAT_GL5_OK，不要添加其他内容。",
      model: "FAST",
    });
    expect(chat.text).toContain("ECHODESK_CHAT_GL5_OK");
    const meetingBar = first.win.getByTestId("meeting-status-bar");
    await expect(meetingBar).toBeEnabled({ timeout: 30_000 });
    await meetingBar.click();
    await expect(meetingBar).toContainText("会议中", { timeout: 20_000 });
    const current = await api<JsonMap>(first.win, "/meetings/current");
    const meetingId = String(current.meeting_id);
    expect(meetingId).toMatch(/^m-/);
    for (const [index, text] of TRANSCRIPT_SEGMENTS.entries()) {
      await api<JsonMap>(first.win, `/meetings/${meetingId}/inject_segment`, "POST", {
        text,
        start_ms: index * 30_000,
        end_ms: (index + 1) * 30_000,
        speaker_label: index < 2 ? "说话人1" : "说话人2",
      });
    }
    const ended = await api<JsonMap>(first.win, "/meetings/manual_end", "POST");
    expect(ended.meeting_id).toBe(meetingId);
    const meetingsAfterEnd = await api<JsonMap[]>(first.win, "/meetings?limit=50");
    const meetingAfterEnd = meetingsAfterEnd.find(
      (meeting) => meeting.meeting_id === meetingId,
    );
    expect(meetingAfterEnd).toBeDefined();
    const stateAfterEnd = await api<JsonMap>(first.win, "/meetings/current");
    expect(stateAfterEnd.mode).toBe("idle");
    expect(["ok", "generation_failed"]).toContain(stateAfterEnd.minutes_status);
    let minutes: JsonMap;
    if (stateAfterEnd.minutes_status === "generation_failed") {
      expect(meetingAfterEnd!.state).toBe("ended");
      minutes = await postForm<JsonMap>(first.win, `/meetings/${meetingId}/finalize`, {
        title: "EchoDesk 0.3.2 安装态工作流验收",
      });
      const meetingsAfterRecovery = await api<JsonMap[]>(first.win, "/meetings?limit=50");
      const recoveredMeeting = meetingsAfterRecovery.find(
        (meeting) => meeting.meeting_id === meetingId,
      );
      expect(recoveredMeeting).toBeDefined();
      expect(recoveredMeeting!.state).toBe("finalized");
      const stateAfterRecovery = await api<JsonMap>(first.win, "/meetings/current");
      expect(stateAfterRecovery.mode).toBe("idle");
      expect(stateAfterRecovery.minutes_status).toBe("ok");
    } else {
      minutes = await api<JsonMap>(first.win, `/meetings/${meetingId}/minutes`);
    }
    const todos = (minutes.todos as JsonMap[]) ?? [];
    const actionableTodo = todos.find(
      (todo) => todo.kind === "actionable" && String(todo.suggested_command ?? "").startsWith("@"),
    );
    expect(actionableTodo, "real minutes must contain an executable todo").toBeDefined();
    const todoId = String(actionableTodo!.id);
    const todoCommand = String(actionableTodo!.suggested_command);
    expect(todoId).toMatch(/^t-/);
    expect(todoCommand.toLowerCase()).toContain("txt");
    const ragAnswer = await ssePost(first.win, "/rag/ask", {
      question: "EchoDesk 0.3.2 安装态工作流验收会议的行动项和报告要求是什么？",
      rag_top_k: 6,
      web_top_n: 0,
    });
    expect(ragAnswer.text).toMatch(/TXT|验收报告/);
    const ragMeta = ragAnswer.events.find(
      (event) => event.meta && typeof event.meta === "object",
    )?.meta as JsonMap | undefined;
    expect((ragMeta?.citations as JsonMap[] | undefined)?.length ?? 0).toBeGreaterThan(0);
    await expect
      .poll(
        async () => {
          const runs = await api<JsonMap[]>(
            first.win,
            `/workflows/runs?meeting_id=${meetingId}&todo_id=${todoId}&limit=20`,
          );
          return runs.some((run) => run.state === "failed");
        },
        { timeout: 45_000, intervals: [250, 500, 1000] },
      )
      .toBe(true);
    const failedRuns = await api<JsonMap[]>(
      first.win,
      `/workflows/runs?meeting_id=${meetingId}&todo_id=${todoId}&limit=20`,
    );
    const failedRun = failedRuns.find((run) => run.state === "failed");
    expect(failedRun).toBeDefined();
    const failedRunId = String(failedRun!.run_id);
    const failedEventResponse = await api<JsonMap>(
      first.win,
      `/workflows/runs/${failedRunId}/events`,
    );
    const failedEvents = (failedEventResponse.events as JsonMap[]) ?? [];
    expect(failedEvents.some((event) => event.state === "failed")).toBe(true);
    await first.win.screenshot({
      path: path.join(SCREENSHOT_DIR, "01-installed-todo-failed.png"),
      fullPage: true,
    });
    await closeInstalled(first.app);

    const working = await launchInstalled(180, modelConfig);
    const restoredMeetings = await api<JsonMap[]>(working.win, "/meetings?limit=50");
    expect(restoredMeetings.some((meeting) => meeting.meeting_id === meetingId)).toBe(true);
    let row = await selectMeeting(working.win, meetingId, todoId);
    await expect(row).toHaveAttribute("data-todo-status", "failed", { timeout: 30_000 });
    await expect(row).toHaveAttribute("data-workflow-run-id", failedRunId);
    await row.getByTestId("minutes-todo-execute-btn").click();
    await working.win.getByTestId("command-textarea").press("Enter");
    await expect(row).toHaveAttribute("data-todo-status", "done", {
      timeout: 240_000,
    });
    row = await selectMeeting(working.win, meetingId, todoId);
    const artifactDownloadButton = row.getByTestId("minutes-todo-artifact-link");
    await expect(artifactDownloadButton).toBeVisible();
    await expect(artifactDownloadButton).toBeEnabled();
    const isolatedDownloadDirectory = path.join(TEST_ROOT, "downloads");
    mkdirSync(isolatedDownloadDirectory, { recursive: true, mode: 0o700 });
    await working.app.evaluate(
      ({ app }, directory) => app.setPath("downloads", directory),
      isolatedDownloadDirectory,
    );
    const downloadDirectory = await working.app.evaluate(({ app }) =>
      app.getPath("downloads"),
    );
    expect(path.resolve(downloadDirectory)).toBe(
      path.resolve(isolatedDownloadDirectory),
    );
    const downloadsBefore = new Set(readdirSync(downloadDirectory));
    const productDownloadResponsePromise = working.win.waitForResponse((response) => {
      if (response.request().method() !== "GET") return false;
      try {
        return /\/artifacts\/[^/]+\/download$/.test(new URL(response.url()).pathname);
      } catch {
        return false;
      }
    }, {
      timeout: 120_000,
    });
    await artifactDownloadButton.click();
    const productDownloadResponse = await productDownloadResponsePromise;
    expect(productDownloadResponse.status()).toBe(200);
    await expect
      .poll(
        () =>
          readdirSync(downloadDirectory).filter((filename) => {
            if (downloadsBefore.has(filename) || filename.endsWith(".crdownload")) {
              return false;
            }
            const candidate = path.join(downloadDirectory, filename);
            try {
              const stat = statSync(candidate);
              return (
                stat.isFile() &&
                stat.size > 0 &&
                readFileSync(candidate, "utf8").includes(TODO_MARKER)
              );
            } catch {
              return false;
            }
          }),
        { timeout: 30_000, intervals: [100, 250, 500] },
      )
      .toHaveLength(1);
    const downloadedArtifact = readdirSync(downloadDirectory).find((filename) => {
      if (downloadsBefore.has(filename) || filename.endsWith(".crdownload")) return false;
      try {
        return readFileSync(path.join(downloadDirectory, filename), "utf8").includes(
          TODO_MARKER,
        );
      } catch {
        return false;
      }
    });
    expect(downloadedArtifact).toMatch(/^echodesk-artifact-[0-9a-f]{12}$/);
    expect(
      statSync(path.join(downloadDirectory, downloadedArtifact!)).mode & 0o777,
    ).toBe(0o600);
    expect(
      readdirSync(downloadDirectory).filter(
        (filename) => !downloadsBefore.has(filename) && filename.endsWith(".crdownload"),
      ),
    ).toEqual([]);
    await expect(artifactDownloadButton).toHaveAttribute("aria-busy", "false");

    const runs = await api<JsonMap[]>(
      working.win,
      `/workflows/runs?meeting_id=${meetingId}&todo_id=${todoId}&limit=20`,
    );
    const succeededRun = runs.find((run) => run.state === "succeeded");
    expect(succeededRun).toBeDefined();
    expect((succeededRun!.input as JsonMap).retry_of_run_id).toBe(failedRunId);
    const meetingArtifacts = await api<JsonMap[]>(
      working.win,
      `/meetings/${meetingId}/artifacts`,
    );
    expect(meetingArtifacts).toHaveLength(1);
    const todoArtifactId = String(meetingArtifacts[0].artifact_id);
    expect(new URL(productDownloadResponse.url()).pathname).toBe(
      `/artifacts/${todoArtifactId}/download`,
    );

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
      path: path.join(SCREENSHOT_DIR, "02-installed-workflows-complete.png"),
      fullPage: true,
    });
    await closeInstalled(working.app);

    first = await launchInstalled(180, modelConfig);
    row = await selectMeeting(first.win, meetingId, todoId);
    await expect(row).toHaveAttribute("data-todo-status", "done", { timeout: 30_000 });
    await expect(first.win.locator(`[data-task-id="${agentTaskId}"]`)).toContainText("已完成");
    await expect(first.win.locator(`[data-task-id="${cancelTaskId}"]`)).toContainText("已取消");
    await expect(first.win.locator(`[data-task-id="${timeoutTaskId}"]`)).toContainText("已超时");
    await first.win.screenshot({
      path: path.join(SCREENSHOT_DIR, "03-installed-restart-restored.png"),
      fullPage: true,
    });
    await closeInstalled(first.app);
    await stopRealAgentServices(agentServices);
  });
});
