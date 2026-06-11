/* eslint-disable @typescript-eslint/no-var-requires */
const {
  app,
  BrowserWindow,
  dialog,
  shell,
  ipcMain,
  systemPreferences,
} = require("electron");
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

// 候选顺序：env > 用户安装位置 (P1.7) > dev 仓库 venv > 系统 python > PATH
// 跨平台：Windows venv 在 Scripts\python.exe，Unix 在 bin/python。
function venvPython(root) {
  return process.platform === "win32"
    ? path.join(root, ".venv", "Scripts", "python.exe")
    : path.join(root, ".venv", "bin", "python");
}
function pythonCandidates() {
  const cands = [];
  if (process.env.ECHO_PYTHON) cands.push(process.env.ECHO_PYTHON);
  cands.push(venvPython(path.join(os.homedir(), ".echodesk", "source", "backend")));
  cands.push(venvPython(path.join(projectRoot(), "backend")));
  if (process.platform === "win32") {
    cands.push("python.exe");
    cands.push("python");
  } else {
    cands.push("/usr/bin/python3");
    cands.push("python3");
  }
  return cands;
}

// 打包后端二进制（PyInstaller onedir）：装机后随 .app/.exe 一起分发，无需系统 Python。
// 找到即优先用它；找不到回退系统 Python + 源码（dev / 旧装机方式）。
function resolveBundledBackend() {
  const exe = process.platform === "win32" ? "echodesk-backend.exe" : "echodesk-backend";
  const cands = [];
  if (process.env.ECHO_BACKEND_BIN) cands.push(process.env.ECHO_BACKEND_BIN);
  // 打包态：Electron extraResources → resources/backend-dist/echodesk-backend/<exe>
  if (process.resourcesPath) {
    cands.push(path.join(process.resourcesPath, "backend-dist", "echodesk-backend", exe));
  }
  // dev 态：仓库 backend/dist/echodesk-backend/<exe>（本地 pyinstaller 产物）
  cands.push(path.join(projectRoot(), "backend", "dist", "echodesk-backend", exe));
  for (const c of cands) {
    try {
      if (c && fs.existsSync(c)) return c;
    } catch {
      /* ignore */
    }
  }
  return null;
}

