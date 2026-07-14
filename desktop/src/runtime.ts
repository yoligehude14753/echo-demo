/**
 * 运行时配置：兼容 3 种场景
 *  1. 浏览器 + vite dev server（默认）→ 走相对 /api，由 vite 代理转发到 backend
 *  2. Electron + vite dev server → 同上（preload 注入的 host 仅做兜底）
 *  3. Electron 打包后加载 echodesk://app/index.html → 直接打 ECHO_BACKEND_HOST
 */

import backendConfig from "../backend.config.json";
import desktopReleaseAssetPatterns from "../electron/release-asset-patterns.json";

// SupervisorStatus 的具体形状定义在 hooks/useBackendHealth.ts；
// 这里用宽松 unknown 让 runtime.ts 不强耦合 health hook，且 hook 内做窄化
/** macOS systemPreferences.getMediaAccessStatus("microphone") 的全部可能值 */
export type ElectronMicStatus =
  | "not-determined"
  | "granted"
  | "denied"
  | "restricted"
  | "unknown";

export interface AppUpdateStatus {
  status:
    | "idle"
    | "checking"
    | "checked"
    | "current"
    | "available"
    | "downloading"
    | "downloaded"
    | "installing"
    | "error";
  currentVersion: string;
  latestVersion?: string | null;
  updateAvailable?: boolean;
  releaseName?: string;
  releaseUrl?: string;
  assetName?: string | null;
  assetUrl?: string | null;
  canAutoInstall?: boolean;
  percent?: number;
  autoDownloaded?: boolean;
  downloadReason?: string;
  error?: string;
}

export interface ElectronWorkspaceStatus {
  configured_dirs: string[];
  authorized_dirs: string[];
  n_indexed: number;
  max_file_mb: number;
  scan_on_startup: boolean;
}

export interface ElectronWorkspaceScanResult {
  n_total: number;
  n_added: number;
  n_updated: number;
  n_removed: number;
  n_skipped: number;
  n_failed: number;
  duration_s: number;
  errors: string[];
}

export interface ElectronWorkspaceContext {
  expectedBackendOrigin: string;
}

export interface ElectronPublicSession {
  token: string | null;
  expires_at: string | null;
  backend_origin: string;
  principal?: Record<string, unknown>;
  credential_expires_at?: string | null;
}

export interface ElectronBackendBuildContract {
  schema_version: number;
  product_id: string;
  product_version: string;
  api_contract: string;
  build_id: string;
  schema_catalog_max: number | null;
}

