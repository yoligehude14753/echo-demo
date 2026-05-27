/* eslint-disable @typescript-eslint/no-var-requires */
const { app, BrowserWindow, shell, ipcMain, systemPreferences } = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const path = require("node:path");
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");

const IS_DEV = !!process.env.ELECTRON_DEV;
const VITE_URL = process.env.VITE_DEV_URL || "http://localhost:5173";
const BACKEND_PORT = parseInt(process.env.ECHO_BACKEND_PORT || "8769", 10);
const BACKEND_HOST = `http://127.0.0.1:${BACKEND_PORT}`;

// 产品独立性硬约束：双击 .app 必须自己起 backend。
// dev 期想自己 uvicorn 调试的开发者，通过 ECHO_SPAWN_BACKEND=0 显式禁用。
const SPAWN_BACKEND = process.env.ECHO_SPAWN_BACKEND !== "0";

// 注意：dev 模式下 macOS Dock / Cmd+Tab 显示的进程名依赖 brand-dev-electron.cjs 补丁后的
// node_modules/electron/dist/Electron.app/Info.plist 的 CFBundleName。
// electron-builder 打包后从 productName=EchoDesk 来。app.setName() 只影响 userData 路径，不改 Dock 名。

// ---------- 健康监控参数 ----------
const HEALTH_INTERVAL_MS = 2000;
const HEALTH_TIMEOUT_MS = 1500;
// 单次抖动（网络 GC、CPU 抢占）很常见；3 次连续失败 ≈ 6s 才判死，避免误杀
const HEALTH_FAIL_THRESHOLD = 3;
// 第 N 次重启等待 RESTART_BACKOFFS_MS[N-1]，超出数组长度后判定 degraded
const RESTART_BACKOFFS_MS = [1000, 3000, 10000];
// uvicorn 冷启动可能要 5-15s（import torch / 加载模型），给 30s 余量
const STARTUP_TIMEOUT_MS = 30_000;
// SIGTERM 后给 uvicorn 3s 跑 lifespan shutdown，超时强制 SIGKILL
const SIGKILL_GRACE_MS = 3000;

// ---------- 运行时状态 ----------
let backendProc = null;
let mainWindow = null;
let healthTimer = null;
let externalHealthTimer = null;
let healthStartedAt = 0;
let healthFailures = 0;
let backendWasReady = false;
let restartAttempts = 0;
let shuttingDown = false;
let quittingForReal = false;
let externalMode = false;
let pythonResolved = null; // { python: string|null, searched: string[] }
// renderer 启动慢于 backend：early status 缓存到 lastStatus，等 did-finish-load 后 replay
let lastStatus = null;
let rendererReady = false;

// 主进程未捕获异常不弹 fatal dialog；UI 应该自己感知 backend 状态
process.on("uncaughtException", (err) => {
  console.error("[main] uncaught exception:", err);
});
process.on("unhandledRejection", (reason) => {
  console.error("[main] unhandled rejection:", reason);
});

function log(msg) {
  console.log(msg);
}

function projectRoot() {
  // dev: desktop/electron/main.cjs → desktop/.. = echo-demo repo root
  return path.resolve(__dirname, "..", "..");
}

// backend 工作目录解析。prod (asar) 下 __dirname 在 asar 虚拟路径，
// 不能作 child_process.spawn 的 cwd（uvicorn 启动期会 chdir 失败）。
// 候选顺序跟 pythonCandidates 对齐，找到第一个真实存在的目录即用。
function resolveBackendCwd() {
  const cands = [
    process.env.ECHO_BACKEND_CWD,
    path.join(os.homedir(), ".echodesk", "source", "backend"),
    path.join(projectRoot(), "backend"),
  ].filter(Boolean);
  for (const c of cands) {
    try {
      if (fs.existsSync(path.join(c, "app", "main.py"))) return c;
    } catch {
      /* ignore */
    }
  }
  // 全找不到时退化用第一个（spawn 会失败，让上层走 handleBackendDeath）
  return cands[1];
}

// ---------- Python 解析（P1.6） ----------

