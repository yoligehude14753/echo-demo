/* eslint-disable @typescript-eslint/no-var-requires */
const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  dialog,
  shell,
  ipcMain,
  systemPreferences,
  session,
  safeStorage,
  protocol,
  net,
  nativeImage,
} = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const path = require("node:path");
const http = require("node:http");
const https = require("node:https");
const fs = require("node:fs");
const os = require("node:os");
const { randomBytes } = require("node:crypto");
const backendConfig = require("../backend.config.json");
const {
  resolveBackendEndpoint,
  resolveShareBackendBase,
} = require("./backend-endpoint.cjs");
const {
  createManualBackendRestart,
  stopBackendProcess,
} = require("./backend-manual-restart.cjs");
const {
  electronNodeRuntimeEnvironment,
} = require("./backend-runtime-env.cjs");
const { createCredentialVault } = require("./credential-vault.cjs");
const {
  backendBoundJsonFetch,
  createPublicIdentitySessionManager,
} = require("./public-identity-session.cjs");
const {
  createWorkspaceBackendTransport,
  readWorkspaceJsonResponse,
} = require("./workspace-backend-transport.cjs");
const {
  atomicWritePrivateJsonFile,
  readPrivateJsonFile,
} = require("./private-json-store.cjs");
const {
  installMediaPermissionHandlers,
} = require("./media-permission-policy.cjs");
const {
  verifyWorkspaceRootIdentity,
} = require("./workspace-root-identity.cjs");
const {
  normalizedWorkspaceRegistry,
  prepareWorkspaceUploadsForClear,
  reapOrphanedWorkspaceDocIds,
  shouldRetainWorkspaceFileOnScanFailure,
  withWorkspaceState,
  workspaceDocReferenceCount,
  workspacePendingSnapshotDirectories,
  workspaceProjectionAfterCleanup,
  workspaceProjectionAfterUpload,
  workspaceRegistryPendingSnapshotDirectories,
  workspaceRendererHandle,
  workspaceStateForOrigin,
} = require("./workspace-registry.cjs");
const {
  pathContains: controlledPathContains,
  resolveControlledLocalArtifactPath,
} = require("./controlled-local-file.cjs");
const { downloadRendererBlob } = require("./artifact-download.cjs");
const {
  WORKSPACE_SNAPSHOT_PREFIX,
  cleanupStaleWorkspaceSnapshotDirs,
  cleanupWorkspaceSnapshotDirectory,
  copyRetainedWorkspaceSnapshot,
  createWorkspaceFileSnapshot,
  ensurePrivateWorkspaceSnapshotRoot,
  removeRetainedWorkspaceSnapshotFile,
} = require("./workspace-file-snapshot.cjs");
const {
  APP_ENTRY_URL,
  APP_HOST,
  APP_SCHEME,
  installAppProtocol,
  registerAppScheme,
} = require("./app-protocol.cjs");
const { createAppUpdateManager } = require("./app-update-protocol.cjs");
const {
  projectBackendStatusForRenderer,
} = require("./backend-status-projection.cjs");
const {
  BackendContractError,
  expectedBackendContract,
  probeBackendContract,
} = require("./backend-contract.cjs");
const { resolveDesktopProductVersion } = require("./product-version.cjs");
const { createModelRuntimeIpcSurface } = require("./model-runtime-contract.cjs");
const {
  startPackagedFusedWorkerBridge,
} = require("./packaged-fused-worker-bridge.cjs");
const {
  DEFAULT_BACKGROUND_STATUS,
  normalizeBackgroundStatus,
  formalMeetingStatusLabel,
  captureStatusLabel,
} = require("./background-residency.cjs");

// Electron 要求 privileged scheme 在 app ready 前完成声明。打包态只从该
// secure/standard origin 加载静态资源，不再使用具有 opaque Origin 的 file://。
registerAppScheme(protocol);

app.commandLine.appendSwitch("enable-media-stream");

// app.getVersion() is Electron's runtime version in the dev CLI path.  The
// backend contract must use EchoDesk's package version in both dev and asar.
const DESKTOP_PRODUCT_VERSION = resolveDesktopProductVersion(
  path.join(__dirname, "..", "package.json"),
);

const IS_DEV = !!process.env.ELECTRON_DEV;
const VITE_URL = process.env.VITE_DEV_URL || "http://localhost:5173";
const BACKEND_ENDPOINT = resolveBackendEndpoint(backendConfig, process.env, {
  isDevelopment: IS_DEV,
});
const BACKEND_PORT = BACKEND_ENDPOINT.port;
const LOCAL_BACKEND_HOST = BACKEND_ENDPOINT.localBase;
const PUBLIC_BACKEND_HOST = BACKEND_ENDPOINT.publicBase;
const RELEASE_OWNER = "yoligehude14753";
const RELEASE_REPO = "echo-demo";
const RELEASES_URL = `https://github.com/${RELEASE_OWNER}/${RELEASE_REPO}/releases`;
const AUTO_UPDATE_CHECK_DELAY_MS = Math.max(
  0,
  parseInt(process.env.ECHODESK_AUTO_UPDATE_CHECK_DELAY_MS || "15000", 10) || 15000,
);
const AUTO_UPDATE_CHECK_INTERVAL_MS = Math.max(
  15 * 60 * 1000,
  parseInt(process.env.ECHODESK_AUTO_UPDATE_CHECK_INTERVAL_MS || "", 10) ||
    4 * 60 * 60 * 1000,
);
const PUBLIC_DEMO_MODE = BACKEND_ENDPOINT.mode === "public";
const BACKEND_HOST = BACKEND_ENDPOINT.backendBase;
const BACKEND_BIND_HOST = BACKEND_ENDPOINT.bindHost;

// Installed Preview defaults to the supervised bundled local backend/fused
// worker. Remote public service is an explicit ECHO_PRINCIPAL_MODE=public opt-in.
const SPAWN_BACKEND = BACKEND_ENDPOINT.spawnBackend;

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
const STARTUP_TIMEOUT_MS = 120_000;
// SIGTERM 后给 uvicorn 3s 跑 lifespan shutdown，超时强制 SIGKILL
const SIGKILL_GRACE_MS = 3000;

// ---------- 运行时状态 ----------
let backendProc = null;
let fusedWorkerBridge = null;
let fusedWorkerNonce = null;
let backendLifecycleGeneration = 0;
let mainWindow = null;
let tray = null;
let backgroundStatus = DEFAULT_BACKGROUND_STATUS;
let healthTimer = null;
let publicBackendHealthTimer = null;
let healthStartedAt = 0;
let healthFailures = 0;
let backendWasReady = false;
let restartAttempts = 0;
let shuttingDown = false;
let quittingForReal = false;
let expectedLocalBackendContractPromise = null;
let backendHealthcheckPromise = null;
let lastBackendContractFailure = null;
let pythonResolved = null; // { python: string|null, searched: string[] }
// renderer 启动慢于 backend：early status 缓存到 lastStatus，等 did-finish-load 后 replay
let lastStatus = null;
let rendererReady = false;
let lastUpdateStatus = {
  status: "idle",
  currentVersion: app.getVersion(),
  releaseUrl: RELEASES_URL,
};
let updateCheckTimer = null;
let updateCheckInFlight = false;
let appUpdateManager = null;
const activeArtifactDownloadSenders = new WeakSet();
const START_HIDDEN = process.argv.includes("--hidden");
const SMOKE_EXIT_ON_WINDOW_CLOSE = process.argv.includes("--smoke-exit-on-window-close");

const singleInstanceLock = app.requestSingleInstanceLock();
if (!singleInstanceLock) {
  app.quit();
}

// 主进程未捕获异常不弹 fatal dialog；UI 应该自己感知 backend 状态
process.on("uncaughtException", (err) => {
  console.error("[main] uncaught exception:", err);
});
process.on("unhandledRejection", (reason) => {
  console.error("[main] unhandled rejection:", reason);
});

function log(msg) {
  console.log(msg);
  try {
    const logDir = path.join(app.getPath("userData"), "logs");
    fs.mkdirSync(logDir, { recursive: true });
    fs.appendFileSync(
      path.join(logDir, "main.log"),
      `${new Date().toISOString()} ${msg}\n`,
      "utf8",
    );
  } catch {
    // Best-effort diagnostic log only.
  }
}

function appendBackendSupervisorLog(streamName, bytes) {
  try {
    const logDir = path.join(app.getPath("userData"), "logs");
    fs.mkdirSync(logDir, { recursive: true });
    fs.appendFileSync(
      path.join(logDir, `backend-${streamName}.log`),
      bytes,
    );
  } catch (error) {
    log(`[backend] ${streamName} log write failed [${safeFailureCode(error)}]`);
  }
}

function isTrustedRenderer(webContents) {
  try {
    const url = webContents?.getURL?.() || "";
    return isTrustedAppRendererUrl(url);
  } catch {
    return false;
  }
}

const PUBLIC_CREDENTIAL_FILENAME = "public-device-credential.bin";

function isTrustedAppRendererUrl(rawUrl) {
  try {
    const candidate = new URL(rawUrl);
    if (IS_DEV) {
      return candidate.origin === new URL(VITE_URL).origin;
    }
    return (
      candidate.protocol === `${APP_SCHEME}:` &&
      candidate.hostname === APP_HOST &&
      !candidate.port &&
      !candidate.username &&
      !candidate.password &&
      candidate.pathname === "/index.html" &&
      !candidate.search &&
      !candidate.hash
    );
  } catch {
    return false;
  }
}

function isTrustedAppRendererOrigin(rawOrigin) {
  try {
    const candidate = new URL(rawOrigin);
    if (candidate.username || candidate.password || candidate.search || candidate.hash) {
      return false;
    }
    if (IS_DEV) {
      return candidate.origin === new URL(VITE_URL).origin &&
        (candidate.pathname === "" || candidate.pathname === "/");
    }
    return (
      candidate.protocol === `${APP_SCHEME}:` &&
      candidate.hostname === APP_HOST &&
      !candidate.port &&
      (candidate.pathname === "" || candidate.pathname === "/")
    );
  } catch {
    return false;
  }
}

function assertTrustedIpcOrigin(event) {
  const senderUrl = event.senderFrame?.url || event.sender?.getURL?.() || "";
  if (!isTrustedAppRendererUrl(senderUrl)) {
    const error = new Error("IPC denied for untrusted renderer origin");
    error.code = "UNTRUSTED_RENDERER_IPC";
    throw error;
  }
}

function trustedRendererBlobInnerOrigin() {
  if (IS_DEV) return new URL(VITE_URL).origin;
  return `${APP_SCHEME}://${APP_HOST}`;
}

function publicCredentialPath() {
  return path.join(app.getPath("userData"), PUBLIC_CREDENTIAL_FILENAME);
}

let publicCredentialVault = null;
let publicIdentityManager = null;
let publicSessionEnsurePromise = null;
let currentPublicSession = null;

function credentialVault() {
  if (publicCredentialVault === null) {
    publicCredentialVault = createCredentialVault({
      safeStorage,
      target: publicCredentialPath(),
      backendBase: BACKEND_HOST,
      officialBackendBase: BACKEND_ENDPOINT.publicServiceEndpoint,
      enabled: PUBLIC_DEMO_MODE,
      logger: (message) => log(`[credential] ${message}`),
    });
  }
  return publicCredentialVault;
}

function clearPublicCredential() {
  if (PUBLIC_DEMO_MODE) {
    const origin = credentialVault().backendOrigin;
    cancelWorkspaceOperations(origin);
    pendingWorkspaceSelections.delete(origin);
    const store = readLocalWorkspaceStore(origin);
    const retainedSnapshots = Object.values(store.pending_uploads || {})
      .map((pending) => pending?.snapshot_path)
      .filter((snapshotPath) => typeof snapshotPath === "string");
    // Remote document ids belong to the authenticated principal. Retaining
    // them across credential rotation could let a replacement principal act
    // on the previous owner's projection, even at the same backend origin.
    writeLocalWorkspaceStore(origin, {
      ...store,
      files: {},
      doc_ids: [],
      pending_uploads: {},
      lastScan: null,
    });
    for (const snapshotPath of retainedSnapshots) {
      void removeRetainedWorkspaceSnapshot(snapshotPath);
    }
  }
  credentialVault().clear();
  publicIdentityManager = null;
  publicSessionEnsurePromise = null;
  currentPublicSession = null;
}

function newDeviceSecret() {
  return randomBytes(32).toString("base64url");
}