interface ElectronEchoBridge {
  isElectron?: boolean;
  isPublicDemo?: boolean;
  backendHost?: string;
  getBackendHost?: () => Promise<string>;
  getBackendContract?: () => Promise<ElectronBackendBuildContract | null>;
  getShareBackendHost?: () => Promise<string>;
  loadLocalLegacyHistory?: () => Promise<unknown | null>;
  ensurePublicSession?: () => Promise<ElectronPublicSession | null>;
  renewPublicSession?: () => Promise<ElectronPublicSession | null>;
  rotatePublicCredential?: (
    sessionToken: string,
  ) => Promise<{ credential_id: string | null; credential_expires_at: string | null }>;
  clearPublicCredential?: () => Promise<{ cleared: boolean }>;
  // Phase 1 P1.5/P1.6 BackendSupervisor IPC
  onBackendStatus?: (cb: (status: unknown) => void) => () => void;
  manualRestartBackend?: () => Promise<{ ok: boolean }>;
  checkForUpdates?: () => Promise<AppUpdateStatus>;
  getUpdateStatus?: () => Promise<AppUpdateStatus>;
  installUpdate?: () => Promise<{ ok: boolean; reason?: string; releaseUrl?: string }>;
  openReleasePage?: () => Promise<{ ok: boolean; releaseUrl?: string }>;
  openExternal?: (url: string) => Promise<{ ok: boolean }>;
  onUpdateStatus?: (cb: (status: AppUpdateStatus) => void) => () => void;
  // Phase 3 P3.5 麦克风权限
  getMicStatus?: () => Promise<ElectronMicStatus>;
  requestMic?: () => Promise<boolean>;
  openMicSystemPrefs?: () => Promise<{ ok: boolean; reason?: string }>;
  // Phase 4 M4 产物预览：用系统默认 App 打开 backend 落盘的绝对路径（pptx → Keynote）。
  // 失败时 reject(new Error(reason))；浏览器/纯 dev 模式下 undefined。
  openArtifactInSystem?: (filePath: string) => Promise<void>;
  downloadArtifactBlob?: (
    blobUrl: string,
    suggestedFilename?: string,
  ) => Promise<{ ok: boolean; cancelled: boolean; filename: string | null }>;
  // P4-fix-rag-chat：选工作区目录。Promise<string | null>，null=用户取消。
  // 浏览器/纯 dev 模式下 undefined（SettingsPanel 会用 prompt() 兜底）。
  pickDirectory?: (
    context: ElectronWorkspaceContext,
    opts?: { defaultPath?: string },
  ) => Promise<string | null>;
  // 公网桌面包：后端在云端，不能直接读取本机目录；目录授权和文件扫描由
  // Electron 主进程在本机完成，再把可索引文件上传到云端 RAG。
  getLocalWorkspaceStatus?: (
    context: ElectronWorkspaceContext,
  ) => Promise<ElectronWorkspaceStatus>;
  addLocalWorkspaceDir?: (
    context: ElectronWorkspaceContext,
    dir: string,
  ) => Promise<{ added: boolean; path: string; configured_dirs: string[] }>;
  removeLocalWorkspaceDir?: (
    context: ElectronWorkspaceContext,
    dir: string,
  ) => Promise<{ removed: boolean; path: string; configured_dirs: string[] }>;
  scanLocalWorkspaces?: (
    context: ElectronWorkspaceContext,
  ) => Promise<ElectronWorkspaceScanResult>;
  clearLocalWorkspaceDocs?: (
    context: ElectronWorkspaceContext,
  ) => Promise<{ n_removed: number }>;
  cancelLocalWorkspaceOperations?: (
    context: ElectronWorkspaceContext,
  ) => Promise<{ cancelled: number }>;
}

declare global {
  interface Window {
    echo?: ElectronEchoBridge;
    Capacitor?: { isNativePlatform?: () => boolean };
    __ECHODESK_TV_PACKAGE__?: boolean;
  }
  // 由 vite.config.ts 从当前 package version 注入；编译时替换为版本字面量。
  const __APP_VERSION__: string;
}

let cachedBase: string | null = null;

export const MOBILE_BACKEND_BASE_KEY = "echodesk.mobileBackendBase";
export const MOBILE_BACKEND_BASE_USER_SET_KEY = "echodesk.mobileBackendBase.userSet";
export const BACKEND_ORIGIN_EVENT = "echodesk:backend-origin-change";
export const SYNC_HUB_BASE_KEY = "echodesk.syncHubBase";
export const SYNC_HUB_BASE_EVENT = "echodesk:sync-hub-change";
export const PUBLIC_DATA_BOUNDARY_KEY = "echodesk.publicDataBoundary.v2";
export const DEFAULT_ANDROID_BACKEND_BASE = backendConfig.public.baseUrl;
export const DEFAULT_LOCAL_BACKEND_BASE = `http://${backendConfig.local.host}:${backendConfig.local.port}`;
export const DEFAULT_SYNC_HUB_BASE = backendConfig.public.baseUrl;
export const FORCE_TV_UI_KEY = "echodesk.forceTvUi";
const PUBLIC_DATA_BOUNDARY_SCHEMA = 3;
export const RELEASES_URL =
  "https://github.com/yoligehude14753/echo-demo/releases/latest";
const RELEASE_API_URL =
  "https://api.github.com/repos/yoligehude14753/echo-demo/releases/latest";
const PUBLIC_HISTORY_STORAGE_KEYS = [
  "echodesk.currentMeetingId",
  "echodesk.lastMeetingId",
  "echodesk.activeMeetingId",
  "echodesk.meetingHistory",
  "echodesk.meetings",
  "echodesk.capture.recent",
  "echodesk.localCaptureState.v1",
  "echodesk.ambientSegments",
  "echodesk.lastAmbientSegments",
  "echodesk.currentMeeting",
];