// 候选顺序：env > 用户安装位置 (P1.7) > dev 仓库 venv > 系统 python3 > PATH
function pythonCandidates() {
  const cands = [];
  if (process.env.ECHO_PYTHON) cands.push(process.env.ECHO_PYTHON);
  cands.push(
    path.join(os.homedir(), ".echodesk", "source", "backend", ".venv", "bin", "python"),
  );
  cands.push(path.join(projectRoot(), "backend", ".venv", "bin", "python"));
  cands.push("/usr/bin/python3");
  cands.push("python3");
  return cands;
}

// 每个候选 fs.existsSync + spawnSync --version 验证；返回第一个能跑的
function resolvePython() {
  const searched = [];
  for (const c of pythonCandidates()) {
    searched.push(c);
    const isAbs = c.startsWith("/");
    if (isAbs) {
      try {
        if (!fs.existsSync(c)) continue;
      } catch {
        continue;
      }
    }
    try {
      const r = spawnSync(c, ["--version"], { timeout: 3000 });
      if (r.error) continue;
      // python --version 走 stdout（3.4+）或 stderr（旧版）；任一含 "Python" 即可
      const out = `${r.stdout || ""}${r.stderr || ""}`;
      if (r.status === 0 && /Python/i.test(out)) {
        log(`[backend] python resolved: ${c} (${out.trim()})`);
        return { python: c, searched };
      }
    } catch {
      /* 继续下一个候选 */
    }
  }
  return { python: null, searched };
}

// ---------- 端口探测 ----------

// 只看 LISTEN 状态的 pid；已建立的 client 连接不算占用 listener。
// 例如 main.cjs 自己测过 lsof -ti tcp:8769（不加 -sTCP:LISTEN）会把 client side 也返回，
// 导致误判端口被占。
function isPortListening(port) {
  try {
    const r = spawnSync("lsof", ["-ti", `tcp:${port}`, "-sTCP:LISTEN"], { timeout: 2000 });
    const out = (r.stdout?.toString() || "").trim();
    return out.length > 0;
  } catch {
    return false;
  }
}

// ---------- IPC 状态广播 ----------

function emitStatus(payload) {
  lastStatus = payload;
  log(`[backend] status -> ${JSON.stringify(payload)}`);
  if (mainWindow && !mainWindow.isDestroyed() && rendererReady) {
    try {
      mainWindow.webContents.send("backend:status", payload);
    } catch (e) {
      log(`[backend] emit failed: ${e.message}`);
    }
  }
}

// ---------- 健康检查 ----------

function healthcheckOnce() {
  return new Promise((resolve) => {
    let settled = false;
    const done = (ok) => {
      if (!settled) {
        settled = true;
        resolve(ok);
      }
    };
    const req = http.get(`${BACKEND_HOST}/healthz`, { timeout: HEALTH_TIMEOUT_MS }, (res) => {
      const ok = res.statusCode === 200;
      res.resume();
      done(ok);
    });
    req.on("error", () => done(false));
    req.on("timeout", () => {
      req.destroy();
      done(false);
    });
  });
}

function startHealthWatcher() {
  if (healthTimer) return;
  healthFailures = 0;
  backendWasReady = false;
  healthStartedAt = Date.now();
  healthTimer = setInterval(async () => {
    if (shuttingDown || !healthTimer) return;
    const ok = await healthcheckOnce();
    if (ok) {
      if (!backendWasReady) {
        backendWasReady = true;
        restartAttempts = 0; // 一次完整 ready → 清空 backoff 计数器
        emitStatus({ state: "ready", port: BACKEND_PORT });
      }
      healthFailures = 0;
      return;
    }
    if (!backendWasReady) {
      // 启动期：uvicorn 还没绑 socket 是正常的，不计入失败计数；但要给一个硬超时
      if (Date.now() - healthStartedAt > STARTUP_TIMEOUT_MS) {
        log("[backend] startup timeout (never became ready)");
        handleBackendDeath("startup timeout");
      }
      return;
    }
    healthFailures += 1;
    log(`[backend] healthz fail ${healthFailures}/${HEALTH_FAIL_THRESHOLD}`);
    if (healthFailures >= HEALTH_FAIL_THRESHOLD) {
      healthFailures = 0;
      handleBackendDeath(`healthz failed ${HEALTH_FAIL_THRESHOLD}x`);
    }
  }, HEALTH_INTERVAL_MS);
}