async function postPublicIdentity(pathname, body, { token = null } = {}) {
  const vault = credentialVault();
  const headers = {
    "Content-Type": "application/json",
    "X-EchoDesk-Client-Version": app.getVersion(),
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  return backendBoundJsonFetch({
    backendOrigin: vault.backendOrigin,
    pathname,
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

function publicIdentitySessionManager() {
  if (publicIdentityManager === null) {
    publicIdentityManager = createPublicIdentitySessionManager({
      vault: credentialVault(),
      request: postPublicIdentity,
      newSecret: newDeviceSecret,
      // Enrollment does not need the user's machine hostname. Keep the
      // server-visible label generic unless a future explicit consent UI is added.
      displayName: "EchoDesk Desktop",
    });
  }
  return publicIdentityManager;
}

async function renewPublicSessionFromCredential() {
  const session = await publicIdentitySessionManager().renew();
  const bound = session
    ? { ...session, backend_origin: credentialVault().backendOrigin }
    : null;
  currentPublicSession = bound;
  return bound;
}

function reusablePublicSession() {
  if (!currentPublicSession?.token) return null;
  const expiresAt = Date.parse(currentPublicSession.expires_at || "");
  if (Number.isFinite(expiresAt) && expiresAt <= Date.now() + 5_000) {
    currentPublicSession = null;
    return null;
  }
  return currentPublicSession;
}

async function ensurePublicSessionInMain() {
  if (!PUBLIC_DEMO_MODE) return null;
  const active = reusablePublicSession();
  if (active) return active;
  if (publicSessionEnsurePromise) return publicSessionEnsurePromise;
  const pending = (async () => {
    const session = await publicIdentitySessionManager().ensure();
    if (session) {
      currentPublicSession = {
        ...session,
        backend_origin: credentialVault().backendOrigin,
      };
      return currentPublicSession;
    }
    const error = new Error(
      "device identity is no longer valid; refusing to enroll a replacement owner",
    );
    error.code = "IDENTITY_LOST";
    throw error;
  })();
  publicSessionEnsurePromise = pending;
  try {
    return await pending;
  } finally {
    if (publicSessionEnsurePromise === pending) publicSessionEnsurePromise = null;
  }
}

async function rotatePublicCredential(sessionToken) {
  return publicIdentitySessionManager().rotate(sessionToken);
}

let publicWorkspaceBackendTransport = null;

function workspaceBackendTransport() {
  if (!PUBLIC_DEMO_MODE) {
    const error = new Error(
      "local workspace IPC is only available for the public desktop runtime",
    );
    error.code = "WORKSPACE_BACKEND_DISABLED";
    throw error;
  }
  if (publicWorkspaceBackendTransport === null) {
    const vault = credentialVault();
    publicWorkspaceBackendTransport = createWorkspaceBackendTransport({
      backendBase: BACKEND_HOST,
      vault,
      ensureSession: ensurePublicSessionInMain,
      renewSession: renewPublicSessionFromCredential,
      clientVersion: app.getVersion(),
    });
  }
  return publicWorkspaceBackendTransport;
}

function sqliteCliJson(dbPath, sql) {
  const result = spawnSync("sqlite3", ["-readonly", "-json", dbPath, sql], {
    encoding: "utf8",
    maxBuffer: 80 * 1024 * 1024,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || "sqlite3 failed").trim());
  }
  const out = (result.stdout || "").trim();
  if (!out) return [];
  return JSON.parse(out);
}

function sqlQuote(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

function parseMinutesJson(raw) {
  if (!raw || typeof raw !== "string") return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function loadLegacyEchodeskHistory() {
  const candidates = [
    process.env.ECHODESK_LEGACY_DB,
    path.join(os.homedir(), ".echodesk", "echodesk.db"),
  ].filter(Boolean);
  const dbPath = candidates.find((candidate) => {
    try {
      return fs.existsSync(candidate) && fs.statSync(candidate).isFile();
    } catch {
      return false;
    }
  });
  if (!dbPath) return null;

  const stat = fs.statSync(dbPath);
  const tables = sqliteCliJson(
    dbPath,
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
  ).map((row) => row.name);
  if (!tables.includes("meetings") || !tables.includes("meeting_segments")) {
    return null;
  }

  const rawMeetings = sqliteCliJson(
    dbPath,
    `SELECT id, title, state, started_at, ended_at, finalized_at, minutes_json,
            minutes_status, minutes_error, display_title
       FROM meetings
      ORDER BY started_at DESC
      LIMIT 200`,
  );
  const ids = rawMeetings.map((row) => row.id).filter(Boolean);
  const idList = ids.map(sqlQuote).join(",");
  const rawSegments = idList
    ? sqliteCliJson(
        dbPath,
        `SELECT meeting_id, text, start_ms, end_ms, speaker_id, speaker_label
           FROM meeting_segments
          WHERE meeting_id IN (${idList})
          ORDER BY meeting_id ASC, id ASC`,
      )
    : [];

  const segmentsByMeeting = new Map();
  for (const seg of rawSegments) {
    if (!seg.meeting_id || !seg.text) continue;
    const list = segmentsByMeeting.get(seg.meeting_id) || [];
    if (list.length < 800) {
      list.push({
        text: String(seg.text),
        start_ms: Number(seg.start_ms) || 0,
        end_ms: Number(seg.end_ms) || 0,
        speaker_id: seg.speaker_id || null,
        speaker_label: seg.speaker_label || null,
      });
    }
    segmentsByMeeting.set(seg.meeting_id, list);
  }

  const meetings = rawMeetings
    .filter((row) => row.id)
    .map((row) => {
      const segments = segmentsByMeeting.get(row.id) || [];
      const speakers = Array.from(
        new Set(
          segments
            .map((seg) => seg.speaker_label)
            .filter((label) => typeof label === "string" && label.trim()),
        ),
      );
      const minutes = parseMinutesJson(row.minutes_json);
      return {
        meeting_id: row.id,
        title: row.title || row.id,
        display_title: row.display_title || minutes?.title || null,
        state: row.state === "in_meeting" ? "in_meeting" : "ended",
        started_at: row.started_at || undefined,
        ended_at: row.ended_at || row.finalized_at || undefined,
        segments,
        speakers,
        minutes: minutes || undefined,
        minutes_status: minutes ? "ok" : row.minutes_status || null,
        minutes_error: row.minutes_error || null,
        artifacts: [],
      };
    });

  const ambientSegments = tables.includes("ambient_segments")
    ? sqliteCliJson(
        dbPath,
        `SELECT text, captured_at, speaker_id, speaker_label, duration_ms
           FROM ambient_segments
          ORDER BY captured_at DESC
          LIMIT 120`,
      )
        .reverse()
        .map((row) => ({
          text: String(row.text || ""),
          captured_at: row.captured_at || new Date().toISOString(),
          speaker_id: row.speaker_id || null,
          speaker_label: row.speaker_label || null,
          duration_ms: Number(row.duration_ms) || 0,
        }))
        .filter((row) => row.text)
    : [];

  return {
    schema: 1,
    appVersion: app.getVersion(),
    savedAt: new Date().toISOString(),
    currentMeetingId: null,
    meetings,
    ambientSegments,
    artifacts: [],
    sourceSize: stat.size,
    sourceMtimeMs: stat.mtimeMs,
    importedAt: new Date().toISOString(),
    meetingCount: meetings.length,
    segmentCount: rawSegments.length,
  };
}

const WORKSPACE_MAX_FILE_MB = Math.max(
  1,
  Number.parseFloat(process.env.ECHO_WORKSPACE_MAX_FILE_MB || "100") || 100,
);
const WORKSPACE_MAX_BYTES = Math.floor(WORKSPACE_MAX_FILE_MB * 1024 * 1024);
const WORKSPACE_DURABLE_SNAPSHOT_DIRNAME = "workspace-upload-snapshots";
const WORKSPACE_SUPPORTED_EXTS = new Set([
  ".pdf",
  ".docx",
  ".doc",
  ".pptx",
  ".ppt",
  ".xlsx",
  ".xls",
  ".html",
  ".htm",
  ".csv",
  ".epub",
  ".msg",
  ".eml",
  ".md",
  ".markdown",
  ".txt",
  ".text",
  ".log",
  ".rst",
  ".json",
  ".jsonl",
  ".yaml",
  ".yml",
  ".xml",
  ".srt",
  ".vtt",
  ".sql",
  ".py",
  ".js",
  ".jsx",
  ".ts",
  ".tsx",
  ".go",
  ".rs",
  ".java",
  ".c",
  ".cc",
  ".cpp",
  ".h",
  ".hpp",
  ".sh",
  ".zsh",
  ".toml",
  ".ini",
  ".cfg",
  ".env",
  ".conf",
]);
const WORKSPACE_EXCLUDED_DIRS = new Set([
  ".git",
  ".hg",
  ".svn",
  "node_modules",
  "dist",
  "build",
  ".next",
  ".nuxt",
  ".cache",
  ".venv",
  "venv",
  "__pycache__",
]);
const workspaceHandleSecret = randomBytes(32);
const pendingWorkspaceSelections = new Map();

function safeFailureCode(error) {
  const raw = String(error?.code || error?.name || "ERROR").toUpperCase();
  return raw.replace(/[^A-Z0-9_-]/g, "_").slice(0, 64) || "ERROR";
}

function safeWorkspaceLabel(raw) {
  return (path.basename(String(raw || "")) || "工作区")
    .replace(/[\r\n]/g, " ")
    .trim()
    .slice(0, 100) || "工作区";
}

function workspaceHandle(expectedOrigin, absolutePath) {
  return workspaceRendererHandle(
    expectedOrigin,
    absolutePath,
    workspaceHandleSecret,
  );
}

function workspaceHandles(expectedOrigin, paths) {
  return (paths || []).map((workspacePath) =>
    workspaceHandle(expectedOrigin, workspacePath),
  );
}

function workspaceStorePath() {
  return path.join(app.getPath("userData"), "workspaces.json");
}

function workspaceSnapshotAllowedRoots() {
  const durableRoot = ensurePrivateWorkspaceSnapshotRoot(
    path.join(app.getPath("userData"), WORKSPACE_DURABLE_SNAPSHOT_DIRNAME),
  );
  return Array.from(new Set([durableRoot, path.resolve(os.tmpdir())]));
}

async function createWorkspaceSnapshotDirectory() {
  const [durableRoot] = workspaceSnapshotAllowedRoots();
  const directory = await fs.promises.mkdtemp(
    path.join(durableRoot, WORKSPACE_SNAPSHOT_PREFIX),
  );
  await fs.promises.chmod(directory, 0o700);
  return directory;
}

async function sweepWorkspaceSnapshotRoots(protectedDirectories) {
  for (const root of workspaceSnapshotAllowedRoots()) {
    await cleanupStaleWorkspaceSnapshotDirs(root, {
      logger: (message) => log(`[workspace] ${message}`),
      protectedDirectories,
    });
  }
}

function expandHome(raw) {
  const value = String(raw || "").trim();
  if (!value) return "";
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
}

function normalizeLocalWorkspaceDir(raw) {
  const expanded = expandHome(raw);
  if (!expanded) throw new Error("目录路径不能为空");
  const resolved = path.resolve(expanded);
  let stat;
  try {
    stat = fs.lstatSync(resolved);
  } catch {
    const error = new Error("所选工作区目录不可用");
    error.code = "WORKSPACE_DIRECTORY_UNAVAILABLE";
    throw error;
  }
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    const error = new Error("所选工作区不是目录");
    error.code = "WORKSPACE_DIRECTORY_INVALID";
    throw error;
  }
  try {
    return fs.realpathSync.native(resolved);
  } catch {
    const error = new Error("所选工作区目录不可用");
    error.code = "WORKSPACE_DIRECTORY_UNAVAILABLE";
    throw error;
  }
}

function readLocalWorkspaceRegistry({ strict = false } = {}) {
  const storePath = workspaceStorePath();
  try {
    const raw = readPrivateJsonFile(storePath);
    return normalizedWorkspaceRegistry(raw, BACKEND_HOST);
  } catch (error) {
    log(`[workspace] read store failed: ${safeFailureCode(error)}`);
    if (strict) throw error;
    return normalizedWorkspaceRegistry(null, BACKEND_HOST);
  }
}

function readLocalWorkspaceStore(expectedOrigin) {
  return workspaceStateForOrigin(readLocalWorkspaceRegistry(), expectedOrigin);
}

function writeLocalWorkspaceStore(expectedOrigin, store) {
  const storePath = workspaceStorePath();
  fs.mkdirSync(path.dirname(storePath), { recursive: true });
  const payload = {
    ...withWorkspaceState(
      readLocalWorkspaceRegistry({ strict: true }),
      expectedOrigin,
      store,
    ),
    updatedAt: new Date().toISOString(),
  };
  atomicWritePrivateJsonFile(storePath, payload);
}

function localWorkspaceDirs(expectedOrigin) {
  const store = readLocalWorkspaceStore(expectedOrigin);
  return store.workspaces.filter((d) => typeof d === "string" && d.trim());
}

function resolveWorkspaceHandle(
  expectedOrigin,
  rawHandle,
  { includePending = false } = {},
) {
  if (typeof rawHandle === "string" && rawHandle) {
    for (const configuredPath of localWorkspaceDirs(expectedOrigin)) {
      if (workspaceHandle(expectedOrigin, configuredPath) === rawHandle) {
        return configuredPath;
      }
    }
    const pending = pendingWorkspaceSelections.get(expectedOrigin);
    if (includePending && pending?.handle === rawHandle) return pending.path;
  }
  const error = new Error("工作区句柄无效或已过期");
  error.code = "WORKSPACE_HANDLE_INVALID";
  throw error;
}

function throwIfWorkspaceAborted(signal) {
  if (!signal?.aborted) return;
  if (signal.reason instanceof Error) throw signal.reason;
  throw new DOMException("workspace operation cancelled", "AbortError");
}

function shouldSkipWorkspaceDir(name) {
  return name.startsWith(".") || WORKSPACE_EXCLUDED_DIRS.has(name);
}

function hasHiddenPathPart(root, target) {
  const rel = path.relative(root, target);
  if (!rel || rel.startsWith("..")) return false;
  return rel.split(path.sep).some((part) => part.startsWith("."));
}

async function collectWorkspaceFiles(
  root,
  authorizedRoot,
  canonicalAuthorizedRoot,
  result,
  out,
  failedPaths,
  signal,
  expectedAuthorizedIdentity = null,
) {
  throwIfWorkspaceAborted(signal);
  const outputStart = out.length;
  let entries;
  let openedStat;
  let openedCanonical;
  let stableAuthorizedRoot = canonicalAuthorizedRoot;
  try {
    [openedStat, openedCanonical] = await Promise.all([
      fs.promises.lstat(root, { bigint: true }),
      fs.promises.realpath(root),
    ]);
    if (!stableAuthorizedRoot) {
      stableAuthorizedRoot = await fs.promises.realpath(authorizedRoot);
    }
    if (
      !openedStat.isDirectory() ||
      openedStat.isSymbolicLink() ||
      (path.resolve(root) === path.resolve(authorizedRoot) &&
        expectedAuthorizedIdentity &&
        (String(openedStat.dev) !== String(expectedAuthorizedIdentity.dev) ||
          String(openedStat.ino) !== String(expectedAuthorizedIdentity.ino))) ||
      !controlledPathContains(stableAuthorizedRoot, openedCanonical)
    ) {
      const error = new Error("workspace directory escaped its authorized root");
      error.code = "WORKSPACE_DIRECTORY_OUTSIDE_ROOT";
      throw error;
    }
    entries = await fs.promises.readdir(root, { withFileTypes: true });
  } catch (e) {
    throwIfWorkspaceAborted(signal);
    failedPaths.add(path.resolve(root));
    result.n_failed += 1;
    result.errors.push(
      `读取目录失败 ${safeWorkspaceLabel(root)} [${safeFailureCode(e)}]`,
    );
    return;
  }

  for (const entry of entries) {
    throwIfWorkspaceAborted(signal);
    const fullPath = path.join(root, entry.name);
    try {
      if (entry.isDirectory()) {
        if (shouldSkipWorkspaceDir(entry.name)) continue;
        await collectWorkspaceFiles(
          fullPath,
          authorizedRoot,
          stableAuthorizedRoot,
          result,
          out,
          failedPaths,
          signal,
          expectedAuthorizedIdentity,
        );
        continue;
      }
      if (!entry.isFile()) continue;
      if (hasHiddenPathPart(root, fullPath)) continue;
      const ext = path.extname(entry.name).toLowerCase();
      if (!WORKSPACE_SUPPORTED_EXTS.has(ext)) continue;
      const stat = await fs.promises.lstat(fullPath);
      if (!stat.isFile()) {
        const error = new Error("workspace source changed during enumeration");
        error.code = "WORKSPACE_SOURCE_INVALID";
        throw error;
      }
      if (stat.size > WORKSPACE_MAX_BYTES) {
        result.n_skipped += 1;
        result.errors.push(
          `跳过超大文件 ${entry.name}: ${(stat.size / 1024 / 1024).toFixed(1)}MB > ${WORKSPACE_MAX_FILE_MB}MB`,
        );
        continue;
      }
      result.n_total += 1;
      out.push({
        path: path.resolve(fullPath),
        name: entry.name,
        size: stat.size,
        mtime: stat.mtimeMs,
        authorizedRoot: stableAuthorizedRoot,
        configuredRoot: authorizedRoot,
        authorizedRootIdentity: expectedAuthorizedIdentity,
      });
    } catch (e) {
      throwIfWorkspaceAborted(signal);
      failedPaths.add(path.resolve(fullPath));
      result.n_failed += 1;
      result.errors.push(
        `读取文件失败 ${safeWorkspaceLabel(fullPath)} [${safeFailureCode(e)}]`,
      );
    }
  }

  try {
    const [currentStat, currentCanonical] = await Promise.all([
      fs.promises.lstat(root, { bigint: true }),
      fs.promises.realpath(root),
    ]);
    if (
      !currentStat.isDirectory() ||
      currentStat.isSymbolicLink() ||
      currentStat.dev !== openedStat.dev ||
      currentStat.ino !== openedStat.ino ||
      currentCanonical !== openedCanonical
    ) {
      const error = new Error("workspace directory changed during enumeration");
      error.code = "WORKSPACE_DIRECTORY_CHANGED";
      throw error;
    }
  } catch (e) {
    throwIfWorkspaceAborted(signal);
    const discarded = out.splice(outputStart);
    result.n_total = Math.max(0, result.n_total - discarded.length);
    failedPaths.add(path.resolve(root));
    result.n_failed += 1;
    result.errors.push(
      `读取目录失败 ${safeWorkspaceLabel(root)} [${safeFailureCode(e)}]`,
    );
  }
}

async function uploadWorkspaceFile(expectedOrigin, fileInfo, snapshot, signal) {
  throwIfWorkspaceAborted(signal);
  const content = await fs.promises.readFile(snapshot.path, { signal });
  if (content.byteLength !== snapshot.size || content.byteLength > WORKSPACE_MAX_BYTES) {
    const error = new Error("workspace snapshot size changed unexpectedly");
    error.code = "WORKSPACE_SNAPSHOT_INVALID";
    throw error;
  }
  throwIfWorkspaceAborted(signal);
  const form = new FormData();
  const BlobCtor = globalThis.Blob || require("node:buffer").Blob;
  const fileName = path.basename(String(fileInfo.name || "workspace-file"));
  form.append("file", new BlobCtor([content]), fileName);
  form.append("title", path.basename(fileName, path.extname(fileName)));
  form.append("source", "workspace");
  const resp = await workspaceBackendTransport().request({
    expectedOrigin,
    pathname: "/rag/ingest",
    init: { method: "POST", body: form, signal },
    timeoutMs: 120_000,
  });
  const payload = await readWorkspaceJsonResponse(resp);
  if (typeof payload?.doc_id !== "string" || !payload.doc_id.trim()) {
    throw new Error("workspace ingest returned no doc_id");
  }
  return payload;
}

async function deleteRemoteRagDoc(expectedOrigin, docId, signal) {
  if (!docId) return;
  throwIfWorkspaceAborted(signal);
  const resp = await workspaceBackendTransport().request({
    expectedOrigin,
    pathname: `/rag/docs/${encodeURIComponent(docId)}`,
    init: { method: "DELETE", signal },
    timeoutMs: 30_000,
  });
  if (resp.status === 404) return;
  await readWorkspaceJsonResponse(resp);
}

async function localWorkspaceStatus(expectedOrigin) {
  const store = readLocalWorkspaceStore(expectedOrigin);
  const authorized = [];
  for (const directory of store.workspaces) {
    try {
      await verifyWorkspaceRootIdentity({
        root: directory,
        expectedIdentity: store.root_identities?.[directory] || null,
      });
      authorized.push(directory);
    } catch {
      // Status exposes only roots that still match the selected inode.
    }
  }
  return {
    configured_dirs: workspaceHandles(expectedOrigin, store.workspaces),
    authorized_dirs: workspaceHandles(expectedOrigin, authorized),
    n_indexed: store.doc_ids.length,
    max_file_mb: WORKSPACE_MAX_FILE_MB,
    scan_on_startup: false,
  };
}

function workspaceScanCheckpoint(
  store,
  nextFiles,
  managedDocIds,
  pendingUploads,
  result,
) {
  return {
    ...store,
    files: { ...nextFiles },
    doc_ids: Array.from(managedDocIds),
    pending_uploads: { ...pendingUploads },
    lastScan: {
      n_indexed: managedDocIds.size,
      scannedAt: new Date().toISOString(),
      errors: result.errors.slice(0, 20),
    },
  };
}

function replaceWorkspaceScanProjection(
  projection,
  nextFiles,
  managedDocIds,
  pendingUploads,
) {
  for (const key of Object.keys(nextFiles)) delete nextFiles[key];
  Object.assign(nextFiles, projection.files);
  managedDocIds.clear();
  for (const docId of projection.doc_ids) managedDocIds.add(docId);
  for (const key of Object.keys(pendingUploads)) delete pendingUploads[key];
  Object.assign(pendingUploads, projection.pending_uploads);
}

async function removeRetainedWorkspaceSnapshot(retainedPath) {
  await removeRetainedWorkspaceSnapshotFile(retainedPath, {
    logger: (message) => log(`[workspace] ${message}`),
    allowedRoots: workspaceSnapshotAllowedRoots(),
  });
}

async function recoverPendingWorkspaceUploads({
  expectedOrigin,
  store,
  nextFiles,
  managedDocIds,
  pendingUploads,
  snapshotDirectory,
  signal,
  persist,
  result,
}) {
  for (const [sourcePath, durablePending] of Object.entries({ ...pendingUploads })) {
    throwIfWorkspaceAborted(signal);
    let recoverySnapshot = null;
    let converged = false;
    const hadPreviousMapping = Boolean(nextFiles[sourcePath]);
    try {
      let activePending = durablePending;
      let uploaded = durablePending.uploaded_doc_id
        ? { doc_id: durablePending.uploaded_doc_id, title: durablePending.title }
        : null;
      if (!uploaded) {
        recoverySnapshot = await copyRetainedWorkspaceSnapshot({
          retainedPath: durablePending.snapshot_path,
          snapshotDirectory,
          expectedSha256: durablePending.sha256,
          expectedSize: durablePending.size,
          maxBytes: WORKSPACE_MAX_BYTES,
          signal,
          allowedRoots: workspaceSnapshotAllowedRoots(),
        });
        if (activePending.upload_started_at === null) {
          activePending = {
            ...activePending,
            upload_started_at: Date.now(),
          };
          pendingUploads[sourcePath] = activePending;
          persist();
        }
        uploaded = await uploadWorkspaceFile(
          expectedOrigin,
          { name: durablePending.file_name },
          recoverySnapshot,
          signal,
        );
      }

      if (activePending.clear_requested === true) {
        const uploadedDocId = uploaded.doc_id.trim();
        pendingUploads[sourcePath] = {
          ...activePending,
          uploaded_doc_id: uploadedDocId,
        };
        managedDocIds.add(uploadedDocId);
        if (activePending.previous_doc_id) {
          managedDocIds.add(activePending.previous_doc_id);
        }
        // Persist the tombstone and every possibly-created remote id before
        // cleanup. Clear can return without blocking forever; future scans keep
        // retrying this tombstone but never apply it as an active mapping.
        persist();
        let cleanupFailed = false;
        const sourceMappedDocId =
          typeof nextFiles[sourcePath]?.doc_id === "string"
            ? nextFiles[sourcePath].doc_id.trim()
            : "";
        for (const docId of new Set([
          uploadedDocId,
          activePending.previous_doc_id,
        ])) {
          if (!docId) continue;
          const otherReferences =
            workspaceDocReferenceCount(nextFiles, docId) -
            (sourceMappedDocId === docId ? 1 : 0);
          if (otherReferences > 0) continue;
          try {
            await deleteRemoteRagDoc(expectedOrigin, docId, signal);
            managedDocIds.delete(docId);
          } catch (error) {
            throwIfWorkspaceAborted(signal);
            cleanupFailed = true;
            result.n_failed += 1;
            result.errors.push(
              `清理取消入库失败 ${activePending.file_name} [${safeFailureCode(error)}]`,
            );
          }
        }
        if (cleanupFailed) {
          persist();
          continue;
        }
        delete nextFiles[sourcePath];
        delete pendingUploads[sourcePath];
        persist();
        converged = true;
        continue;
      }

      const afterUpload = workspaceProjectionAfterUpload(
        workspaceScanCheckpoint(
          store,
          nextFiles,
          managedDocIds,
          pendingUploads,
          result,
        ),
        sourcePath,
        activePending,
        uploaded,
      );
      replaceWorkspaceScanProjection(
        afterUpload,
        nextFiles,
        managedDocIds,
        pendingUploads,
      );
      // This is the crash boundary: both the new mapping and old/new remote ids
      // are durable before any destructive remote cleanup begins.
      persist();

      const previousDocId = durablePending.previous_doc_id;
      const uploadedDocId = uploaded.doc_id.trim();
      let previousDeleted = false;
      if (
        previousDocId &&
        previousDocId !== uploadedDocId &&
        workspaceDocReferenceCount(nextFiles, previousDocId) === 0
      ) {
        try {
          await deleteRemoteRagDoc(expectedOrigin, previousDocId, signal);
          previousDeleted = true;
        } catch (error) {
          throwIfWorkspaceAborted(signal);
          result.n_failed += 1;
          result.errors.push(
            `清理旧索引失败 ${durablePending.file_name} [${safeFailureCode(error)}]`,
          );
        }
      }

      const afterCleanup = workspaceProjectionAfterCleanup(
        workspaceScanCheckpoint(
          store,
          nextFiles,
          managedDocIds,
          pendingUploads,
          result,
        ),
        sourcePath,
        { previousDeleted },
      );
      replaceWorkspaceScanProjection(
        afterCleanup,
        nextFiles,
        managedDocIds,
        pendingUploads,
      );
      persist();
      converged = true;
      if (hadPreviousMapping) result.n_updated += 1;
      else result.n_added += 1;
    } catch (error) {
      throwIfWorkspaceAborted(signal);
      result.n_failed += 1;
      result.errors.push(
        `恢复待入库文件失败 ${durablePending.file_name} [${safeFailureCode(error)}]`,
      );
    } finally {
      if (recoverySnapshot?.path) {
        try {
          await fs.promises.unlink(recoverySnapshot.path);
        } catch (error) {
          if (error?.code !== "ENOENT") {
            log(`[workspace] recovery copy cleanup deferred: ${safeFailureCode(error)}`);
          }
        }
      }
      if (converged) {
        await removeRetainedWorkspaceSnapshot(durablePending.snapshot_path);
      }
    }
  }
}

async function scanLocalWorkspaces(expectedOrigin, signal, leaseEpoch) {
  const started = Date.now();
  const result = {
    n_total: 0,
    n_added: 0,
    n_updated: 0,
    n_removed: 0,
    n_skipped: 0,
    n_failed: 0,
    duration_s: 0,
    errors: [],
  };
  const store = readLocalWorkspaceStore(expectedOrigin);
  const files = [];
  const failedPaths = new Set();
  const nextFiles = { ...(store.files || {}) };
  const managedDocIds = new Set(store.doc_ids || []);
  const pendingUploads = { ...(store.pending_uploads || {}) };
  let snapshotDirectory = null;
  const persist = () => {
    assertWorkspaceOperationCurrent(expectedOrigin, leaseEpoch, signal);
    writeLocalWorkspaceStore(
      expectedOrigin,
      workspaceScanCheckpoint(
        store,
        nextFiles,
        managedDocIds,
        pendingUploads,
        result,
      ),
    );
  };
  try {
    let protectedDirectories = null;
    try {
      protectedDirectories = workspaceRegistryPendingSnapshotDirectories(
        readLocalWorkspaceRegistry({ strict: true }),
      );
    } catch (error) {
      log(`[workspace] snapshot sweep skipped: ${safeFailureCode(error)}`);
    }
    if (protectedDirectories !== null) {
      await sweepWorkspaceSnapshotRoots(protectedDirectories);
    }
    snapshotDirectory = await createWorkspaceSnapshotDirectory();
    await recoverPendingWorkspaceUploads({
      expectedOrigin,
      store,
      nextFiles,
      managedDocIds,
      pendingUploads,
      snapshotDirectory,
      signal,
      persist,
      result,
    });
    for (const dir of store.workspaces) {
      let verifiedRoot;
      try {
        verifiedRoot = await verifyWorkspaceRootIdentity({
          root: dir,
          expectedIdentity: store.root_identities?.[dir] || null,
        });
        if (!store.root_identities?.[dir]) {
          store.root_identities = {
            ...(store.root_identities || {}),
            [dir]: verifiedRoot.identity,
          };
          // Legacy roots acquire an inode fence before enumeration or network I/O.
          persist();
        }
      } catch (error) {
        failedPaths.add(path.resolve(dir));
        result.n_failed += 1;
        result.errors.push(
          `读取目录失败 ${safeWorkspaceLabel(dir)} [${safeFailureCode(error)}]`,
        );
        continue;
      }
      await collectWorkspaceFiles(
        dir,
        dir,
        verifiedRoot.canonical,
        result,
        files,
        failedPaths,
        signal,
        verifiedRoot.identity,
      );
    }

    const orphanCleanup = await reapOrphanedWorkspaceDocIds(
      {
        ...store,
        files: nextFiles,
        doc_ids: Array.from(managedDocIds),
      },
      {
        deleteDoc: (docId) => deleteRemoteRagDoc(expectedOrigin, docId, signal),
        signal,
      },
    );
    for (const docId of orphanCleanup.deletedDocIds) {
      managedDocIds.delete(docId);
      result.n_removed += 1;
    }
    for (const { docId, error } of orphanCleanup.failures) {
      throwIfWorkspaceAborted(signal);
      result.n_failed += 1;
      result.errors.push(
        `清理孤立索引失败 ${String(docId).slice(0, 40)} [${safeFailureCode(error)}]`,
      );
    }

    const currentPaths = new Set(files.map((f) => f.path));
    for (const [sourcePath, meta] of Object.entries({ ...nextFiles })) {
      throwIfWorkspaceAborted(signal);
      if (currentPaths.has(sourcePath)) continue;
      if (
        shouldRetainWorkspaceFileOnScanFailure(
          sourcePath,
          store.workspaces,
          failedPaths,
        )
      ) {
        continue;
      }
      delete nextFiles[sourcePath];
      result.n_removed += 1;
      const previousDocId =
        typeof meta?.doc_id === "string" ? meta.doc_id.trim() : "";
      if (!previousDocId || workspaceDocReferenceCount(nextFiles, previousDocId) > 0) {
        continue;
      }
      try {
        await deleteRemoteRagDoc(expectedOrigin, previousDocId, signal);
        managedDocIds.delete(previousDocId);
      } catch (e) {
        throwIfWorkspaceAborted(signal);
        result.n_failed += 1;
        result.errors.push(
          `删除旧索引失败 ${safeWorkspaceLabel(sourcePath)} [${safeFailureCode(e)}]`,
        );
      }
    }

    for (const file of files) {
      throwIfWorkspaceAborted(signal);
      if (pendingUploads[file.path]) {
        // A failed recovery (including a deferred clear tombstone) owns this
        // source until it converges. Never overwrite its durable snapshot/id set.
        result.n_skipped += 1;
        continue;
      }
      const prev = nextFiles[file.path];
      let snapshot = null;
      try {
        await verifyWorkspaceRootIdentity({
          root: file.configuredRoot,
          expectedIdentity: file.authorizedRootIdentity,
        });
        snapshot = await createWorkspaceFileSnapshot({
          sourcePath: file.path,
          authorizedRoot: file.authorizedRoot,
          snapshotDirectory,
          maxBytes: WORKSPACE_MAX_BYTES,
          signal,
          onCleanupError: (error) =>
            log(
              `[workspace] snapshot handle cleanup failed: ${safeFailureCode(error)}`,
            ),
        });
        await verifyWorkspaceRootIdentity({
          root: file.configuredRoot,
          expectedIdentity: file.authorizedRootIdentity,
        });
      } catch (e) {
        throwIfWorkspaceAborted(signal);
        if (snapshot?.path) {
          try {
            await fs.promises.unlink(snapshot.path);
          } catch (cleanupError) {
            if (cleanupError?.code !== "ENOENT") {
              log(
                `[workspace] rejected snapshot cleanup deferred: ${safeFailureCode(cleanupError)}`,
              );
            }
          }
        }
        failedPaths.add(file.path);
        result.n_failed += 1;
        result.errors.push(
          `创建安全快照失败 ${file.name} [${safeFailureCode(e)}]`,
        );
        continue;
      }
      let intentDurable = false;
      let converged = false;
      try {
        if (prev?.sha256 && prev.sha256 === snapshot.sha256) {
          nextFiles[file.path] = {
            ...prev,
            mtime: snapshot.mtime,
            size: snapshot.size,
            sha256: snapshot.sha256,
          };
          result.n_skipped += 1;
          continue;
        }

        const previousDocId =
          typeof prev?.doc_id === "string" ? prev.doc_id.trim() : "";
        pendingUploads[file.path] = {
          snapshot_path: snapshot.path,
          sha256: snapshot.sha256,
          size: snapshot.size,
          mtime: snapshot.mtime,
          file_name: file.name,
          title: file.name,
          previous_doc_id: previousDocId,
          uploaded_doc_id: "",
          upload_started_at: null,
          queued_at: Date.now(),
        };
        try {
          // Upload is not started until its exact private snapshot and previous
          // mapping are durable. A lost response can then be retried through the
          // backend's content-hash idempotency contract.
          persist();
          intentDurable = true;
        } catch (error) {
          delete pendingUploads[file.path];
          throw error;
        }

        pendingUploads[file.path] = {
          ...pendingUploads[file.path],
          upload_started_at: Date.now(),
        };
        persist();

        const uploaded = await uploadWorkspaceFile(
          expectedOrigin,
          file,
          snapshot,
          signal,
        );
        const uploadedDocId = uploaded.doc_id.trim();
        const afterUpload = workspaceProjectionAfterUpload(
          workspaceScanCheckpoint(
            store,
            nextFiles,
            managedDocIds,
            pendingUploads,
            result,
          ),
          file.path,
          pendingUploads[file.path],
          uploaded,
        );
        replaceWorkspaceScanProjection(
          afterUpload,
          nextFiles,
          managedDocIds,
          pendingUploads,
        );
        persist();

        // Commit the new local mapping before cleaning up the previous remote
        // projection. If cleanup is interrupted or fails, the old doc id stays
        // in doc_ids as a durable orphan and is retried on the next scan.
        let previousDeleted = false;
        if (
          previousDocId &&
          previousDocId !== uploadedDocId &&
          workspaceDocReferenceCount(nextFiles, previousDocId) === 0
        ) {
          try {
            await deleteRemoteRagDoc(expectedOrigin, previousDocId, signal);
            previousDeleted = true;
          } catch (e) {
            throwIfWorkspaceAborted(signal);
            log(
              `[workspace] delete previous doc failed: ${safeFailureCode(e)}`,
            );
            result.n_failed += 1;
            result.errors.push(
              `清理旧索引失败 ${file.name} [${safeFailureCode(e)}]`,
            );
          }
        }
        const afterCleanup = workspaceProjectionAfterCleanup(
          workspaceScanCheckpoint(
            store,
            nextFiles,
            managedDocIds,
            pendingUploads,
            result,
          ),
          file.path,
          { previousDeleted },
        );
        replaceWorkspaceScanProjection(
          afterCleanup,
          nextFiles,
          managedDocIds,
          pendingUploads,
        );
        persist();
        converged = true;
        if (prev) result.n_updated += 1;
        else result.n_added += 1;
      } catch (e) {
        throwIfWorkspaceAborted(signal);
        result.n_failed += 1;
        result.errors.push(`入库失败 ${file.name} [${safeFailureCode(e)}]`);
      } finally {
        if (snapshot?.path && (!intentDurable || converged)) {
          try {
            await fs.promises.unlink(snapshot.path);
          } catch (error) {
            if (error?.code !== "ENOENT") {
              log(
                `[workspace] snapshot file cleanup deferred: ${safeFailureCode(error)}`,
              );
            }
          }
        }
      }
    }
    return result;
  } finally {
    result.duration_s = Number(((Date.now() - started) / 1000).toFixed(3));
    if (snapshotDirectory) {
      const retainedDirectories = new Set(
        workspacePendingSnapshotDirectories({ pending_uploads: pendingUploads }).map(
          (directory) => path.resolve(directory),
        ),
      );
      if (!retainedDirectories.has(path.resolve(snapshotDirectory))) {
        await cleanupWorkspaceSnapshotDirectory(snapshotDirectory, {
          logger: (message) => log(`[workspace] ${message}`),
        });
      }
    }
    persist();
  }
}

async function clearLocalWorkspaceDocs(expectedOrigin, signal, leaseEpoch) {
  const store = readLocalWorkspaceStore(expectedOrigin);
  let removed = 0;
  const nextFiles = { ...(store.files || {}) };
  const remainingDocIds = new Set(store.doc_ids || []);
  const pendingUploads = { ...(store.pending_uploads || {}) };
  const result = { errors: [], n_failed: 0, n_added: 0, n_updated: 0 };
  let snapshotDirectory = null;
  const persist = () => {
    assertWorkspaceOperationCurrent(expectedOrigin, leaseEpoch, signal);
    writeLocalWorkspaceStore(
      expectedOrigin,
      workspaceScanCheckpoint(
        store,
        nextFiles,
        remainingDocIds,
        pendingUploads,
        result,
      ),
    );
  };
  try {
    snapshotDirectory = await createWorkspaceSnapshotDirectory();
    const prepared = prepareWorkspaceUploadsForClear(
      workspaceScanCheckpoint(
        store,
        nextFiles,
        remainingDocIds,
        pendingUploads,
        result,
      ),
    );
    replaceWorkspaceScanProjection(
      prepared.state,
      nextFiles,
      remainingDocIds,
      pendingUploads,
    );
    if (Object.keys(pendingUploads).length > 0 || prepared.abandonedSnapshotPaths.length > 0) {
      persist();
    }
    for (const snapshotPath of prepared.abandonedSnapshotPaths) {
      await removeRetainedWorkspaceSnapshot(snapshotPath);
    }
    await recoverPendingWorkspaceUploads({
      expectedOrigin,
      store,
      nextFiles,
      managedDocIds: remainingDocIds,
      pendingUploads,
      snapshotDirectory,
      signal,
      persist,
      result,
    });
    for (const docId of Array.from(remainingDocIds)) {
      throwIfWorkspaceAborted(signal);
      try {
        await deleteRemoteRagDoc(expectedOrigin, docId, signal);
        remainingDocIds.delete(docId);
        for (const [sourcePath, pending] of Object.entries(pendingUploads)) {
          if (
            pending.previous_doc_id === docId ||
            pending.uploaded_doc_id === docId
          ) {
            pendingUploads[sourcePath] = {
              ...pending,
              previous_doc_id:
                pending.previous_doc_id === docId ? "" : pending.previous_doc_id,
              uploaded_doc_id:
                pending.uploaded_doc_id === docId ? "" : pending.uploaded_doc_id,
            };
          }
        }
        removed += 1;
      } catch (e) {
        throwIfWorkspaceAborted(signal);
        log(`[workspace] delete managed doc failed: ${safeFailureCode(e)}`);
        result.errors.push(`删除索引失败 [${safeFailureCode(e)}]`);
      }
    }
    return { n_removed: removed };
  } finally {
    const remainingFiles = Object.fromEntries(
      Object.entries(nextFiles).filter(([, metadata]) =>
        remainingDocIds.has(metadata?.doc_id),
      ),
    );
    for (const key of Object.keys(nextFiles)) delete nextFiles[key];
    Object.assign(nextFiles, remainingFiles);
    if (snapshotDirectory) {
      await cleanupWorkspaceSnapshotDirectory(snapshotDirectory, {
        logger: (message) => log(`[workspace] ${message}`),
      });
    }
    persist();
  }
}

const workspaceOperationEpochs = new Map();
const activeWorkspaceOperations = new Map();
const workspaceMutationTails = new Map();

function rendererExpectedMainBackendOrigin(event, context) {
  assertTrustedIpcOrigin(event);
  const rawExpectedOrigin = context?.expectedBackendOrigin;
  if (typeof rawExpectedOrigin !== "string" || !rawExpectedOrigin.trim()) {
    const error = new Error("workspace IPC requires an expected backend origin");
    error.code = "WORKSPACE_BACKEND_ORIGIN_REQUIRED";
    throw error;
  }
  let expectedOrigin;
  try {
    const parsed = new URL(rawExpectedOrigin);
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== "/" ||
      parsed.search ||
      parsed.hash
    ) {
      throw new Error("not an HTTP(S) origin");
    }
    expectedOrigin = parsed.origin;
  } catch (cause) {
    const error = new Error("workspace IPC received an invalid backend origin", {
      cause,
    });
    error.code = "WORKSPACE_BACKEND_ORIGIN_INVALID";
    throw error;
  }
  const mainOrigin = new URL(BACKEND_HOST).origin;
  if (expectedOrigin !== mainOrigin) {
    const error = new Error(
      "renderer backend origin no longer matches the Electron main process",
    );
    error.code = "WORKSPACE_BACKEND_ORIGIN_MISMATCH";
    throw error;
  }
  return expectedOrigin;
}

function workspaceExpectedOrigin(event, context) {
  const expectedOrigin = rendererExpectedMainBackendOrigin(event, context);
  return workspaceBackendTransport().assertExpectedOrigin(expectedOrigin);
}

function workspaceOperationEpoch(expectedOrigin) {
  return workspaceOperationEpochs.get(expectedOrigin) || 0;
}

function assertWorkspaceOperationCurrent(expectedOrigin, leaseEpoch, signal) {
  throwIfWorkspaceAborted(signal);
  if (leaseEpoch !== workspaceOperationEpoch(expectedOrigin)) {
    throw new DOMException("workspace origin lease expired", "AbortError");
  }
}

function cancelWorkspaceOperations(expectedOrigin) {
  workspaceOperationEpochs.set(
    expectedOrigin,
    workspaceOperationEpoch(expectedOrigin) + 1,
  );
  const controllers = activeWorkspaceOperations.get(expectedOrigin);
  if (!controllers) return 0;
  for (const controller of controllers) {
    controller.abort(new DOMException("backend origin changed", "AbortError"));
  }
  return controllers.size;
}

async function runWorkspaceMutation(expectedOrigin, operation) {
  const leaseEpoch = workspaceOperationEpoch(expectedOrigin);
  const previous = workspaceMutationTails.get(expectedOrigin) || Promise.resolve();
  const running = previous.catch(() => undefined).then(async () => {
    assertWorkspaceOperationCurrent(expectedOrigin, leaseEpoch);
    const controller = new AbortController();
    let controllers = activeWorkspaceOperations.get(expectedOrigin);
    if (!controllers) {
      controllers = new Set();
      activeWorkspaceOperations.set(expectedOrigin, controllers);
    }
    controllers.add(controller);
    try {
      const value = await operation(controller.signal, leaseEpoch);
      assertWorkspaceOperationCurrent(
        expectedOrigin,
        leaseEpoch,
        controller.signal,
      );
      return value;
    } finally {
      controllers.delete(controller);
      if (controllers.size === 0) activeWorkspaceOperations.delete(expectedOrigin);
    }
  });
  workspaceMutationTails.set(expectedOrigin, running.catch(() => undefined));
  return running;
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
      log(`[updates] emit failed: ${safeFailureCode(e)}`);
    }
  }
}

function safeUpdateFailure(error, fallback = "更新服务暂时不可用") {
  return `${fallback} [${safeFailureCode(error)}]`;
}

function installedMacBundlePath() {
  if (process.platform !== "darwin") return null;
  const marker = `${path.sep}Contents${path.sep}MacOS${path.sep}`;
  const index = process.execPath.lastIndexOf(marker);
  return index > 0 ? process.execPath.slice(0, index) : null;
}

function getAppUpdateManager() {
  if (appUpdateManager) return appUpdateManager;
  appUpdateManager = createAppUpdateManager({
    owner: RELEASE_OWNER,
    repo: RELEASE_REPO,
    currentVersion: app.getVersion(),
    platform: process.platform,
    channel: process.env.ECHODESK_UPDATE_CHANNEL === "stable"
      ? "stable"
      : "preview",
    tempRoot: path.join(app.getPath("temp"), "echodesk-updates"),
    helperPath: path.join(__dirname, "detached-updater.cjs"),
    executablePath: process.execPath,
    currentPid: process.pid,
    currentBundlePath: installedMacBundlePath(),
    emit: emitUpdateStatus,
    quit: () => {
      quittingForReal = true;
      app.quit();
    },
  });
  return appUpdateManager;
}

async function checkForUpdatesWithFallback() {
  try {
    return await getAppUpdateManager().check();
  } catch (e) {
    const failure = {
      status: "error",
      currentVersion: app.getVersion(),
      latestVersion: null,
      updateAvailable: false,
      releaseUrl: RELEASES_URL,
      assetName: null,
      assetUrl: null,
      canAutoInstall: false,
      error: safeUpdateFailure(e),
    };
    emitUpdateStatus(failure);
    return failure;
  }
}

async function runScheduledUpdateCheck(reason) {
  if (updateCheckInFlight || shuttingDown || quittingForReal) return;
  updateCheckInFlight = true;
  try {
    log(`[updates] scheduled check: ${reason}`);
    await checkForUpdatesWithFallback();
  } finally {
    updateCheckInFlight = false;
  }
}

function scheduleStartupUpdateCheck() {
  if (IS_DEV || process.env.ECHODESK_DISABLE_AUTO_UPDATE_CHECK === "1") {
    return;
  }
  setTimeout(() => {
    if (shuttingDown || quittingForReal) return;
    void runScheduledUpdateCheck("startup");
    if (!updateCheckTimer) {
      updateCheckTimer = setInterval(() => {
        void runScheduledUpdateCheck("interval");
      }, AUTO_UPDATE_CHECK_INTERVAL_MS);
    }
  }, AUTO_UPDATE_CHECK_DELAY_MS);
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
  return resolveShareBackendBase(BACKEND_ENDPOINT, {
    shareBaseUrl: process.env.ECHO_SHARE_BASE_URL,
    lanAddress: firstLanAddress(),
    allowLan: BACKEND_ENDPOINT.role !== "public_service",
  });
}

function projectRoot() {
  // dev: desktop/electron/main.cjs → desktop/.. = echo-demo repo root
  return path.resolve(__dirname, "..", "..");
}

// backend 工作目录解析。prod (asar) 下 __dirname 在 asar 虚拟路径，
// 不能作 child_process.spawn 的 cwd（uvicorn 启动期会 chdir 失败）。
// 源码开发只允许显式工作目录或当前 checkout；安装包永不进入源码路径。
function resolveBackendCwd() {
  const cands = [
    process.env.ECHO_BACKEND_CWD,
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
  return cands[0];
}

function bundledBackendPath() {
  const name = process.platform === "win32" ? "echodesk-backend.exe" : "echodesk-backend";
  return path.join(process.resourcesPath, "backend", name);
}

function bundledBackendExecutable() {
  if (!app.isPackaged) return null;
  const candidate = bundledBackendPath();
  try {
    fs.accessSync(candidate, fs.constants.X_OK);
    return candidate;
  } catch (error) {
    log(`[backend] bundled executable unavailable [${safeFailureCode(error)}]`);
    return null;
  }
}

function refusePackagedSourceFallback() {
  if (!app.isPackaged) return false;
  emitStatus({
    state: "bundled-backend-unavailable",
    reason: "packaged backend is missing or not executable",
    searched: [bundledBackendPath()],
    help_url: "docs/INSTALL.md",
  });
  return true;
}

function sanitizedWindowsPackagedBackendEnv(baseEnv) {
  const blockedPrefixes = [
    "ELECTRON_",
    "NODE_",
    "NPM_",
    "PYTHON",
  ];
  const blockedExact = new Set([
    "INIT_CWD",
    "npm_config_node_gyp",
    "npm_execpath",
    "npm_lifecycle_event",
    "npm_lifecycle_script",
    "npm_node_execpath",
    "VIRTUAL_ENV",
  ].map((key) => key.toUpperCase()));
  const clean = {};
  for (const [key, value] of Object.entries(baseEnv || {})) {
    const upper = key.toUpperCase();
    if (blockedExact.has(upper)) continue;
    if (blockedPrefixes.some((prefix) => upper.startsWith(prefix))) continue;
    clean[key] = value;
  }
  return clean;
}

// ---------- Python 解析（P1.6） ----------

// 源码开发只允许显式绝对路径或当前 checkout 的专属 venv；不扫描 HOME、系统
// Python 或 PATH，安装包也不会进入该分支。
function pythonCandidates() {
  const cands = [];
  const explicit = String(process.env.ECHO_PYTHON || "").trim();
  if (explicit && path.isAbsolute(explicit)) cands.push(explicit);
  cands.push(
    path.join(
      projectRoot(),
      "backend",
      ".venv",
      process.platform === "win32" ? "Scripts" : "bin",
      process.platform === "win32" ? "python.exe" : "python",
    ),
  );
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
        log(`[backend] python resolved candidate=${searched.length}`);
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
  const rendererPayload = projectBackendStatusForRenderer(payload);
  lastStatus = rendererPayload;
  log(`[backend] status -> ${JSON.stringify(rendererPayload)}`);
  if (mainWindow && !mainWindow.isDestroyed() && rendererReady) {
    try {
      mainWindow.webContents.send("backend:status", rendererPayload);
    } catch (e) {
      log(`[backend] emit failed: ${e.message}`);
    }
  }
}

// ---------- 健康检查 ----------

function healthzOnce() {
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

function expectedLocalBackendContract() {
  if (PUBLIC_DEMO_MODE) return Promise.resolve(null);
  if (expectedLocalBackendContractPromise) {
    return expectedLocalBackendContractPromise;
  }
  const bundledBackend = bundledBackendExecutable();
  const sourceCwd = bundledBackend ? null : resolveBackendCwd();
  expectedLocalBackendContractPromise = expectedBackendContract({
    productVersion: DESKTOP_PRODUCT_VERSION,
    bundledBackendPath: bundledBackend,
    sourceAppPath: sourceCwd ? path.join(sourceCwd, "app") : null,
  });
  return expectedLocalBackendContractPromise;
}

async function performBackendHealthcheck() {
  if (PUBLIC_DEMO_MODE) return healthzOnce();
  try {
    const expected = await expectedLocalBackendContract();
    await probeBackendContract(BACKEND_HOST, expected);
    lastBackendContractFailure = null;
    return true;
  } catch (error) {
    const failure =
      error instanceof BackendContractError
        ? error.code
        : safeFailureCode(error);
    if (lastBackendContractFailure !== failure) {
      log(`[backend] contract probe rejected [${failure}]`);
    }
    lastBackendContractFailure = failure;
    return false;
  }
}

function healthcheckOnce() {
  if (backendHealthcheckPromise) return backendHealthcheckPromise;
  const pending = performBackendHealthcheck();
  backendHealthcheckPromise = pending;
  void pending.finally(() => {
    if (backendHealthcheckPromise === pending) backendHealthcheckPromise = null;
  });
  return pending;
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
        if (!startFusedWorkerBridge()) {
          // 融合 worker 是本地 HTTP backend 之上的独立能力。打包资源缺失时，
          // 融合任务继续 fail closed，但不能杀死健康 backend 并触发无限重启。
          return;
        }
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

function startFusedWorkerBridge() {
  // Development keeps its source-runtime test harness. Only the packaged
  // release path claims this resource-bound fused bridge.
  if (PUBLIC_DEMO_MODE || IS_DEV) return true;
  if (fusedWorkerBridge) return true;
  const duplex = backendProc?.stdio?.[3];
  if (!duplex || !fusedWorkerNonce) {
    emitStatus({
      state: "degraded",
      port: BACKEND_PORT,
      reason: "supervised backend has no inherited fused-runtime handle",
    });
    return false;
  }
  try {
    fusedWorkerBridge = startPackagedFusedWorkerBridge({
      duplex,
      nonce: fusedWorkerNonce,
      resourcesPath: process.resourcesPath,
    });
    log("[runtime] packaged fused worker bridge connected");
    return true;
  } catch (error) {
    fusedWorkerBridge = null;
    emitStatus({
      state: "degraded",
      port: BACKEND_PORT,
      reason: `packaged fused worker unavailable: ${safeFailureCode(error)}`,
    });
    log(`[runtime] packaged fused worker bridge rejected [${safeFailureCode(error)}]`);
    return false;
  }
}

function stopFusedWorkerBridge() {
  const bridge = fusedWorkerBridge;
  fusedWorkerBridge = null;
  fusedWorkerNonce = null;
  try {
    bridge?.close?.();
  } catch (error) {
    log(`[runtime] fused worker bridge stop failed [${safeFailureCode(error)}]`);
  }
}

function stopHealthWatcher() {
  if (healthTimer) {
    clearInterval(healthTimer);
    healthTimer = null;
  }
}

function stopPublicBackendHealthWatcher() {
  if (publicBackendHealthTimer) {
    clearInterval(publicBackendHealthTimer);
    publicBackendHealthTimer = null;
  }
}

// Public service 是正式的远端业务路由；只观察健康状态，不接管本机 daemon。
function startPublicBackendHealthWatcher() {
  if (publicBackendHealthTimer) return;
  publicBackendHealthTimer = setInterval(async () => {
    if (shuttingDown) return;
    const ok = await healthcheckOnce();
    if (ok) {
      emitStatus({ state: "ready", mode: "public-service" });
      return;
    }
    emitStatus({
      state: "degraded",
      reason: "public backend unhealthy",
      attempts: 0,
      last_error: "healthz failed",
    });
  }, HEALTH_INTERVAL_MS);
}

function attachPublicBackend() {
  emitStatus({ state: "connecting", mode: "public-service" });
  void healthcheckOnce().then((ok) => {
    if (shuttingDown || !PUBLIC_DEMO_MODE) return;
    if (ok) {
      emitStatus({ state: "ready", mode: "public-service" });
    } else {
      emitStatus({
        state: "degraded",
        reason: "public backend unhealthy",
        attempts: 0,
        last_error: "healthz failed",
      });
    }
    startPublicBackendHealthWatcher();
  });
}

// ---------- 进程生命周期 ----------

async function killBackendProc() {
  stopFusedWorkerBridge();
  if (!backendProc || backendProc.exitCode !== null) {
    backendProc = null;
    return;
  }
  const proc = backendProc;
  backendProc = null;
  try {
    await stopBackendProcess(proc, { graceMs: SIGKILL_GRACE_MS });
  } catch (error) {
    log(`[backend] stop failed during recovery: ${safeFailureCode(error)}`);
  }
}

function stopBackendProcForRestart() {
  stopFusedWorkerBridge();
  if (!backendProc || backendProc.exitCode !== null) {
    backendProc = null;
    return Promise.resolve();
  }
  const proc = backendProc;
  backendProc = null;
  return stopBackendProcess(proc, { graceMs: SIGKILL_GRACE_MS });
}

async function handleBackendDeath(reason) {
  if (shuttingDown) return;
  stopHealthWatcher();
  await killBackendProc();

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
  const lifecycleGeneration = backendLifecycleGeneration;
  setTimeout(() => {
    if (shuttingDown || lifecycleGeneration !== backendLifecycleGeneration) return;
    spawnBackendAndWatch();
  }, backoff);
}

function spawnBackendAndWatch() {
  if (shuttingDown) return;
  if (backendProc && backendProc.exitCode === null && !backendProc.killed) {
    log("[backend] spawn ignored because a supervised child is already running");
    return;
  }

  if (!SPAWN_BACKEND) {
    log("[backend] local backend spawn is disabled; refusing unmanaged backend fallback");
    emitStatus({
      state: "backend-spawn-disabled",
      reason: "local backend must be supervised by the current EchoDesk process",
      attempts: 0,
    });
    return;
  }

  // 端口被其它进程占用时必须 fail closed，不能把未知 daemon 当作本应用 backend。
  if (isPortListening(BACKEND_PORT)) {
    log(`[backend] port ${BACKEND_PORT} is already occupied; refusing unmanaged backend`);
    emitStatus({
      state: "backend-port-conflict",
      reason: "backend port is occupied by an unmanaged process",
      attempts: 0,
    });
    return;
  }

  const bundledBackend = bundledBackendExecutable();
  if (!bundledBackend && refusePackagedSourceFallback()) {
    return;
  }
  // 开发/源码安装仍走 Python；打包安装优先运行随安装器携带的 backend executable。
  if (!bundledBackend && (!pythonResolved || !pythonResolved.python)) {
    pythonResolved = resolvePython();
  }
  if (!bundledBackend && !pythonResolved.python) {
    emitStatus({
      state: "python-not-found",
      searched: pythonResolved.searched,
      help_url: "docs/INSTALL.md",
    });
    return;
  }

  const cwd = bundledBackend ? path.dirname(bundledBackend) : resolveBackendCwd();
  if (!bundledBackend && (!cwd || !fs.existsSync(path.join(cwd, "app", "main.py")))) {
    emitStatus({
      state: "backend-source-not-found",
      searched: [
        process.env.ECHO_BACKEND_CWD,
        path.join(projectRoot(), "backend"),
      ].filter(Boolean),
      help_url: "docs/INSTALL.md",
    });
    return;
  }
  emitStatus({ state: "starting" });
  const executable = bundledBackend || pythonResolved.python;
  const args = bundledBackend
    ? [
        "--host",
        BACKEND_BIND_HOST,
        "--port",
        String(BACKEND_PORT),
        "--ws-max-size",
        "4096",
        "--log-level",
        "info",
      ]
    : [
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        BACKEND_BIND_HOST,
        "--port",
        String(BACKEND_PORT),
        "--ws-max-size",
        "4096",
        "--log-level",
        "info",
      ];
  log(`[backend] spawn mode=${bundledBackend ? "bundled" : "source"} port=${BACKEND_PORT}`);
  log(
    `[backend] spawn executable=${JSON.stringify(executable)} cwd=${JSON.stringify(cwd)} args=${JSON.stringify(args)}`,
  );

  try {
    const enablePackagedRuntimeBridge = true;
    fusedWorkerNonce = enablePackagedRuntimeBridge
      ? randomBytes(32).toString("hex")
      : null;
    const backendStdio = enablePackagedRuntimeBridge
      ? ["ignore", "pipe", "pipe", "pipe"]
      : ["ignore", "pipe", "pipe"];
    const inheritedBackendEnv = (bundledBackend && process.platform === "win32")
      ? sanitizedWindowsPackagedBackendEnv(process.env)
      : process.env;
    backendProc = spawn(
      executable,
      args,
      {
        cwd,
        env: {
          ...inheritedBackendEnv,
          // The packaged app already contains a platform-correct Node runtime.
          // The backend reuses it for deterministic PPT rendering instead of
          // requiring users to install node/npm separately. Windows packaged
          // builds must keep the bridge too; otherwise STT/LLM stay healthy but
          // document/PPT agent generation loses its bundled runtime.
          ...(enablePackagedRuntimeBridge
            ? electronNodeRuntimeEnvironment(process.execPath)
            : {}),
          // The supervisor-selected endpoint is authoritative for both the
          // uvicorn socket and backend Settings/health/bootstrap diagnostics.
          PORT: String(BACKEND_PORT),
          PUBLIC_HTTP_URL:
            process.env.PUBLIC_HTTP_URL || LOCAL_BACKEND_HOST,
          PUBLIC_WS_URL:
            process.env.PUBLIC_WS_URL ||
            `${LOCAL_BACKEND_HOST.replace(/^http/, "ws")}/ws/echo`,
          // localhost 流量走代理会导致 uvicorn 自己 GET healthz 都失败
          HTTP_PROXY: "",
          HTTPS_PROXY: "",
          ALL_PROXY: "",
          http_proxy: "",
          https_proxy: "",
          all_proxy: "",
          // The local production backend consumes this inherited duplex for the
          // packaged fused runtime used by document/PPT agent generation.
          ...(enablePackagedRuntimeBridge
            ? {
                ECHODESK_RUNTIME_FD: "3",
                ECHODESK_RUNTIME_NONCE: fusedWorkerNonce,
              }
            : {}),
        },
        // Keep fd 3 for the packaged runtime bridge. Backend stdout/stderr are
        // drained below; Windows packaged GUI apps do not have a stable console,
        // so bytes go to files instead of process.stdout/process.stderr there.
        stdio: backendStdio,
        windowsHide: true,
      },
    );
  } catch (e) {
    log(`[backend] spawn threw [${safeFailureCode(e)}]`);
    backendProc = null;
    handleBackendDeath("spawn failed");
    return;
  }

  // ENOENT / EACCES 走 'error' 事件而不是 throw；不挂监听 electron 会判 fatal
  backendProc.on("error", (err) => {
    log(`[backend] spawn error [${safeFailureCode(err)}]`);
  });
  backendProc.stdout?.on("data", (b) => {
    if (bundledBackend && process.platform === "win32") {
      appendBackendSupervisorLog("stdout", b);
      return;
    }
    process.stdout.write(`[backend] ${b.toString()}`);
  });
  backendProc.stderr?.on("data", (b) => {
    if (bundledBackend && process.platform === "win32") {
      appendBackendSupervisorLog("stderr", b);
      return;
    }
    process.stderr.write(`[backend] ${b.toString()}`);
  });
  backendProc.on("exit", (code, signal) => {
    const wasOurs = backendProc !== null; // killBackendProc 会先置 null
    log(`[backend] child exited code=${code} signal=${signal} ours=${wasOurs}`);
    stopFusedWorkerBridge();
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
    if (PUBLIC_DEMO_MODE) {
      log(`[backend] local spawn disabled; connecting to public service ${BACKEND_HOST}`);
      attachPublicBackend();
    } else {
      log("[backend] local backend spawn disabled; refusing unmanaged backend fallback");
      emitStatus({
        state: "backend-spawn-disabled",
        reason: "local backend must be supervised by the current EchoDesk process",
        attempts: 0,
      });
    }
    return;
  }

  if (bundledBackendExecutable()) {
    spawnBackendAndWatch();
    return;
  }

  if (refusePackagedSourceFallback()) {
    return;
  }

  // P1.6: 源码安装启动第一步验证 Python 存在；找不到就直接 emit python-not-found
  // 不 spawn uvicorn 是为了避免 ENOENT 太晚才暴露
  pythonResolved = resolvePython();
  if (!pythonResolved.python) {
    log(`[backend] python not found searched_count=${pythonResolved.searched.length}`);
    emitStatus({
      state: "python-not-found",
      searched: pythonResolved.searched,
      help_url: "docs/INSTALL.md",
    });
    return;
  }
  spawnBackendAndWatch();
}

function validatedExternalHttpsUrl(rawUrl) {
  let target;
  try {
    target = new URL(String(rawUrl || ""));
  } catch (cause) {
    const error = new Error("valid HTTPS URL required", { cause });
    error.code = "EXTERNAL_URL_INVALID";
    throw error;
  }
  if (
    target.protocol !== "https:" ||
    target.username ||
    target.password
  ) {
    const error = new Error("valid HTTPS URL required");
    error.code = "EXTERNAL_URL_INVALID";
    throw error;
  }
  return target.href;
}

function openExternalHttps(rawUrl) {
  return shell.openExternal(validatedExternalHttpsUrl(rawUrl));
}

function sendBackgroundCommand(command) {
  if (!mainWindow || mainWindow.isDestroyed() || !rendererReady) return false;
  try {
    mainWindow.webContents.send("background:command", command);
    return true;
  } catch (error) {
    log(`[background] command failed [${safeFailureCode(error)}]`);
    return false;
  }
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    void createWindow({ showOnReady: true }).catch((error) => {
      console.error("[app] failed to recreate the main window:", error);
    });
    return;
  }
  if (process.platform === "darwin") {
    void app.dock?.show?.();
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function hideMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.hide();
  if (process.platform === "darwin") {
    app.dock?.hide?.();
  }
}

function loginItemSettings() {
  if (process.platform !== "darwin" && process.platform !== "win32") {
    return { supported: false, openAtLogin: false };
  }
  const settings = app.getLoginItemSettings();
  return {
    supported: true,
    openAtLogin: settings.openAtLogin === true,
  };
}

function setOpenAtLogin(openAtLogin) {
  if (process.platform !== "darwin" && process.platform !== "win32") {
    return loginItemSettings();
  }
  const options = { openAtLogin: openAtLogin === true };
  if (process.platform === "win32") {
    options.path = process.execPath;
    options.args = ["--hidden"];
  } else {
    options.openAsHidden = true;
  }
  app.setLoginItemSettings(options);
  rebuildTrayMenu();
  return loginItemSettings();
}

function requestExplicitQuit() {
  if (shuttingDown || quittingForReal) return;
  app.quit();
}

function trayIcon() {
  const source = nativeImage.createFromPath(
    path.join(__dirname, "icons", "echodesk.png"),
  );
  if (source.isEmpty()) return source;
  const icon = source.resize({ width: 18, height: 18 });
  if (process.platform === "darwin") icon.setTemplateImage(true);
  return icon;
}

function rebuildTrayMenu() {
  if (!tray || tray.isDestroyed()) return;
  const login = loginItemSettings();
  tray.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: "显示 EchoDesk",
        click: showMainWindow,
      },
      { type: "separator" },
      {
        label: formalMeetingStatusLabel(backgroundStatus),
        enabled: false,
      },
      {
        label: captureStatusLabel(backgroundStatus),
        enabled: false,
      },
      {
        label: backgroundStatus.freeModeEnabled
          ? "暂停自由收音"
          : "恢复自由收音",
        click: () =>
          sendBackgroundCommand(
            backgroundStatus.freeModeEnabled ? "pause" : "resume",
          ),
      },
      { type: "separator" },
      {
        label: "检查更新",
        click: () => {
          void runScheduledUpdateCheck("tray");
        },
      },
      {
        label: "登录时启动",
        type: "checkbox",
        visible: login.supported,
        checked: login.openAtLogin,
        click: (item) => {
          setOpenAtLogin(item.checked);
        },
      },
      { type: "separator" },
      {
        label: "退出 EchoDesk",
        click: requestExplicitQuit,
      },
    ]),
  );
  tray.setToolTip(
    `${formalMeetingStatusLabel(backgroundStatus)} · ${captureStatusLabel(backgroundStatus)}`,
  );
}

function ensureTray() {
  if (tray && !tray.isDestroyed()) return tray;
  tray = new Tray(trayIcon());
  tray.setTitle("");
  tray.on("click", showMainWindow);
  tray.on("double-click", showMainWindow);
  rebuildTrayMenu();
  return tray;
}

async function createWindow({ showOnReady = true } = {}) {
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
      sandbox: true,
    },
  });

  mainWindow.once("ready-to-show", () => {
    if (showOnReady) {
      showMainWindow();
    } else {
      hideMainWindow();
    }
  });

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
        log(`[updates] replay failed: ${safeFailureCode(e)}`);
      }
    }
  });

  mainWindow.on("close", (event) => {
    if (shuttingDown || quittingForReal) return;
    if (SMOKE_EXIT_ON_WINDOW_CLOSE) {
      event.preventDefault();
      requestExplicitQuit();
      return;
    }
    event.preventDefault();
    hideMainWindow();
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
    rendererReady = false;
  });

  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (isTrustedAppRendererUrl(url)) return;
    event.preventDefault();
    try {
      void openExternalHttps(url);
    } catch {
      // Untrusted navigation remains denied without handing a custom scheme to OS.
    }
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    try {
      void openExternalHttps(url);
    } catch {
      // Invalid/non-HTTPS popups are denied below.
    }
    return { action: "deny" };
  });

  try {
    if (IS_DEV) {
      await mainWindow.loadURL(VITE_URL);
    } else {
      await mainWindow.loadURL(APP_ENTRY_URL);
    }
  } catch (error) {
    const currentUrl = mainWindow?.webContents.getURL() || "";
    const initialNavigationWasReloaded =
      error?.code === "ERR_ABORTED" && isTrustedAppRendererUrl(currentUrl);
    if (!initialNavigationWasReloaded) throw error;
    log(`[renderer] initial navigation was replaced by a trusted reload: ${currentUrl}`);
  }
}