export class BackendBasePolicyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BackendBasePolicyError";
  }
}

function parseIpv4(hostname: string): number[] | null {
  if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(hostname)) return null;
  const parts = hostname.split(".").map((part) => Number.parseInt(part, 10));
  return parts.every((part) => part >= 0 && part <= 255) ? parts : null;
}

/** HTTP is only a deliberate LAN/loopback escape hatch; public targets require TLS. */
export function isPrivateHttpHostname(rawHostname: string): boolean {
  const hostname = rawHostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (hostname === "localhost" || hostname.endsWith(".localhost")) return true;

  const ipv4 = parseIpv4(hostname);
  if (ipv4) {
    const [a, b] = ipv4;
    return (
      a === 10 ||
      a === 127 ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 169 && b === 254)
    );
  }

  // IPv4-mapped IPv6 inherits the embedded IPv4 policy.
  const mappedIpv4 = hostname.match(/^(?:::ffff:)(\d{1,3}(?:\.\d{1,3}){3})$/i);
  if (mappedIpv4) return isPrivateHttpHostname(mappedIpv4[1]);
  if (hostname === "::1") return true;
  const firstHextet = hostname.split(":", 1)[0];
  const first = Number.parseInt(firstHextet, 16);
  if (!Number.isFinite(first)) return false;
  return (first & 0xfe00) === 0xfc00 || (first & 0xffc0) === 0xfe80;
}

export function normalizeBackendBase(
  raw: string | null | undefined,
): string | null {
  const value = raw?.trim();
  if (!value) return null;
  const withScheme = /^[a-z][a-z\d+.-]*:\/\//i.test(value)
    ? value
    : `http://${value}`;
  let parsed: URL;
  try {
    parsed = new URL(withScheme);
  } catch {
    throw new BackendBasePolicyError("服务地址格式无效，请输入完整的主机名或 IP 地址");
  }
  if (
    (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
    parsed.username ||
    parsed.password
  ) {
    throw new BackendBasePolicyError("服务地址必须是不含账号信息的 HTTP(S) 地址");
  }
  if (parsed.pathname !== "/" || parsed.search || parsed.hash) {
    throw new BackendBasePolicyError("服务地址只能填写 origin，不能包含路径、参数或片段");
  }
  if (parsed.protocol === "http:" && !isPrivateHttpHostname(parsed.hostname)) {
    throw new BackendBasePolicyError(
      "公网主机必须使用 HTTPS；HTTP 仅允许 loopback 或私有/链路本地 IP",
    );
  }
  return parsed.origin;
}

function normalizeVersion(raw: string | null | undefined): string {
  return String(raw ?? "").trim().replace(/^v/i, "");
}

interface ParsedVersion {
  core: string[];
  prerelease: string[] | null;
}

function parseVersion(raw: string): ParsedVersion {
  const withoutBuild = normalizeVersion(raw).split("+", 1)[0];
  const prereleaseIndex = withoutBuild.indexOf("-");
  const core = (
    prereleaseIndex >= 0 ? withoutBuild.slice(0, prereleaseIndex) : withoutBuild
  ).split(".");
  const prerelease =
    prereleaseIndex >= 0
      ? withoutBuild.slice(prereleaseIndex + 1).split(".")
      : null;
  return { core, prerelease };
}

function compareNumericIdentifiers(a: string, b: string): number {
  const aa = a.replace(/^0+/, "") || "0";
  const bb = b.replace(/^0+/, "") || "0";
  if (aa.length !== bb.length) return aa.length > bb.length ? 1 : -1;
  if (aa === bb) return 0;
  return aa > bb ? 1 : -1;
}

function comparePrereleaseIdentifiers(a: string[], b: string[]): number {
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    const av = a[i];
    const bv = b[i];
    if (av === undefined) return -1;
    if (bv === undefined) return 1;
    if (av === bv) continue;
    const aIsNumeric = /^\d+$/.test(av);
    const bIsNumeric = /^\d+$/.test(bv);
    if (aIsNumeric && bIsNumeric) return compareNumericIdentifiers(av, bv);
    if (aIsNumeric !== bIsNumeric) return aIsNumeric ? -1 : 1;
    return av > bv ? 1 : -1;
  }
  return 0;
}

