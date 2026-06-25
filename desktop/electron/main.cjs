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
const https = require("node:https");
const fs = require("node:fs");
const os = require("node:os");

let autoUpdater = null;
try {
  ({ autoUpdater } = require("electron-updater"));
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = false;
  autoUpdater.allowPrerelease = false;
} catch (e) {
  console.warn("[updates] electron-updater unavailable:", e?.message ?? e);
}

const IS_DEV = !!process.env.ELECTRON_DEV;
const VITE_URL = process.env.VITE_DEV_URL || "http://localhost:5173";
const BACKEND_PORT = parseInt(process.env.ECHO_BACKEND_PORT || "8769", 10);
const LOCAL_BACKEND_HOST = `http://127.0.0.1:${BACKEND_PORT}`;
const PUBLIC_BACKEND_HOST =
  normalizeHttpBase(process.env.ECHO_PUBLIC_BACKEND_BASE) ||
  "https://echodesk.yoliyoli.uk";
const RELEASE_OWNER = "yoligehude14753";
const RELEASE_REPO = "echo-demo";
const RELEASES_URL = `https://github.com/${RELEASE_OWNER}/${RELEASE_REPO}/releases/latest`;
const RELEASE_API_URL = `https://api.github.com/repos/${RELEASE_OWNER}/${RELEASE_REPO}/releases/latest`;
const FORCE_LOCAL_BACKEND = process.env.ECHO_FORCE_LOCAL_BACKEND === "1";
const PUBLIC_DEMO_MODE =
  process.env.ECHO_PUBLIC_DEMO === "1" || (!IS_DEV && !FORCE_LOCAL_BACKEND);
const BACKEND_HOST = PUBLIC_DEMO_MODE ? PUBLIC_BACKEND_HOST : LOCAL_BACKEND_HOST;
const BACKEND_BIND_HOST = process.env.ECHO_BACKEND_BIND_HOST || "0.0.0.0";

// 公开发布包默认走 public backend：key 与模型服务留在服务端，新用户不需要本机 Python。
// 私有/离线部署可以显式 ECHO_FORCE_LOCAL_BACKEND=1 恢复本地 backend spawn。
const SPAWN_BACKEND =
  !PUBLIC_DEMO_MODE && process.env.ECHO_SPAWN_BACKEND !== "0";

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
let lastUpdateStatus = {
  status: "idle",
  currentVersion: app.getVersion(),
  releaseUrl: RELEASES_URL,
};

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

function normalizeHttpBase(raw) {
  const value = String(raw || "").trim().replace(/\/+$/, "");
  if (!value) return null;
  return /^https?:\/\//i.test(value) ? value : `http://${value}`;
}

function normalizeVersion(raw) {
  return String(raw || "").trim().replace(/^v/i, "");
}

function compareVersions(a, b) {
  const aa = normalizeVersion(a).split(".").map((x) => parseInt(x, 10) || 0);
  const bb = normalizeVersion(b).split(".").map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(aa.length, bb.length); i += 1) {
    const av = aa[i] || 0;
    const bv = bb[i] || 0;
    if (av > bv) return 1;
    if (av < bv) return -1;
  }
  return 0;
}

function preferredReleaseAsset(assets) {
  const names = (assets || []).map((asset) => asset.name || "");
  const patterns =
    process.platform === "darwin"
      ? [/arm64\.dmg$/i, /arm64-mac\.zip$/i, /\.dmg$/i]
      : process.platform === "win32"
        ? [/Setup\.[\d.]+\.exe$/i, /\.exe$/i]
        : [/\.AppImage$/i, /\.deb$/i];
  for (const pattern of patterns) {
    const name = names.find((n) => pattern.test(n));
    if (name) return (assets || []).find((asset) => asset.name === name) || null;
  }
  return (assets || [])[0] || null;
}

function fetchJson(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(
      url,
      {
        headers: {
          "Accept": "application/vnd.github+json",
          "User-Agent": `EchoDesk/${app.getVersion()}`,
        },
        timeout: 8000,
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          const body = Buffer.concat(chunks).toString("utf8");
          if (!res.statusCode || res.statusCode < 200 || res.statusCode >= 300) {
            reject(new Error(`HTTP ${res.statusCode}: ${body.slice(0, 200)}`));
            return;
          }
          try {
            resolve(JSON.parse(body));
          } catch (e) {
            reject(e);
          }
        });
      },
    );
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy(new Error("request timeout"));
    });
  });
}