// ---------- IPC handlers ----------

function backendRoutingSnapshot() {
  return Object.freeze({
    runtimeMode: BACKEND_ENDPOINT.runtimeMode,
    principalMode: BACKEND_ENDPOINT.principalMode,
    role: BACKEND_ENDPOINT.role,
    source: BACKEND_ENDPOINT.source,
    schemaVersion: BACKEND_ENDPOINT.schemaVersion,
    backendBase: BACKEND_HOST,
    publicServiceEndpoint: BACKEND_ENDPOINT.publicServiceEndpoint,
    pairedHubSyncGatewayEndpoint: null,
    localDevDiagnosticEndpoint: BACKEND_ENDPOINT.localDevDiagnosticEndpoint,
  });
}

ipcMain.handle("echo:backend-host", (event) => {
  assertTrustedIpcOrigin(event);
  return BACKEND_HOST;
});

// B05M model identity is published by the kernel/gateway owner and projected
// read-only to Settings. Renderer input can never become model identity.
const modelRuntimeIpc = createModelRuntimeIpcSurface({
  ipcMain,
  assertTrustedIpcOrigin,
  sendToRenderers(channel, payload) {
    for (const window of BrowserWindow.getAllWindows()) {
      if (!window.isDestroyed()) window.webContents.send(channel, payload);
    }
  },
});
modelRuntimeIpc.register();