export function compareVersions(a: string, b: string): number {
  const aa = parseVersion(a);
  const bb = parseVersion(b);
  for (let i = 0; i < Math.max(aa.core.length, bb.core.length); i += 1) {
    const coreOrder = compareNumericIdentifiers(aa.core[i] ?? "0", bb.core[i] ?? "0");
    if (coreOrder !== 0) return coreOrder;
  }
  if (aa.prerelease === null) return bb.prerelease === null ? 0 : 1;
  if (bb.prerelease === null) return -1;
  return comparePrereleaseIdentifiers(aa.prerelease, bb.prerelease);
}

/**
 * 只接受严格高于“桥接上报版本”和当前前端构建版本的更新。
 *
 * Electron 的更新状态可能来自缓存或旧主进程，因此不能只信任
 * updateAvailable / currentVersion。任何一侧显示目标版本不够新时都
 * fail closed，避免把公开旧版本误当成更新。
 */
export function isNewerAppUpdate(
  status: AppUpdateStatus | null | undefined,
): boolean {
  if (!status?.latestVersion) return false;
  const reportedCurrent = status.currentVersion || __APP_VERSION__;
  return (
    compareVersions(status.latestVersion, reportedCurrent) > 0 &&
    compareVersions(status.latestVersion, __APP_VERSION__) > 0
  );
}

export function canInstallAppUpdate(
  status: AppUpdateStatus | null | undefined,
): boolean {
  if (!status || !isNewerAppUpdate(status)) return false;
  if (status.status === "downloaded") return status.canAutoInstall === true;
  return (
    (status.status === "available" || status.status === "checked") &&
    status.updateAvailable === true
  );
}

function preferredUpdateAsset(
  assets: Array<{ name: string; url: string; size?: number }>,
): { name: string; url: string; size?: number } | null {
  let patterns = desktopReleaseAssetPatterns.darwin.map(
    (source) => new RegExp(source, "i"),
  );
  if (typeof window !== "undefined") {
    const ua = window.navigator.userAgent;
    const tv = isTvRuntime();
    if (tv && (isNativeMobile() || /Android|AFT|TV|EchoDeskTV/i.test(ua))) {
      patterns = [/smart-tv\.apk$/i, /smart-tv-oneclick\.zip$/i];
    } else if (isNativeMobile() || /Android/i.test(ua)) {
      patterns = [/-android\.apk$/i, /smart-tv\.apk$/i];
    } else if (/Windows/i.test(ua)) {
      patterns = desktopReleaseAssetPatterns.win32.map(
        (source) => new RegExp(source, "i"),
      );
    } else if (/Linux/i.test(ua)) {
      patterns = desktopReleaseAssetPatterns.linux.map(
        (source) => new RegExp(source, "i"),
      );
    }
  }
  for (const pattern of patterns) {
    const asset = assets.find((a) => pattern.test(a.name));
    if (asset) return asset;
  }
  return null;
}

function envBackendBase(): string | null {
  const env = (import.meta as { env?: Record<string, string | undefined> }).env;
  return normalizeBackendBase(
    env?.VITE_ECHODESK_API_BASE ?? env?.VITE_API_BASE_URL,
  );
}

export function storedBackendBase(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return normalizeBackendBase(window.localStorage.getItem(MOBILE_BACKEND_BASE_KEY));
  } catch {
    return null;
  }
}

function envSyncHubBase(): string | null {
  const env = (import.meta as { env?: Record<string, string | undefined> }).env;
  return normalizeBackendBase(env?.VITE_ECHODESK_SYNC_HUB_BASE);
}

