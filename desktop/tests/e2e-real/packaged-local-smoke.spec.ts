import { expect, test, _electron as electron } from "@playwright/test";
import {
  accessSync,
  constants,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
} from "node:fs";
import { execFileSync } from "node:child_process";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../../..");
const EXPECTED_VERSION = String(
  process.env.ECHODESK_EXPECTED_VERSION ||
    JSON.parse(
      readFileSync(path.join(REPO_ROOT, "desktop/package.json"), "utf8"),
    ).version ||
    "",
).trim();
if (!EXPECTED_VERSION) throw new Error("desktop package version is missing");
const APP_BIN =
  process.env.ECHODESK_APP_BIN ??
  path.join(
    REPO_ROOT,
    "desktop/release/mac-arm64/EchoDesk.app/Contents/MacOS/EchoDesk",
  );
const EXPECTED_BACKEND_BIN =
  process.env.ECHODESK_EXPECTED_BACKEND_BIN ??
  path.resolve(path.dirname(APP_BIN), "../Resources/backend/echodesk-backend");
const PORT = Number(process.env.ECHODESK_SMOKE_PORT ?? "18769");
const TEST_ROOT =
  process.env.ECHODESK_SMOKE_ROOT ??
  path.join(os.tmpdir(), "echodesk-packaged-local-smoke");
const ISOLATED_HOME =
  process.env.ECHODESK_SMOKE_HOME ?? path.join(TEST_ROOT, "home");

function isolatedEnvironment(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    HOME: ISOLATED_HOME,
    ECHO_BACKEND_PORT: String(PORT),
    ECHO_BACKEND_BIND_HOST: "127.0.0.1",
    ECHO_RUNTIME_MODE: "diagnostic",
    ECHO_PRINCIPAL_MODE: "local",
    ECHODESK_DISABLE_AUTO_UPDATE_DOWNLOAD: "1",
    WORKSPACE_SCAN_ON_STARTUP: "false",
    DIARIZER_ENABLED: "false",
    TTS_ENABLED: "false",
    AGENT_OS_ENABLED: "false",
  };
  for (const name of [
    "ECHO_PYTHON",
    "ECHO_BACKEND_CWD",
    "ECHO_ALLOW_PACKAGED_SOURCE_BACKEND",
    "ECHO_USER_DIR",
    "DB_PATH",
    "STORAGE_DIR",
    "RAG_INDEX_DIR",
    "SKILL_EXECUTOR_BUILD_DIR",
    "PYTHONPATH",
    "ELECTRON_DEV",
    "VITE_DEV_URL",
    "ECHO_PUBLIC_DEMO",
    "ECHO_SPAWN_BACKEND",
  ]) {
    delete env[name];
  }
  return env;
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

async function portOpen(): Promise<boolean> {
  return await new Promise((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port: PORT });
    const finish = (value: boolean) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(value);
    };
    socket.setTimeout(300);
    socket.once("connect", () => finish(true));
    socket.once("timeout", () => finish(false));
    socket.once("error", () => finish(false));
  });
}