ipcMain.handle("echo:backend-routing", (event) => {
  assertTrustedIpcOrigin(event);
  return backendRoutingSnapshot();
});
ipcMain.handle("echo:backend-contract", async (event) => {
  assertTrustedIpcOrigin(event);
  return expectedLocalBackendContract();
});
ipcMain.handle("echo:share-backend-host", (event) => {
  assertTrustedIpcOrigin(event);
  return shareBackendHost();
});
ipcMain.on("echo:backend-host-sync", (event) => {
  assertTrustedIpcOrigin(event);
  event.returnValue = BACKEND_HOST;
});
ipcMain.on("echo:backend-routing-sync", (event) => {
  assertTrustedIpcOrigin(event);
  event.returnValue = backendRoutingSnapshot();
});
ipcMain.on("echo:is-public-demo", (event) => {
  assertTrustedIpcOrigin(event);
  event.returnValue = PUBLIC_DEMO_MODE;
});

ipcMain.handle("credential:ensure-session", async (event) => {
  assertTrustedIpcOrigin(event);
  return ensurePublicSessionInMain();
});

ipcMain.handle("credential:renew-session", async (event) => {
  assertTrustedIpcOrigin(event);
  return renewPublicSessionFromCredential();
});

ipcMain.handle("credential:rotate", async (event, sessionToken) => {
  assertTrustedIpcOrigin(event);
  if (typeof sessionToken !== "string" || sessionToken.length < 20) {
    throw new Error("valid session token required for credential rotation");
  }
  return rotatePublicCredential(sessionToken);
});