// 每个候选 fs.existsSync + spawnSync --version 验证；返回第一个能跑的
function resolvePython() {
  const searched = [];
  for (const c of pythonCandidates()) {
    searched.push(c);
    const isAbs = path.isAbsolute(c);
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

  // 决定启动命令：优先打包二进制（无需系统 Python），否则回退系统 Python + 源码。
  let spawnCmd;
  let spawnArgs;
  let spawnCwd;
  const bundled = resolveBundledBackend();
  if (bundled) {
    spawnCmd = bundled;
    spawnArgs = [];
    spawnCwd = path.dirname(bundled);
    log(`[backend] using bundled binary: ${bundled}`);
  } else {
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
    spawnCmd = pythonResolved.python;
    spawnArgs = [
      "-m",
      "uvicorn",
      "app.main:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(BACKEND_PORT),
      "--log-level",
      "info",
    ];
    spawnCwd = cwd;
    log(`[backend] spawn ${spawnCmd} -m uvicorn (cwd=${cwd})`);
  }
  emitStatus({ state: "starting" });

  const childEnv = {
    ...process.env,
    // 打包二进制通过这些 env 读端口/host/日志级别（run_server.py）；
    // python 路径走 --port 参数，这些 env 无害。
    ECHO_BACKEND_HOST: "127.0.0.1",
    ECHO_BACKEND_PORT: String(BACKEND_PORT),
    ECHO_LOG_LEVEL: "info",
    // localhost 流量走代理会导致 uvicorn 自己 GET healthz 都失败
    HTTP_PROXY: "",
    HTTPS_PROXY: "",
    ALL_PROXY: "",
    http_proxy: "",
    https_proxy: "",
    all_proxy: "",
  };
  // 冻结二进制自带 Python + dylib：若继承用户环境里的 DYLD_*/PYTHON* 变量
  // （不少用户为 CUDA/homebrew 设过 DYLD_LIBRARY_PATH），内置解释器/动态库会错乱
  // 甚至 SIGSEGV。spawn 前一律剥离，确保用干净环境（E2E 实测发现的崩溃根因）。
  for (const k of [
    "DYLD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_FRAMEWORK_PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "__PYVENV_LAUNCHER__",
  ]) {
    delete childEnv[k];
  }

  try {
    backendProc = spawn(spawnCmd, spawnArgs, {
      cwd: spawnCwd,
      env: childEnv,
      stdio: ["ignore", "pipe", "pipe"],
    });
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
  // 持久化 backend stdout / stderr 到 ~/.echodesk/logs/runtime.log（最大 8MB 滚动）。
  // 用户 2026-05-28 反馈"生成 HTML 报 400 看不到根因"——Electron 转发到
  // process.stdout 但 macOS 上没人看，diag 时拿不到任何后端日志。
  // 现在写到文件，下次同样的 bug 直接 `tail -200 ~/.echodesk/logs/runtime.log` 就能定位。
  const runtimeLogPath = path.join(
    os.homedir(),
    ".echodesk",
    "logs",
    "runtime.log",
  );
  try {
    fs.mkdirSync(path.dirname(runtimeLogPath), { recursive: true });
    // 启动时若已 >8MB，简单 truncate（避免无限增长）
    try {
      const st = fs.statSync(runtimeLogPath);
      if (st.size > 8 * 1024 * 1024) fs.truncateSync(runtimeLogPath, 0);
    } catch {
      /* 不存在 → 接下来 appendFile 自动创建 */
    }
  } catch (e) {
    log(`[backend] runtime log dir prepare failed: ${e.message}`);
  }
  const writeRuntime = (chunk) => {
    try {
      fs.appendFile(runtimeLogPath, chunk, () => undefined);
    } catch {
      /* 写 log 失败时不能影响主链路 */
    }
  };
  backendProc.stdout?.on("data", (b) => {
    const s = b.toString();
    process.stdout.write(`[backend] ${s}`);
    writeRuntime(s);
  });
  backendProc.stderr?.on("data", (b) => {
    const s = b.toString();
    process.stderr.write(`[backend] ${s}`);
    writeRuntime(s);
  });
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

// P4.1 M4：把 backend 落盘的产物文件交给系统默认应用打开（如 .pptx 走 Keynote）。
// Electron 与 backend 跑在同一台 mac，artifact.file_path 是绝对路径，可直接传给 shell.openPath。
// 安全：只接受字符串绝对路径；不在主进程做任何 file system 修改，仅委派给 OS。
// 失败：shell.openPath 把错误字符串作为 resolve 值返回（非 reject），所以这里手动 throw 让 renderer catch。
ipcMain.handle("echo:open-artifact-in-system", async (_event, filePath) => {
  if (typeof filePath !== "string" || !filePath.trim()) {
    throw new Error("filePath required");
  }
  try {
    const err = await shell.openPath(filePath);
    if (err) {
      // err 不为空字符串 = 系统层失败（文件不存在 / 没有匹配 app / 权限不足）
      throw new Error(err);
    }
  } catch (e) {
    log(`[artifact] openPath failed (${filePath}): ${e?.message ?? e}`);
    throw e instanceof Error ? e : new Error(String(e));
  }
});

// P4-fix-rag-chat（2026-05-28）：让 SettingsPanel"工作区目录"section 能用系统
// dialog 选目录，再 POST /workspace/add-dir 持久化 + 触发 scan。
//
// 安全：dialog 由 electron 主进程出，用户必须看到/点确认；返回 null 时表示
// 用户取消，不写任何配置。失败 reject 让 renderer message.error。
ipcMain.handle("workspace:pick-directory", async (_event, opts = {}) => {
  const win = BrowserWindow.getFocusedWindow();
  const defaultPath = typeof opts.defaultPath === "string" ? opts.defaultPath : os.homedir();
  try {
    const r = await dialog.showOpenDialog(win || undefined, {
      title: "选择工作区目录（EchoDesk 会扫描索引整个文件夹）",
      properties: ["openDirectory", "createDirectory"],
      defaultPath,
      message: "支持的文件：PDF / Word / Excel / PPT / Markdown / TXT / HTML / CSV 等",
      buttonLabel: "选中此目录",
    });
    if (r.canceled || r.filePaths.length === 0) return null;
    return r.filePaths[0];
  } catch (e) {
    log(`[workspace] pick-directory failed: ${e?.message ?? e}`);
    throw e instanceof Error ? e : new Error(String(e));
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