export function configuredSyncHubBase(): string {
  if (typeof window !== "undefined") {
    try {
      const stored = normalizeBackendBase(window.localStorage.getItem(SYNC_HUB_BASE_KEY));
      if (stored) return stored;
    } catch {
      // 继续使用环境变量或内置地址。
    }
  }
  return envSyncHubBase() ?? DEFAULT_SYNC_HUB_BASE;
}

export function setSyncHubBase(value: string): string {
  const normalized = normalizeBackendBase(value);
  if (!normalized) {
    if (typeof window !== "undefined") {
      window.localStorage.removeItem(SYNC_HUB_BASE_KEY);
      window.dispatchEvent(new Event(SYNC_HUB_BASE_EVENT));
    }
    return configuredSyncHubBase();
  }
  if (typeof window !== "undefined") {
    window.localStorage.setItem(SYNC_HUB_BASE_KEY, normalized);
    window.dispatchEvent(new Event(SYNC_HUB_BASE_EVENT));
  }
  return normalized;
}

export function setStoredBackendBase(value: string): string | null {
  if (typeof window === "undefined") return null;
  if (isPackagedElectronRenderer()) {
    // Installed Electron transport identity is fixed by main/preload. A stale
    // mobile localStorage value must never redirect authenticated app traffic.
    return normalizeBackendBase(window.echo?.backendHost);
  }
  const previous = storedBackendBase();
  const normalized = normalizeBackendBase(value);
  try {
    if (normalized) {
      window.localStorage.setItem(MOBILE_BACKEND_BASE_KEY, normalized);
      if (isDefaultPublicBackend(normalized)) {
        window.localStorage.removeItem(MOBILE_BACKEND_BASE_USER_SET_KEY);
      } else {
        window.localStorage.setItem(MOBILE_BACKEND_BASE_USER_SET_KEY, "1");
      }
    } else {
      window.localStorage.removeItem(MOBILE_BACKEND_BASE_KEY);
      window.localStorage.removeItem(MOBILE_BACKEND_BASE_USER_SET_KEY);
    }
  } catch {
    return normalized;
  }
  cachedBase = null;
  if (previous !== normalized) {
    window.dispatchEvent(
      new CustomEvent(BACKEND_ORIGIN_EVENT, {
        detail: { base: normalized },
      }),
    );
  }
  return normalized;
}

export function isNativeMobile(): boolean {
  if (typeof window === "undefined") return false;
  return window.Capacitor?.isNativePlatform?.() === true;
}