function stopHealthWatcher() {
  if (healthTimer) {
    clearInterval(healthTimer);
    healthTimer = null;
  }
}

function stopExternalHealthWatcher() {
  if (externalHealthTimer) {
    clearInterval(externalHealthTimer);
    externalHealthTimer = null;
  }
}

// External 模式：我们没拥有该 process，不重启；只观察存活情况
function startExternalHealthWatcher() {
  if (externalHealthTimer) return;
  externalHealthTimer = setInterval(async () => {
    if (shuttingDown) return;
    const ok = await healthcheckOnce();
    if (ok) return;
    if (!isPortListening(BACKEND_PORT)) {
      // 外部 backend 进程退出 → 端口已空 → 我们接管
      log("[backend] external backend exited, taking over");
      stopExternalHealthWatcher();
      externalMode = false;
      restartAttempts = 0;
      spawnBackendAndWatch();
      return;
    }
    // 端口还占着但 healthz 失败 → 外部 backend 卡死了，标记 degraded 让 UI 提示
    emitStatus({
      state: "degraded",
      reason: "external backend unhealthy",
      attempts: 0,
      last_error: "healthz failed",
    });
  }, HEALTH_INTERVAL_MS);
  // 立即尝试一次 healthz，让 ready 尽早发出
  setImmediate(async () => {
    const ok = await healthcheckOnce();
    if (ok) emitStatus({ state: "ready", port: BACKEND_PORT });
  });
}

// ---------- 进程生命周期 ----------

function killBackendProc() {
  if (!backendProc || backendProc.killed) {
    backendProc = null;
    return;
  }
  const proc = backendProc;
  backendProc = null;
  try {
    proc.kill("SIGTERM");
  } catch {
    /* ignore */
  }
  // 异步 SIGKILL 兜底，避免 uvicorn lifespan shutdown 卡住
  setTimeout(() => {
    if (proc && proc.exitCode === null) {
      try {
        proc.kill("SIGKILL");
      } catch {
        /* ignore */
      }
    }
  }, SIGKILL_GRACE_MS);
}

async function handleBackendDeath(reason) {
  if (shuttingDown) return;
  stopHealthWatcher();
  killBackendProc();

  if (restartAttempts >= RESTART_BACKOFFS_MS.length) {
    // 3 次重启都没救活 → 进入 degraded，停止自动循环，等 renderer 手动触发
    emitStatus({
      state: "degraded",
      reason: reason || "repeated backend failures",
      attempts: RESTART_BACKOFFS_MS.length,
      last_error: reason || "unknown",
    });
    return;
  }

  const backoff = RESTART_BACKOFFS_MS[restartAttempts];
  restartAttempts += 1;
  emitStatus({
    state: "restarting",
    attempt: restartAttempts,
    backoff_ms: backoff,
    reason,
  });
  setTimeout(() => {
    if (shuttingDown) return;
    spawnBackendAndWatch();
  }, backoff);
}