test("mounted packaged app uses only bundled backend and persists owner-scoped data", async () => {
  test.setTimeout(180_000);
  expect(existsSync(APP_BIN), `packaged app missing: ${APP_BIN}`).toBe(true);
  expect(
    existsSync(EXPECTED_BACKEND_BIN),
    `bundled backend missing: ${EXPECTED_BACKEND_BIN}`,
  ).toBe(true);
  expect(() => accessSync(EXPECTED_BACKEND_BIN, constants.X_OK)).not.toThrow();
  if (process.env.ECHODESK_REQUIRE_MOUNTED_DMG === "1") {
    const mountRoot = process.env.ECHODESK_DMG_MOUNT;
    expect(
      mountRoot,
      "ECHODESK_DMG_MOUNT must identify the read-only mount",
    ).toBeTruthy();
    expect(
      path
        .resolve(APP_BIN)
        .startsWith(`${path.resolve(mountRoot ?? "")}${path.sep}`),
    ).toBe(true);
  }
  await expect.poll(portOpen, { timeout: 5_000 }).toBe(false);
  rmSync(TEST_ROOT, { recursive: true, force: true });
  mkdirSync(ISOLATED_HOME, { recursive: true });

  const launch = () =>
    electron.launch({
      executablePath: APP_BIN,
      cwd: path.dirname(APP_BIN),
      args: [`--user-data-dir=${path.join(TEST_ROOT, "electron")}`],
      env: isolatedEnvironment(),
      timeout: 60_000,
    });

  let app = await launch();
  try {
    const appVersion = await app.evaluate(async ({ app }) => app.getVersion());
    expect(appVersion).toBe(EXPECTED_VERSION);
    const win = await app.firstWindow({ timeout: 60_000 });
    await win.waitForLoadState("domcontentloaded");
    expect(
      await win.evaluate(() => ({
        origin: window.location.origin,
        protocol: window.location.protocol,
        pathname: window.location.pathname,
      })),
    ).toEqual({
      origin: "echodesk://app",
      protocol: "echodesk:",
      pathname: "/index.html",
    });
    await expect(
      win.getByText("EchoDesk", { exact: true }).first(),
    ).toBeVisible({ timeout: 30_000 });
    await expect
      .poll(() =>
        app.evaluate(
          ({ BrowserWindow }) =>
            !BrowserWindow.getAllWindows()[0]?.webContents.isLoadingMainFrame(),
        ),
      )
      .toBe(true);
    await win.evaluate(() =>
      localStorage.setItem("echodesk.onboarding.completed", "1"),
    );
    const previousDocumentStartedAt = await win.evaluate(
      () => performance.timeOrigin,
    );
    await win.reload({ waitUntil: "domcontentloaded" });
    await expect
      .poll(() => win.evaluate(() => performance.timeOrigin))
      .not.toBe(previousDocumentStartedAt);
    await expect(
      win.getByText("EchoDesk", { exact: true }).first(),
    ).toBeVisible({
      timeout: 30_000,
    });
    await expect.poll(portOpen, { timeout: 60_000 }).toBe(true);
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

    await win.getByTestId("open-settings").click();
    const settingsDialog = win.getByRole("dialog", { name: "设置" });
    await expect(settingsDialog).toBeVisible({ timeout: 15_000 });
    await expect(settingsDialog.getByText("移动端连接")).toHaveCount(0);
    await win.keyboard.press("Escape");
    const command = win.locator("textarea[placeholder*='生成']");
    const smokeCommand = `${EXPECTED_VERSION} 安装态点击验证`;
    await command.fill(smokeCommand);
    await expect(command).toHaveValue(smokeCommand);

    const result = await win.evaluate(async () => {
      const base = await window.echo?.getBackendHost?.();
      if (!base) throw new Error("backend host unavailable");
      const bootstrap = await fetch(`${base}/bootstrap`).then((response) =>
        response.json(),
      );
      const health = await fetch(`${base}/healthz/full`).then((response) =>
        response.json(),
      );
      const meetingId = "packaged-smoke-meeting";
      const started = await fetch(`${base}/meetings/${meetingId}/start`, {
        method: "POST",
      });
      const injected = await fetch(
        `${base}/meetings/${meetingId}/inject_segment`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            text: "packaged smoke durable segment",
            start_ms: 0,
            end_ms: 1_000,
          }),
        },
      );
      const meetings = await fetch(`${base}/meetings`).then((response) =>
        response.json(),
      );
      return {
        base,
        bootstrap,
        health,
        startedStatus: started.status,
        injectedStatus: injected.status,
        meetings,
      };
    });

    expect(result.base).toBe(`http://127.0.0.1:${PORT}`);
    expect(result.bootstrap.backend_version).toBe(appVersion);
    expect(result.bootstrap.http_url).toBe(`http://127.0.0.1:${PORT}`);
    expect(result.bootstrap.ws_url).toBe(`ws://127.0.0.1:${PORT}/ws/echo`);
    expect(result.bootstrap.capabilities.workflow_kernel).toBe("dispatcher-v1");
    expect(result.health.backend.port).toBe(PORT);
    expect(result.startedStatus).toBe(200);
    expect(result.injectedStatus).toBe(200);
    expect(
      result.meetings.some(
        (meeting: { meeting_id?: string }) =>
          meeting.meeting_id === "packaged-smoke-meeting",
      ),
    ).toBe(true);
  } finally {
    await app.close();
    await expect.poll(portOpen, { timeout: 30_000 }).toBe(false);
  }

  app = await launch();
  try {
    const win = await app.firstWindow({ timeout: 60_000 });
    await win.waitForLoadState("domcontentloaded");
    await expect(win.locator(".app-connection-status")).toHaveText("已连接", {
      timeout: 30_000,
    });
    const persisted = await win.evaluate(async () => {
      const base = await window.echo?.getBackendHost?.();
      if (!base) throw new Error("backend host unavailable after restart");
      return await fetch(`${base}/meetings`).then((response) =>
        response.json(),
      );
    });
    expect(
      persisted.some(
        (meeting: { meeting_id?: string }) =>
          meeting.meeting_id === "packaged-smoke-meeting",
      ),
    ).toBe(true);
    expect(
      existsSync(path.join(ISOLATED_HOME, ".echodesk", "echodesk.db")),
    ).toBe(true);
  } finally {
    await app.close();
    await expect.poll(portOpen, { timeout: 30_000 }).toBe(false);
  }
});