async function fetchLatestReleaseStatus(base = {}) {
  const release = await fetchJson(RELEASE_API_URL);
  const latestVersion = normalizeVersion(release.tag_name || release.name || "");
  const currentVersion = app.getVersion();
  const assets = Array.isArray(release.assets)
    ? release.assets.map((asset) => ({
        name: asset.name,
        size: asset.size,
        url: asset.browser_download_url,
      }))
    : [];
  const preferredAsset = preferredReleaseAsset(assets);
  return {
    ...base,
    status: base.status || "checked",
    currentVersion,
    latestVersion,
    updateAvailable: latestVersion
      ? compareVersions(latestVersion, currentVersion) > 0
      : false,
    releaseName: release.name || release.tag_name || "",
    releaseUrl: release.html_url || RELEASES_URL,
    assetName: preferredAsset?.name || null,
    assetUrl: preferredAsset?.url || null,
    canAutoInstall: !!base.canAutoInstall,
  };
}

function emitUpdateStatus(payload) {
  lastUpdateStatus = {
    ...lastUpdateStatus,
    ...payload,
    currentVersion: app.getVersion(),
    releaseUrl: payload.releaseUrl || lastUpdateStatus.releaseUrl || RELEASES_URL,
  };
  log(`[updates] status -> ${JSON.stringify(lastUpdateStatus)}`);
  if (mainWindow && !mainWindow.isDestroyed() && rendererReady) {
    try {
      mainWindow.webContents.send("updates:status", lastUpdateStatus);
    } catch (e) {
      log(`[updates] emit failed: ${e.message}`);
    }
  }
}

if (autoUpdater) {
  autoUpdater.setFeedURL({
    provider: "github",
    owner: RELEASE_OWNER,
    repo: RELEASE_REPO,
    releaseType: "release",
  });
  autoUpdater.on("checking-for-update", () =>
    emitUpdateStatus({ status: "checking", canAutoInstall: !IS_DEV }),
  );
  autoUpdater.on("update-available", (info) =>
    emitUpdateStatus({
      status: "available",
      latestVersion: normalizeVersion(info.version),
      updateAvailable: true,
      releaseName: info.releaseName || `EchoDesk v${info.version}`,
      releaseUrl: RELEASES_URL,
      canAutoInstall: !IS_DEV,
    }),
  );
  autoUpdater.on("update-not-available", (info) =>
    emitUpdateStatus({
      status: "current",
      latestVersion: normalizeVersion(info.version),
      updateAvailable: false,
      canAutoInstall: !IS_DEV,
    }),
  );
  autoUpdater.on("download-progress", (progress) =>
    emitUpdateStatus({
      status: "downloading",
      percent: Math.round(progress.percent || 0),
      canAutoInstall: true,
    }),
  );
  autoUpdater.on("update-downloaded", (info) =>
    emitUpdateStatus({
      status: "downloaded",
      latestVersion: normalizeVersion(info.version),
      updateAvailable: true,
      canAutoInstall: true,
    }),
  );
  autoUpdater.on("error", (err) =>
    emitUpdateStatus({
      status: "error",
      error: err?.message || String(err),
      canAutoInstall: false,
    }),
  );
}

function firstLanAddress() {
  const interfaces = os.networkInterfaces();
  const candidates = [];
  for (const [name, entries] of Object.entries(interfaces)) {
    for (const entry of entries || []) {
      if (!entry || entry.family !== "IPv4" || entry.internal) continue;
      const address = entry.address;
      if (!address || address.startsWith("169.254.")) continue;
      const score =
        name === "en0" || name === "en1"
          ? 0
          : address.startsWith("192.168.") || address.startsWith("10.") || address.startsWith("172.")
            ? 1
            : 2;
      candidates.push({ address, score, name });
    }
  }
  candidates.sort((a, b) => a.score - b.score || a.name.localeCompare(b.name));
  return candidates[0]?.address || "127.0.0.1";
}

