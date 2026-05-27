/* eslint-disable @typescript-eslint/no-var-requires */
const { app, BrowserWindow, shell, ipcMain } = require("electron");
const { spawn } = require("node:child_process");
const path = require("node:path");
const http = require("node:http");
const fs = require("node:fs");

const IS_DEV = !!process.env.ELECTRON_DEV;
const VITE_URL = process.env.VITE_DEV_URL || "http://localhost:5173";
const BACKEND_PORT = parseInt(process.env.ECHO_BACKEND_PORT || "8769", 10);
const BACKEND_HOST = `http://127.0.0.1:${BACKEND_PORT}`;
const SPAWN_BACKEND = process.env.ECHO_SPAWN_BACKEND !== "0";

// 注意：dev 模式下 macOS Dock / Cmd+Tab 显示的进程名固定是 "Electron"（黑紫色 Atom 图标），
// 因为读的是 node_modules/electron/dist/Electron.app/Info.plist 的 CFBundleName。
// 只有 electron-builder 打包后才会显示为 "Echo"。app.setName() 只影响 userData 路径，不改 Dock 名。
// dev 期识别窗口：看窗口内 UI 顶部的 "Echo · echo-demo" brand，或 Cmd+Tab 中的黑紫 Atom 图标。

let backendProc = null;
let mainWindow = null;

function projectRoot() {
  // dev: desktop/electron/main.cjs → desktop/.. = echo-demo
  return path.resolve(__dirname, "..", "..");
}

function resolvePython() {
  if (process.env.ECHO_PYTHON) return process.env.ECHO_PYTHON;
  const candidates = [
    "/Users/yoligehude/Desktop/all/echo/backend/.venv-test/bin/python",
    path.join(projectRoot(), "backend", ".venv", "bin", "python"),
    "/usr/bin/python3",
    "python3",
  ];
  for (const c of candidates) {
    try {
      if (c.startsWith("/") && fs.existsSync(c)) return c;
    } catch {
      /* ignore */
    }
  }
  return "python3";
}

function startBackend() {
  if (!SPAWN_BACKEND) {
    log(`[backend] spawn 已被禁用，假定 ${BACKEND_HOST} 已在运行`);
    return;
  }
  const py = resolvePython();
  const cwd = path.join(projectRoot(), "backend");
  log(`[backend] spawn ${py} -m uvicorn app.main:app --port ${BACKEND_PORT}`);
  log(`[backend] cwd=${cwd}`);

  backendProc = spawn(
    py,
    ["-m", "uvicorn", "app.main:app", "--port", String(BACKEND_PORT), "--log-level", "warning"],
    {
      cwd,
      env: {
        ...process.env,
        HTTP_PROXY: "",
        HTTPS_PROXY: "",
        ALL_PROXY: "",
        http_proxy: "",
        https_proxy: "",
        all_proxy: "",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  backendProc.stdout?.on("data", (b) =>
    process.stdout.write(`[backend] ${b.toString()}`),
  );
  backendProc.stderr?.on("data", (b) =>
    process.stderr.write(`[backend] ${b.toString()}`),
  );
  backendProc.on("exit", (code) => log(`[backend] exited ${code}`));
}

function log(msg) {
  console.log(msg);
}

function waitForBackend(timeoutMs = 30_000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http
        .get(`${BACKEND_HOST}/healthz`, { timeout: 1500 }, (res) => {
          if (res.statusCode === 200) {
            res.resume();
            return resolve();
          }
          res.resume();
          retry();
        })
        .on("error", retry)
        .on("timeout", () => {
          req.destroy();
          retry();
        });
    };
    const retry = () => {
      if (Date.now() - start > timeoutMs) {
        return reject(new Error(`backend ${BACKEND_HOST} timeout`));
      }
      setTimeout(tick, 500);
    };
    tick();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    title: IS_DEV ? "Echo (dev)" : "Echo",
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 600,
    titleBarStyle: "hiddenInset",
    backgroundColor: "#ffffff",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  mainWindow.once("ready-to-show", () => mainWindow.show());

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  if (IS_DEV) {
    mainWindow.loadURL(VITE_URL);
  } else {
    mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

ipcMain.handle("echo:backend-host", () => BACKEND_HOST);

app.whenReady().then(async () => {
  startBackend();
  try {
    await waitForBackend();
    log(`[backend] ready at ${BACKEND_HOST}`);
  } catch (e) {
    log(`[backend] WARN: ${e.message}（继续启动 UI，但接口会失败）`);
  }
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (backendProc && !backendProc.killed) {
    log("[backend] killing child process");
    try {
      backendProc.kill("SIGTERM");
    } catch {
      /* ignore */
    }
  }
});