ipcMain.handle("credential:clear-public", async (event) => {
  assertTrustedIpcOrigin(event);
  clearPublicCredential();
  return { cleared: true };
});

ipcMain.handle("echo:load-local-legacy-history", async (event) => {
  assertTrustedIpcOrigin(event);
  if (process.platform !== "darwin") return null;
  try {
    return loadLegacyEchodeskHistory();
  } catch (e) {
    log(`[legacy-history] import failed: ${safeFailureCode(e)}`);
    return {
      schema: 1,
      appVersion: app.getVersion(),
      savedAt: new Date().toISOString(),
      currentMeetingId: null,
      meetings: [],
      ambientSegments: [],
      artifacts: [],
      error: "legacy history import failed",
    };
  }
});

ipcMain.handle("shell:open-external", async (event, url) => {
  assertTrustedIpcOrigin(event);
  await openExternalHttps(url);
  return { ok: true };
});

ipcMain.handle("updates:check", async (event) => {
  assertTrustedIpcOrigin(event);
  return checkForUpdatesWithFallback();
});

ipcMain.handle("updates:last-status", async (event) => {
  assertTrustedIpcOrigin(event);
  return lastUpdateStatus;
});

ipcMain.handle("updates:download-and-install", async (event) => {
  assertTrustedIpcOrigin(event);
  if (IS_DEV) {
    await openExternalHttps(RELEASES_URL);
    return { ok: false, reason: "manual-release-page", releaseUrl: RELEASES_URL };
  }
  try {
    await getAppUpdateManager().download();
    return await getAppUpdateManager().install();
  } catch (e) {
    emitUpdateStatus({
      status: "error",
      error: safeUpdateFailure(e, "更新包下载或安装失败"),
      canAutoInstall: false,
    });
    const error = new Error("更新包下载或安装失败");
    error.code = safeFailureCode(e);
    throw error;
  }
});