function spawnBackendAndWatch() {
  if (shuttingDown) return;

  // 端口已经被外部 backend 占着（dev 期 cursor 已经跑了 uvicorn）→ 不要 spawn 第二份
  if (isPortListening(BACKEND_PORT)) {
    externalMode = true;
    log(`[backend] port ${BACKEND_PORT} already in use, assuming external backend`);
    emitStatus({ state: "external", port: BACKEND_PORT });
    startExternalHealthWatcher();
    return;
  }

  // resolvePython 在 startBackend 已经跑过；这里防御性兜底
  if (!pythonResolved || !pythonResolved.python) {
    pythonResolved = resolvePython();
  }
  if (!pythonResolved.python) {
    emitStatus({
      state: "python-not-found",
      searched: pythonResolved.searched,
      help_url: "docs/INSTALL.md",
    });
    return;
  }

  const cwd = resolveBackendCwd();
  if (!cwd || !fs.existsSync(path.join(cwd, "app", "main.py"))) {
    emitStatus({
      state: "backend-source-not-found",
      searched: [
        process.env.ECHO_BACKEND_CWD,
        path.join(os.homedir(), ".echodesk", "source", "backend"),
        path.join(projectRoot(), "backend"),
      ].filter(Boolean),
      help_url: "docs/INSTALL.md",
    });
    return;
  }
  emitStatus({ state: "starting" });
  log(`[backend] spawn ${pythonResolved.python} -m uvicorn (cwd=${cwd})`);

  try {
    backendProc = spawn(
      pythonResolved.python,
      [
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        String(BACKEND_PORT),
        "--log-level",
        "info",
      ],
      {
        cwd,
        env: {
          ...process.env,
          // localhost 流量走代理会导致 uvicorn 自己 GET healthz 都失败
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
  } catch (e) {
    log(`[backend] spawn threw: ${e.message}`);
    backendProc = null;
    handleBackendDeath(`spawn threw: ${e.message}`);
    return;
  }

  // ENOENT / EACCES 走 'error' 事件而不是 throw；不挂监听 electron 会判 fatal
  backendProc.on("error", (err) => {
    log(`[backend] spawn error: ${err.message}`);
  });
  backendProc.stdout?.on("data", (b) =>
    process.stdout.write(`[backend] ${b.toString()}`),
  );
  backendProc.stderr?.on("data", (b) =>
    process.stderr.write(`[backend] ${b.toString()}`),
  );
  backendProc.on("exit", (code, signal) => {
    const wasOurs = backendProc !== null; // killBackendProc 会先置 null
    log(`[backend] child exited code=${code} signal=${signal} ours=${wasOurs}`);
    if (wasOurs && !shuttingDown) {
      // 我们没主动 kill，child 自己崩了 → 让 health watcher 触发死亡路径
      // 立即标记一次失败而非等 watcher tick，加快感知
      backendProc = null;
      handleBackendDeath(`child exited code=${code}`);
    }
  });

  startHealthWatcher();
}

// ---------- 启动 ----------

function startBackend() {
  if (!SPAWN_BACKEND) {
    log(`[backend] spawn disabled (ECHO_SPAWN_BACKEND=0), assuming external ${BACKEND_HOST}`);
    externalMode = true;
    emitStatus({ state: "external", port: BACKEND_PORT });
    startExternalHealthWatcher();
    return;
  }

  // P1.6: 启动第一步验证 Python 存在；找不到就直接 emit python-not-found
  // 不 spawn uvicorn 是为了避免 ENOENT 太晚才暴露
  pythonResolved = resolvePython();
  if (!pythonResolved.python) {
    log(`[backend] python not found. searched=${JSON.stringify(pythonResolved.searched)}`);
    emitStatus({
      state: "python-not-found",
      searched: pythonResolved.searched,
      help_url: "docs/INSTALL.md",
    });
    return;
  }
  spawnBackendAndWatch();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    title: IS_DEV ? "EchoDesk (dev)" : "EchoDesk",
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

  // renderer 启动早于 backend ready；did-finish-load 之后才能 send IPC
  // 这里 replay 最近一条 status，让 renderer 立刻拿到当前状态
  mainWindow.webContents.on("did-finish-load", () => {
    rendererReady = true;
    if (lastStatus) {
      try {
        mainWindow.webContents.send("backend:status", lastStatus);
      } catch (e) {
        log(`[backend] replay failed: ${e.message}`);
      }
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
    rendererReady = false;
  });

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

// ---------- IPC handlers ----------

ipcMain.handle("echo:backend-host", () => BACKEND_HOST);

// ---------- 麦克风权限 IPC（P3.5） ----------
//
// 浏览器 navigator.permissions.query 在 Electron 下 granted/denied/prompt 已经够用，
// 但有两件事它做不到：
// 1) 区分 macOS 的 "not-determined"（用户从未被问过）vs "denied"（曾点过拒绝）
// 2) 当 denied 时直接打开系统设置-隐私-麦克风（用户只能口头被引导，体验差）
//
// 这两个 IPC：
// - mic:status 用 systemPreferences.getMediaAccessStatus("microphone")
//   返回 'not-determined'|'granted'|'denied'|'restricted'|'unknown'（非 mac 直接 unknown）
// - mic:open-system-prefs shell.openExternal 一键打开 macOS 隐私设置-麦克风分页

ipcMain.handle("mic:status", () => {
  if (process.platform !== "darwin") return "unknown";
  try {
    return systemPreferences.getMediaAccessStatus("microphone");
  } catch (e) {
    log("[mic] getMediaAccessStatus failed:", e?.message ?? e);
    return "unknown";
  }
});

ipcMain.handle("mic:request", async () => {
  if (process.platform !== "darwin") return false;
  try {
    return await systemPreferences.askForMediaAccess("microphone");
  } catch (e) {
    log("[mic] askForMediaAccess failed:", e?.message ?? e);
    return false;
  }
});

ipcMain.handle("mic:open-system-prefs", async () => {
  if (process.platform !== "darwin") {
    return { ok: false, reason: "non-darwin" };
  }
  // x-apple.systempreferences URL scheme 直接定位到「隐私与安全 → 麦克风」
  // 兼容 macOS 13+；旧版本会落回 System Preferences 顶层（仍可接受）
  try {
    await shell.openExternal(
      "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    );
    return { ok: true };
  } catch (e) {
    log("[mic] openExternal failed:", e?.message ?? e);
    return { ok: false, reason: String(e?.message ?? e) };
  }
});

// 让 renderer 在 degraded UI 上按钮触发一次干净重启：清 backoff 计数 + 重新 spawn
ipcMain.handle("backend:manual-restart", async () => {
  log("[backend] manual restart requested");
  restartAttempts = 0;
  externalMode = false;
  stopHealthWatcher();
  stopExternalHealthWatcher();
  killBackendProc();
  // 给端口一点时间释放（SIGTERM → close socket）
  setTimeout(() => {
    if (shuttingDown) return;
    if (!pythonResolved || !pythonResolved.python) {
      pythonResolved = resolvePython();
    }
    if (!pythonResolved.python) {
      emitStatus({
        state: "python-not-found",
        searched: pythonResolved.searched,
        help_url: "docs/INSTALL.md",
      });
      return;
    }
    spawnBackendAndWatch();
  }, 500);
  return { ok: true };
});

// ---------- app 生命周期 ----------

app.whenReady().then(() => {
  // 主窗口先起，让用户看到 UI；backend 状态由 renderer 自己渲染（degraded UI 等）
  createWindow();
  startBackend();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

// 优雅退出：先通知 renderer（避免它弹"断开"红条），再 SIGTERM child，超时 SIGKILL，
// 最后真正 app.quit()。preventDefault 第一次拦下 quit；等子进程清干净再放行。
app.on("before-quit", (event) => {
  if (quittingForReal) return;
  shuttingDown = true;
  emitStatus({ state: "shutting-down" });
  stopHealthWatcher();
  stopExternalHealthWatcher();

  if (!backendProc || backendProc.killed) {
    quittingForReal = true;
    return;
  }

  event.preventDefault();
  const proc = backendProc;
  backendProc = null;
  log("[backend] SIGTERM child (graceful)");
  try {
    proc.kill("SIGTERM");
  } catch (e) {
    log(`[backend] SIGTERM failed: ${e.message}`);
  }

  let finished = false;
  const finalize = () => {
    if (finished) return;
    finished = true;
    quittingForReal = true;
    app.quit();
  };
  const t = setTimeout(() => {
    if (proc && proc.exitCode === null) {
      log("[backend] grace expired, SIGKILL");
      try {
        proc.kill("SIGKILL");
      } catch {
        /* ignore */
      }
    }
    finalize();
  }, SIGKILL_GRACE_MS);
  proc.once("exit", () => {
    clearTimeout(t);
    finalize();
  });
});