export async function checkAppUpdate(): Promise<AppUpdateStatus> {
  if (typeof window !== "undefined" && window.echo?.checkForUpdates) {
    return window.echo.checkForUpdates();
  }

  try {
    const res = await fetch(RELEASE_API_URL, {
      headers: { Accept: "application/vnd.github+json" },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const release = (await res.json()) as {
      tag_name?: string;
      name?: string;
      html_url?: string;
      assets?: Array<{
        name?: string;
        size?: number;
        browser_download_url?: string;
      }>;
    };
    const latestVersion = normalizeVersion(release.tag_name || release.name);
    const assets = (release.assets ?? [])
      .filter((a) => a.name && a.browser_download_url)
      .map((a) => ({
        name: a.name as string,
        size: a.size,
        url: a.browser_download_url as string,
      }));
    const asset = preferredUpdateAsset(assets);
    const hasNewerVersion = compareVersions(latestVersion, __APP_VERSION__) > 0;
    const updateAvailable = hasNewerVersion && asset !== null;
    return {
      status: updateAvailable ? "available" : hasNewerVersion ? "checked" : "current",
      currentVersion: __APP_VERSION__,
      latestVersion,
      updateAvailable,
      releaseName: release.name || release.tag_name || "",
      releaseUrl: release.html_url || RELEASES_URL,
      assetName: asset?.name ?? null,
      assetUrl: asset?.url ?? null,
      canAutoInstall: false,
    };
  } catch (e) {
    return {
      status: "error",
      currentVersion: __APP_VERSION__,
      latestVersion: null,
      updateAvailable: false,
      releaseUrl: RELEASES_URL,
      assetName: null,
      assetUrl: null,
      canAutoInstall: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

export async function openUpdateTarget(status?: AppUpdateStatus): Promise<void> {
  const target = status?.assetUrl || status?.releaseUrl || RELEASES_URL;
  if (typeof window !== "undefined" && window.echo?.openExternal) {
    await window.echo.openExternal(target);
    return;
  }
  if (typeof window !== "undefined") {
    window.open(target, "_blank", "noopener,noreferrer");
  }
}

export async function installAppUpdate(status?: AppUpdateStatus): Promise<void> {
  if (!status || !canInstallAppUpdate(status)) {
    throw new Error("no newer EchoDesk update is available");
  }
  if (status?.canAutoInstall && typeof window !== "undefined" && window.echo?.installUpdate) {
    await window.echo.installUpdate();
    return;
  }
  await openUpdateTarget(status);
}

export function configuredBackendBase(): string | null {
  if (isPackagedElectronRenderer()) {
    return normalizeBackendBase(window.echo?.backendHost);
  }
  return storedBackendBase() ?? envBackendBase();
}

export function isPackagedElectronRenderer(): boolean {
  if (typeof window === "undefined" || window.echo?.isElectron !== true) {
    return false;
  }
  // file: 仅用于兼容尚未升级到 secure custom scheme 的旧安装包。
  return window.location.protocol === "echodesk:" || window.location.protocol === "file:";
}

/**
 * 同步读取当前 Renderer 的权威 backend base。
 *
 * Electron preload 在页面脚本执行前从主进程取得最终 local/public/custom host，
 * 因而下载链接不需要猜测端口。返回 null 只表示旧 preload 尚未提供同步快照，
 * 此时异步 API 路径仍可通过 getBackendHost() 完成兼容解析。
 */
export function backendBaseSnapshot(): string | null {
  if (cachedBase !== null) return cachedBase;

  if (isPackagedElectronRenderer()) {
    const bridgeHost = normalizeBackendBase(window.echo?.backendHost);
    if (!bridgeHost) return null;
    cachedBase = bridgeHost;
    return cachedBase;
  }

  const configured = configuredBackendBase();
  if (configured) {
    cachedBase = configured;
    return cachedBase;
  }

  if (isNativeMobile()) {
    cachedBase = DEFAULT_ANDROID_BACKEND_BASE;
    return cachedBase;
  }

  cachedBase = "";
  return cachedBase;
}

export function isDefaultPublicBackend(base: string | null | undefined): boolean {
  try {
    const normalized = normalizeBackendBase(base);
    return normalized === DEFAULT_ANDROID_BACKEND_BASE;
  } catch {
    return false;
  }
}

function isPublicDesktopDemo(): boolean {
  if (typeof window === "undefined") return false;
  // Packaged Electron asks the main process for the authoritative runtime mode.
  // 只在旧 preload 没有暴露 isPublicDemo 时使用 packaged renderer 启发式；
  // 否则显式 local 模式会被误判为 public。
  if (
    window.echo?.isElectron === true &&
    typeof window.echo.isPublicDemo === "boolean"
  ) {
    return window.echo.isPublicDemo;
  }
  return (
    isPackagedElectronRenderer() &&
    !storedBackendBase()
  );
}

function hasExplicitBackendOverride(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(MOBILE_BACKEND_BASE_USER_SET_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * Renderer 是否连接公共多租户服务。
 *
 * Electron 只信任 main process 在 preload 中给出的最终运行模式；旧 preload
 * 才使用既有 packaged fallback。Android / TV 和浏览器显式指向默认公共地址时
 * 也属于 public。组件用这个 capability 裁剪 host-admin 功能，不能靠 403 响应
 * 事后猜测服务模式。
 */
export function isPublicRuntime(): boolean {
  if (typeof window === "undefined") return false;
  if (typeof window.echo?.isPublicDemo === "boolean") {
    return window.echo.isPublicDemo;
  }
  if (isPublicDesktopDemo()) return true;

  const configured = configuredBackendBase();
  if (configured !== null && isDefaultPublicBackend(configured)) return true;
  return (
    (isNativeMobile() || isTvLikeViewport()) &&
    isDefaultPublicBackend(configured ?? DEFAULT_ANDROID_BACKEND_BASE)
  );
}

function isPublicNativeOrTvContext(): boolean {
  if (typeof window === "undefined") return false;
  return window.echo?.isPublicDemo === true || isNativeMobile() || isTvLikeViewport();
}

/**
 * Public demo 的数据边界：
 * - public backend 是共享服务，新装客户端不能继承旧 WebView/localStorage 的会议选择、
 *   ambient 缓存或旧 backend URL，否则看起来像“数据库串了”。
 * - 仅当用户在设置中明确保存过自定义 backend（userSet=1）时，保留该地址作为
 *   私有/内网演示入口。
 * - 不清 onboarding / TTS 等纯偏好设置，避免升级后用户体验被重置。
 */
export function installPublicDemoStorageMigration(): void {
  if (typeof window === "undefined") return;
  if (!isPublicNativeOrTvContext()) return;
  try {
    const markerRaw = window.localStorage.getItem(PUBLIC_DATA_BOUNDARY_KEY);
    if (markerRaw) {
      try {
        const marker = JSON.parse(markerRaw) as { schema?: number };
        if ((marker.schema ?? 0) >= PUBLIC_DATA_BOUNDARY_SCHEMA) {
          return;
        }
      } catch {
        // marker 损坏时按首次迁移处理，避免继续继承旧共享状态。
      }
    }
    const explicitBackend = hasExplicitBackendOverride();
    if (!explicitBackend) {
      window.localStorage.removeItem(MOBILE_BACKEND_BASE_KEY);
      cachedBase = null;
    }
    for (const key of PUBLIC_HISTORY_STORAGE_KEYS) {
      window.localStorage.removeItem(key);
    }
    window.localStorage.setItem(
      PUBLIC_DATA_BOUNDARY_KEY,
      JSON.stringify({
        schema: PUBLIC_DATA_BOUNDARY_SCHEMA,
        appVersion: typeof __APP_VERSION__ === "string" ? __APP_VERSION__ : "unknown",
        explicitBackend,
      }),
    );
  } catch {
    // localStorage 在极端 WebView 设置下可能不可用；迁移失败不能阻塞启动。
  }
}

/**
 * Android / TV demo 包默认连接公共 backend。公共 backend 不能把其它设备的
 * historical meetings / ambient feed 直接 hydrate 到新装设备，否则会议室电视
 * 看起来像“继承了别人数据”。本函数只影响客户端启动期展示策略；桌面和自建
 * backend 仍保留完整历史。
 */
export function shouldHideSharedPublicHistory(): boolean {
  if (typeof window === "undefined") return false;
  const configured = configuredBackendBase();
  const usesDefaultPublicBackend = isDefaultPublicBackend(
    configured ?? DEFAULT_ANDROID_BACKEND_BASE,
  );
  const explicitCustomBackend =
    hasExplicitBackendOverride() && configured !== null && !usesDefaultPublicBackend;
  if (explicitCustomBackend) return false;
  return (
    isPublicDesktopDemo() ||
    ((isNativeMobile() || isTvLikeViewport()) && usesDefaultPublicBackend)
  );
}

export function isTvLikeViewport(): boolean {
  if (isTvRuntime()) return true;
  if (typeof window === "undefined") return false;
  const ua = window.navigator.userAgent;
  const isAndroid = /Android/i.test(ua);
  const width = Math.max(window.screen.width || 0, window.innerWidth || 0);
  const height = Math.max(window.screen.height || 0, window.innerHeight || 0);
  const shortSide = Math.min(width, height);
  const longSide = Math.max(width, height);
  // 仅作为最后兜底：部分 Android TV WebView 不暴露 TV UA。普通 Android
  // 更新资产选择不会使用该 viewport 兜底，避免平板横屏下载 smart-tv APK。
  return isAndroid && longSide >= 900 && shortSide >= 500;
}

export function isTvRuntime(): boolean {
  if (typeof window === "undefined") return false;
  let force = false;
  try {
    force = window.localStorage.getItem(FORCE_TV_UI_KEY) === "1";
  } catch {
    force = false;
  }
  if (force) return true;
  if (window.__ECHODESK_TV_PACKAGE__ === true) return true;
  const ua = window.navigator.userAgent;
  return /EchoDeskTV|SmartTV|Android TV|AFT/i.test(ua);
}

export function installRuntimeBodyClasses(): void {
  if (typeof document === "undefined") return;
  const applyRuntimeState = () => {
    document.documentElement.classList.toggle("echodesk-tv", isTvLikeViewport());
    document.documentElement.classList.toggle(
      "echodesk-public-native",
      shouldHideSharedPublicHistory(),
    );
    if (typeof window !== "undefined") {
      document.documentElement.style.setProperty(
        "--echodesk-vh",
        `${window.innerHeight}px`,
      );
    }
  };
  applyRuntimeState();
  if (typeof window !== "undefined") {
    window.addEventListener("resize", applyRuntimeState, { passive: true });
    window.addEventListener("orientationchange", applyRuntimeState, { passive: true });
  }
}

export function installTvRemoteClickBridge(): void {
  if (typeof window === "undefined" || typeof document === "undefined") return;
  window.addEventListener(
    "keydown",
    (event) => {
      if (!document.documentElement.classList.contains("echodesk-tv")) return;
      if (event.defaultPrevented) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      const active = document.activeElement;
      if (!(active instanceof HTMLElement)) return;
      const tag = active.tagName.toLowerCase();
      const role = active.getAttribute("role");
      const clickable =
        tag === "button" ||
        role === "button" ||
        active.hasAttribute("data-tv-clickable");
      if (!clickable) return;
      event.preventDefault();
      active.click();
    },
    true,
  );
}

export async function shareBackendBase(): Promise<string> {
  if (isPackagedElectronRenderer()) {
    const authoritative = await window.echo?.getShareBackendHost?.();
    if (authoritative) {
      return normalizeBackendBase(authoritative) ?? authoritative;
    }
  }
  const configured = configuredBackendBase();
  if (configured) return configured;

  const fromElectron =
    typeof window !== "undefined"
      ? await window.echo?.getShareBackendHost?.()
      : null;
  if (fromElectron) return normalizeBackendBase(fromElectron) ?? fromElectron;

  const base = await backendBase();
  if (base) return base;

  if (
    typeof window !== "undefined" &&
    window.location.protocol.startsWith("http") &&
    window.location.host
  ) {
    return window.location.origin;
  }
  return DEFAULT_ANDROID_BACKEND_BASE;
}

export async function backendBase(): Promise<string> {
  const snapshot = backendBaseSnapshot();
  if (snapshot !== null) return snapshot;

  // 兼容旧 preload：当前版本会由 backendBaseSnapshot() 同步取得权威 host。
  if (isPackagedElectronRenderer()) {
    const host =
      (await window.echo?.getBackendHost?.()) ?? DEFAULT_LOCAL_BACKEND_BASE;
    cachedBase = normalizeBackendBase(host) ?? DEFAULT_LOCAL_BACKEND_BASE;
    return cachedBase;
  }

  return "";
}

export async function backendWsUrl(): Promise<string> {
  const base = await backendBase();
  if (base) {
    return base.replace(/^http/, "ws") + "/ws/echo";
  }
  // vite dev server：ws 走 host
  if (typeof window !== "undefined" && window.location.protocol.startsWith("http")) {
    return `${window.location.protocol.replace("http", "ws")}//${window.location.host}/ws/echo`;
  }
  return DEFAULT_LOCAL_BACKEND_BASE.replace(/^http/, "ws") + "/ws/echo";
}

export function apiPath(p: string): string {
  // 在 vite 代理场景下，/api 会被 rewrite 掉 /api 前缀 → 直达 backend
  // 在 Electron packaged 场景，apiPath 拼绝对 base，去掉 /api 前缀
  return `/api${p.startsWith("/") ? p : `/${p}`}`;
}

export async function apiUrl(p: string): Promise<string> {
  const base = await backendBase();
  if (base) {
    return base + (p.startsWith("/") ? p : `/${p}`);
  }
  return apiPath(p);
}