ipcMain.handle("updates:open-release", async (event) => {
  assertTrustedIpcOrigin(event);
  await openExternalHttps(RELEASES_URL);
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

ipcMain.handle("mic:status", (event) => {
  assertTrustedIpcOrigin(event);
  if (process.platform !== "darwin") return "unknown";
  try {
    return systemPreferences.getMediaAccessStatus("microphone");
  } catch (e) {
    log(`[mic] getMediaAccessStatus failed: ${safeFailureCode(e)}`);
    return "unknown";
  }
});

ipcMain.handle("mic:request", async (event) => {
  assertTrustedIpcOrigin(event);
  if (process.platform !== "darwin") return false;
  try {
    return await systemPreferences.askForMediaAccess("microphone");
  } catch (e) {
    log(`[mic] askForMediaAccess failed: ${safeFailureCode(e)}`);
    return false;
  }
});

function localArtifactRoots() {
  return [
    process.env.SKILL_EXECUTOR_BUILD_DIR || "~/.echodesk/skill_build",
    process.env.STORAGE_DIR || "~/.echodesk/storage",
  ].map((root) => path.resolve(expandHome(root)));
}

// 只允许本机 backend 受控生成根下的真实普通文件。public runtime 的
// file_path 属于远端服务器，既无本机语义，也绝不能成为 renderer 选择本机文件的通道。
ipcMain.handle("echo:open-artifact-in-system", async (event, filePath) => {
  assertTrustedIpcOrigin(event);
  if (PUBLIC_DEMO_MODE) {
    const error = new Error("remote artifacts must be downloaded through the backend");
    error.code = "ARTIFACT_LOCAL_OPEN_DISABLED";
    throw error;
  }
  let controlledPath;
  try {
    controlledPath = resolveControlledLocalArtifactPath(
      filePath,
      localArtifactRoots(),
    );
  } catch (cause) {
    log(`[artifact] path denied: ${safeFailureCode(cause)}`);
    const error = new Error("artifact file is unavailable");
    error.code = cause?.code || "ARTIFACT_PATH_DENIED";
    throw error;
  }
  try {
    const err = await shell.openPath(controlledPath);
    if (err) {
      const error = new Error("system application could not open the artifact");
      error.code = "ARTIFACT_SYSTEM_OPEN_FAILED";
      throw error;
    }
  } catch (e) {
    log(`[artifact] openPath failed: ${safeFailureCode(e)}`);
    const error = new Error("system application could not open the artifact");
    error.code = e?.code || "ARTIFACT_SYSTEM_OPEN_FAILED";
    throw error;
  }
});

ipcMain.handle("echo:download-renderer-blob", async (
  event,
  blobUrl,
  suggestedFilename,
) => {
    assertTrustedIpcOrigin(event);
    const sender = event.sender;
    if (activeArtifactDownloadSenders.has(sender)) {
      const error = new Error("an artifact download is already active");
      error.code = "ARTIFACT_DOWNLOAD_BUSY";
      throw error;
    }
    activeArtifactDownloadSenders.add(sender);
    try {
      return await downloadRendererBlob({
        blobUrl,
        expectedInnerOrigin: trustedRendererBlobInnerOrigin(),
        suggestedFilename,
        sender,
        senderFrame: event.senderFrame,
        downloadDirectory: app.getPath("downloads"),
      });
    } catch (cause) {
      const error = new Error("artifact download failed");
      error.code = cause?.code || "ARTIFACT_DOWNLOAD_FAILED";
      throw error;
    } finally {
      activeArtifactDownloadSenders.delete(sender);
    }
});

// P4-fix-rag-chat（2026-05-28）：让 SettingsPanel"工作区目录"section 能用系统
// dialog 选目录，再 POST /workspace/add-dir 持久化 + 触发 scan。
//
// 安全：dialog 由 electron 主进程出，用户必须看到/点确认；返回 null 时表示
// 用户取消，不写任何配置。失败 reject 让 renderer message.error。
ipcMain.handle("workspace:pick-directory", async (event, context = {}, opts = {}) => {
  assertTrustedIpcOrigin(event);
  const expectedOrigin = rendererExpectedMainBackendOrigin(event, context);
  const win = BrowserWindow.getFocusedWindow();
  let defaultPath = os.homedir();
  if (typeof opts.defaultPath === "string" && opts.defaultPath) {
    try {
      defaultPath = resolveWorkspaceHandle(expectedOrigin, opts.defaultPath, {
        includePending: true,
      });
    } catch {
      // Renderer input is an opaque convenience hint; invalid hints fall back home.
    }
  }
  try {
    const r = await dialog.showOpenDialog(win || undefined, {
      title: "选择工作区目录（EchoDesk 会扫描索引整个文件夹）",
      properties: ["openDirectory", "createDirectory"],
      defaultPath,
      message: "支持的文件：PDF / Word / Excel / PPT / Markdown / TXT / HTML / CSV 等",
      buttonLabel: "选中此目录",
    });
    if (r.canceled || r.filePaths.length === 0) return null;
    const selectedCandidate = path.resolve(expandHome(r.filePaths[0]));
    const verifiedRoot = await verifyWorkspaceRootIdentity({
      root: selectedCandidate,
    });
    const selectedPath = verifiedRoot.canonical;
    // A trusted local/self-hosted renderer sends the selected path to its
    // colocated backend. Public mode must never expose it and continues to use
    // the origin-bound opaque handle consumed by workspace:add-local-dir.
    if (!PUBLIC_DEMO_MODE) return selectedPath;
    const handle = workspaceHandle(expectedOrigin, selectedPath);
    pendingWorkspaceSelections.set(expectedOrigin, {
      handle,
      path: selectedPath,
      identity: verifiedRoot.identity,
    });
    return handle;
  } catch (e) {
    log(`[workspace] pick-directory failed: ${safeFailureCode(e)}`);
    if (
      e?.code === "WORKSPACE_DIRECTORY_UNAVAILABLE" ||
      e?.code === "WORKSPACE_DIRECTORY_INVALID"
    ) {
      throw e;
    }
    const error = new Error("无法打开工作区目录选择器");
    error.code = "WORKSPACE_DIRECTORY_PICK_FAILED";
    throw error;
  }
});

ipcMain.handle("workspace:local-status", async (event, context = {}) => {
  assertTrustedIpcOrigin(event);
  const expectedOrigin = workspaceExpectedOrigin(event, context);
  return localWorkspaceStatus(expectedOrigin);
});

ipcMain.handle("workspace:add-local-dir", async (event, context = {}, dir) => {
  assertTrustedIpcOrigin(event);
  const expectedOrigin = workspaceExpectedOrigin(event, context);
  return runWorkspaceMutation(expectedOrigin, async (signal, leaseEpoch) => {
    const pendingSelection = pendingWorkspaceSelections.get(expectedOrigin);
    const selectedPath = resolveWorkspaceHandle(expectedOrigin, dir, {
      includePending: true,
    });
    const normalized = normalizeLocalWorkspaceDir(selectedPath);
    const store = readLocalWorkspaceStore(expectedOrigin);
    const expectedIdentity =
      pendingSelection?.handle === dir
        ? pendingSelection.identity
        : store.root_identities?.[normalized] || null;
    const verifiedRoot = await verifyWorkspaceRootIdentity({
      root: normalized,
      expectedIdentity,
    });
    const exists = store.workspaces.includes(normalized);
    const workspaces = exists ? store.workspaces : [...store.workspaces, normalized];
    const rootIdentities = {
      ...(store.root_identities || {}),
      [normalized]: verifiedRoot.identity,
    };
    assertWorkspaceOperationCurrent(expectedOrigin, leaseEpoch, signal);
    writeLocalWorkspaceStore(expectedOrigin, {
      ...store,
      workspaces,
      root_identities: rootIdentities,
    });
    pendingWorkspaceSelections.delete(expectedOrigin);
    return {
      added: !exists,
      path: workspaceHandle(expectedOrigin, normalized),
      configured_dirs: workspaceHandles(expectedOrigin, workspaces),
    };
  });
});

ipcMain.handle("workspace:remove-local-dir", async (event, context = {}, dir) => {
  assertTrustedIpcOrigin(event);
  const expectedOrigin = workspaceExpectedOrigin(event, context);
  return runWorkspaceMutation(expectedOrigin, async (signal, leaseEpoch) => {
    const normalized = resolveWorkspaceHandle(expectedOrigin, dir);
    const store = readLocalWorkspaceStore(expectedOrigin);
    const workspaces = store.workspaces.filter((d) => d !== normalized);
    const rootIdentities = { ...(store.root_identities || {}) };
    delete rootIdentities[normalized];
    const removed = workspaces.length !== store.workspaces.length;
    assertWorkspaceOperationCurrent(expectedOrigin, leaseEpoch, signal);
    writeLocalWorkspaceStore(expectedOrigin, {
      ...store,
      workspaces,
      root_identities: rootIdentities,
    });
    return {
      removed,
      path: workspaceHandle(expectedOrigin, normalized),
      configured_dirs: workspaceHandles(expectedOrigin, workspaces),
    };
  });
});

ipcMain.handle("workspace:scan-local", async (event, context = {}) => {
  assertTrustedIpcOrigin(event);
  const expectedOrigin = workspaceExpectedOrigin(event, context);
  return runWorkspaceMutation(expectedOrigin, (signal, leaseEpoch) =>
    scanLocalWorkspaces(expectedOrigin, signal, leaseEpoch),
  );
});

ipcMain.handle("workspace:clear-local-docs", async (event, context = {}) => {
  assertTrustedIpcOrigin(event);
  const expectedOrigin = workspaceExpectedOrigin(event, context);
  return runWorkspaceMutation(expectedOrigin, (signal, leaseEpoch) =>
    clearLocalWorkspaceDocs(expectedOrigin, signal, leaseEpoch),
  );
});

ipcMain.handle("workspace:cancel-origin-operations", async (event, context = {}) => {
  assertTrustedIpcOrigin(event);
  const expectedOrigin = workspaceExpectedOrigin(event, context);
  pendingWorkspaceSelections.delete(expectedOrigin);
  return { cancelled: cancelWorkspaceOperations(expectedOrigin) };
});

ipcMain.handle("mic:open-system-prefs", async (event) => {
  assertTrustedIpcOrigin(event);
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
    log(`[mic] openExternal failed: ${safeFailureCode(e)}`);
    return { ok: false, reason: "system-preferences-open-failed" };
  }
});

// 让 renderer 在 degraded UI 上按钮触发一次干净重启。重新启动必须回到
// spawnBackendAndWatch() 的 bundled-first 选择，打包安装不能依赖系统 Python。
const manualRestartBackend = createManualBackendRestart({
  isPublicDemo: () => PUBLIC_DEMO_MODE,
  healthcheckOnce,
  emitStatus,
  resetRestartState: () => {
    backendLifecycleGeneration += 1;
    restartAttempts = 0;
    expectedLocalBackendContractPromise = null;
    lastBackendContractFailure = null;
  },
  stopHealthWatcher,
  stopPublicBackendHealthWatcher,
  stopBackendProc: stopBackendProcForRestart,
  spawnBackendAndWatch,
  isShuttingDown: () => shuttingDown,
});

ipcMain.handle("backend:manual-restart", async (event) => {
  assertTrustedIpcOrigin(event);
  log("[backend] manual restart requested");
  return manualRestartBackend();
});

ipcMain.handle("background:set-status", async (event, rawStatus) => {
  assertTrustedIpcOrigin(event);
  backgroundStatus = normalizeBackgroundStatus(rawStatus);
  rebuildTrayMenu();
  return backgroundStatus;
});

ipcMain.handle("background:get-login-item", async (event) => {
  assertTrustedIpcOrigin(event);
  return loginItemSettings();
});

ipcMain.handle("background:set-login-item", async (event, openAtLogin) => {
  assertTrustedIpcOrigin(event);
  return setOpenAtLogin(openAtLogin === true);
});

// ---------- app 生命周期 ----------

if (singleInstanceLock) app.whenReady()
  .then(async () => {
    let protectedDirectories = [];
    let workspaceSweepSafe = true;
    try {
      protectedDirectories = workspaceRegistryPendingSnapshotDirectories(
        readLocalWorkspaceRegistry({ strict: true }),
      );
    } catch {
      // A registry that cannot be opened safely may still reference retained
      // snapshots, including after switching from public to local runtime.
      // Fail closed instead of sweeping recoverable upload intents.
      workspaceSweepSafe = false;
    }
    if (workspaceSweepSafe) {
      await sweepWorkspaceSnapshotRoots(protectedDirectories);
    }
    if (!IS_DEV) {
      installAppProtocol(
        protocol,
        path.join(__dirname, "..", "dist"),
        (url) => net.fetch(url),
        { backendBase: BACKEND_HOST },
      );
    }
    installMediaPermissionHandlers(session.defaultSession, {
      isTrustedRendererUrl: isTrustedAppRendererUrl,
      isTrustedRendererOrigin: isTrustedAppRendererOrigin,
    });
    // 主窗口先起，让用户看到 UI；backend 状态由 renderer 自己渲染（degraded UI 等）
    ensureTray();
    const openedAtLogin =
      loginItemSettings().openAtLogin &&
      app.getLoginItemSettings().wasOpenedAtLogin === true;
    await createWindow({ showOnReady: !START_HIDDEN && !openedAtLogin });
    startBackend();
    scheduleStartupUpdateCheck();

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        void createWindow({ showOnReady: true }).catch((error) => {
          console.error("[app] failed to recreate the main window:", error);
          app.quit();
        });
      } else {
        showMainWindow();
      }
    });
  })
  .catch((error) => {
    console.error("[app] secure renderer startup failed:", error);
    app.quit();
  });