function shareBackendHost() {
  const configured = normalizeHttpBase(process.env.ECHO_SHARE_BASE_URL);
  if (configured) return configured;
  if (PUBLIC_DEMO_MODE) return PUBLIC_BACKEND_HOST;
  return `http://${firstLanAddress()}:${BACKEND_PORT}`;
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
    let url;
    try {
      url = new URL("/healthz", BACKEND_HOST);
    } catch {
      done(false);
      return;
    }
    const transport = url.protocol === "https:" ? https : http;
    const req = transport.get(url, { timeout: HEALTH_TIMEOUT_MS }, (res) => {
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
    if (PUBLIC_DEMO_MODE) {
      emitStatus({
        state: "degraded",
        reason: "public backend unhealthy",
        attempts: 0,
        last_error: "healthz failed",
      });
      return;
    }
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
        BACKEND_BIND_HOST,
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
    log(
      `[backend] spawn disabled (${PUBLIC_DEMO_MODE ? "public demo" : "ECHO_SPAWN_BACKEND=0"}), using ${BACKEND_HOST}`,
    );
    externalMode = true;
    emitStatus({
      state: "external",
      port: PUBLIC_DEMO_MODE ? undefined : BACKEND_PORT,
      mode: PUBLIC_DEMO_MODE ? "public-demo" : "external",
    });
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
    if (lastUpdateStatus) {
      try {
        mainWindow.webContents.send("updates:status", lastUpdateStatus);
      } catch (e) {
        log(`[updates] replay failed: ${e.message}`);
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
ipcMain.handle("echo:share-backend-host", () => shareBackendHost());

ipcMain.handle("shell:open-external", async (_event, url) => {
  if (typeof url !== "string" || !/^https?:\/\//i.test(url)) {
    throw new Error("http(s) url required");
  }
  await shell.openExternal(url);
  return { ok: true };
});

ipcMain.handle("updates:check", async () => {
  emitUpdateStatus({ status: "checking" });
  let fallback;
  try {
    fallback = await fetchLatestReleaseStatus({
      status: "checked",
      canAutoInstall: false,
    });
  } catch (e) {
    fallback = {
      status: "error",
      currentVersion: app.getVersion(),
      latestVersion: null,
      updateAvailable: false,
      releaseUrl: RELEASES_URL,
      assetName: null,
      assetUrl: null,
      canAutoInstall: false,
      error: e?.message || String(e),
    };
  }
  if (!autoUpdater || IS_DEV) {
    emitUpdateStatus(fallback);
    return fallback;
  }
  try {
    const result = await autoUpdater.checkForUpdates();
    const info = result?.updateInfo;
    const latestVersion = normalizeVersion(info?.version || fallback.latestVersion);
    const merged = {
      ...fallback,
      status: compareVersions(latestVersion, app.getVersion()) > 0
        ? "available"
        : "current",
      latestVersion,
      updateAvailable: compareVersions(latestVersion, app.getVersion()) > 0,
      canAutoInstall: compareVersions(latestVersion, app.getVersion()) > 0,
    };
    emitUpdateStatus(merged);
    return merged;
  } catch (e) {
    const merged = {
      ...fallback,
      status: fallback.updateAvailable ? "available" : "checked",
      error: e?.message || String(e),
      canAutoInstall: false,
    };
    emitUpdateStatus(merged);
    return merged;
  }
});

ipcMain.handle("updates:download-and-install", async () => {
  if (!autoUpdater || IS_DEV) {
    await shell.openExternal(RELEASES_URL);
    return { ok: false, reason: "manual-release-page", releaseUrl: RELEASES_URL };
  }
  emitUpdateStatus({ status: "downloading", percent: 0, canAutoInstall: true });
  try {
    await autoUpdater.downloadUpdate();
    emitUpdateStatus({ status: "installing", canAutoInstall: true });
    autoUpdater.quitAndInstall(false, true);
    return { ok: true };
  } catch (e) {
    emitUpdateStatus({
      status: "error",
      error: e?.message || String(e),
      canAutoInstall: false,
    });
    throw e instanceof Error ? e : new Error(String(e));
  }
});

ipcMain.handle("updates:open-release", async () => {
  await shell.openExternal(RELEASES_URL);
  return { ok: true, releaseUrl: RELEASES_URL };
});

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
  if (PUBLIC_DEMO_MODE) {
    const ok = await healthcheckOnce();
    emitStatus(
      ok
        ? { state: "ready", mode: "public-demo" }
        : {
            state: "degraded",
            reason: "public backend unhealthy",
            attempts: 0,
            last_error: "healthz failed",
          },
    );
    return { ok };
  }
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