app.on("second-instance", () => {
  showMainWindow();
});

app.on("window-all-closed", () => {
  // The supervised backend, agent runtime, public session and sync worker are
  // app-scoped. Keep them alive while the user has merely hidden every window.
});

// 优雅退出：先通知 renderer（避免它弹"断开"红条），再停止完整 backend
// process tree，最后真正 app.quit()。preventDefault 第一次拦下 quit；等子进程
// 清干净再放行。Windows 的 PyInstaller one-file backend 有 bootloader + server
// 两层进程，必须由 stopBackendProcess 使用 taskkill /T 一并回收。
app.on("before-quit", (event) => {
  if (quittingForReal) return;
  shuttingDown = true;
  emitStatus({ state: "shutting-down" });
  stopHealthWatcher();
  stopPublicBackendHealthWatcher();

  if (!backendProc || backendProc.exitCode !== null) {
    stopFusedWorkerBridge();
    quittingForReal = true;
    return;
  }

  event.preventDefault();
  const proc = backendProc;
  backendProc = null;
  log(
    process.platform === "win32"
      ? "[backend] stopping Windows process tree"
      : "[backend] SIGTERM child (graceful)",
  );

  let finished = false;
  const finalize = () => {
    if (finished) return;
    finished = true;
    stopFusedWorkerBridge();
    quittingForReal = true;
    app.quit();
  };
  void stopBackendProcess(proc, { graceMs: SIGKILL_GRACE_MS })
    .catch((error) => {
      log(`[backend] stop failed: ${safeFailureCode(error)}`);
    })
    .finally(finalize);
});
