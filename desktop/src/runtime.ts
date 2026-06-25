/**
 * 运行时配置：兼容 3 种场景
 *  1. 浏览器 + vite dev server（默认）→ 走相对 /api，由 vite 代理转发到 backend
 *  2. Electron + vite dev server → 同上（preload 注入的 host 仅做兜底）
 *  3. Electron 打包后加载 file://dist/index.html → 直接打 ECHO_BACKEND_HOST
 */

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
  error?: string;
}

interface ElectronEchoBridge {
  isElectron?: boolean;
  isPublicDemo?: boolean;
  getBackendHost?: () => Promise<string>;
  getShareBackendHost?: () => Promise<string>;
  // Phase 1 P1.5/P1.6 BackendSupervisor IPC
  onBackendStatus?: (cb: (status: unknown) => void) => () => void;
  manualRestartBackend?: () => Promise<{ ok: boolean }>;
  checkForUpdates?: () => Promise<AppUpdateStatus>;
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
  // P4-fix-rag-chat：选工作区目录。Promise<string | null>，null=用户取消。
  // 浏览器/纯 dev 模式下 undefined（SettingsPanel 会用 prompt() 兜底）。
  pickDirectory?: (opts?: { defaultPath?: string }) => Promise<string | null>;
}

declare global {
  interface Window {
    echo?: ElectronEchoBridge;
    Capacitor?: { isNativePlatform?: () => boolean };
  }
  // 由 vite.config.ts define 注入；编译时替换为 "0.2.0" 字面量
  const __APP_VERSION__: string;
}

let cachedBase: string | null = null;

export const MOBILE_BACKEND_BASE_KEY = "echodesk.mobileBackendBase";
export const DEFAULT_ANDROID_BACKEND_BASE = "https://echodesk.yoliyoli.uk";
export const FORCE_TV_UI_KEY = "echodesk.forceTvUi";
export const RELEASES_URL =
  "https://github.com/yoligehude14753/echo-demo/releases/latest";
const RELEASE_API_URL =
  "https://api.github.com/repos/yoligehude14753/echo-demo/releases/latest";

function normalizeBackendBase(raw: string | null | undefined): string | null {
  const v = raw?.trim().replace(/\/+$/, "");
  if (!v) return null;
  if (!/^https?:\/\//.test(v)) return `http://${v}`;
  return v;
}

function normalizeVersion(raw: string | null | undefined): string {
  return String(raw ?? "").trim().replace(/^v/i, "");
}

function compareVersions(a: string, b: string): number {
  const aa = normalizeVersion(a).split(".").map((x) => Number.parseInt(x, 10) || 0);
  const bb = normalizeVersion(b).split(".").map((x) => Number.parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(aa.length, bb.length); i += 1) {
    const av = aa[i] ?? 0;
    const bv = bb[i] ?? 0;
    if (av > bv) return 1;
    if (av < bv) return -1;
  }
  return 0;
}

function preferredUpdateAsset(
  assets: Array<{ name: string; url: string; size?: number }>,
): { name: string; url: string; size?: number } | null {
  let patterns: RegExp[] = [/\.dmg$/i, /-mac\.zip$/i];
  if (typeof window !== "undefined") {
    const ua = window.navigator.userAgent;
    const tv = isTvLikeViewport();
    if (tv && (isNativeMobile() || /Android|AFT|TV/i.test(ua))) {
      patterns = [/smart-tv\.apk$/i, /smart-tv-oneclick\.zip$/i];
    } else if (isNativeMobile() || /Android/i.test(ua)) {
      patterns = [/-android\.apk$/i, /smart-tv\.apk$/i];
    } else if (/Windows/i.test(ua)) {
      patterns = [/Setup\.[\d.]+\.exe$/i, /\.exe$/i];
    } else if (/Linux/i.test(ua)) {
      patterns = [/\.AppImage$/i, /\.deb$/i];
    }
  }
  for (const pattern of patterns) {
    const asset = assets.find((a) => pattern.test(a.name));
    if (asset) return asset;
  }
  return assets[0] ?? null;
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

export function setStoredBackendBase(value: string): string | null {
  if (typeof window === "undefined") return null;
  const normalized = normalizeBackendBase(value);
  try {
    if (normalized) {
      window.localStorage.setItem(MOBILE_BACKEND_BASE_KEY, normalized);
    } else {
      window.localStorage.removeItem(MOBILE_BACKEND_BASE_KEY);
    }
  } catch {
    return normalized;
  }
  cachedBase = null;
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
    return {
      status: compareVersions(latestVersion, __APP_VERSION__) > 0 ? "available" : "current",
      currentVersion: __APP_VERSION__,
      latestVersion,
      updateAvailable: compareVersions(latestVersion, __APP_VERSION__) > 0,
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
  if (status?.canAutoInstall && typeof window !== "undefined" && window.echo?.installUpdate) {
    await window.echo.installUpdate();
    return;
  }
  await openUpdateTarget(status);
}

export function configuredBackendBase(): string | null {
  return storedBackendBase() ?? envBackendBase();
}

export function isDefaultPublicBackend(base: string | null | undefined): boolean {
  const normalized = normalizeBackendBase(base);
  return normalized === DEFAULT_ANDROID_BACKEND_BASE;
}

function isPublicDesktopDemo(): boolean {
  if (typeof window === "undefined") return false;
  if (window.echo?.isPublicDemo === true) return true;
  return (
    window.echo?.isElectron === true &&
    window.location.protocol === "file:" &&
    !storedBackendBase()
  );
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
  return (
    isPublicDesktopDemo() ||
    ((isNativeMobile() || isTvLikeViewport()) && usesDefaultPublicBackend)
  );
}

export function isTvLikeViewport(): boolean {
  if (typeof window === "undefined") return false;
  let force = false;
  try {
    force = window.localStorage.getItem(FORCE_TV_UI_KEY) === "1";
  } catch {
    force = false;
  }
  if (force) return true;
  const ua = window.navigator.userAgent;
  const isAndroid = /Android/i.test(ua);
  const width = Math.max(window.screen.width || 0, window.innerWidth || 0);
  const height = Math.max(window.screen.height || 0, window.innerHeight || 0);
  const shortSide = Math.min(width, height);
  const longSide = Math.max(width, height);
  // 多数 Android TV WebView 使用 density-scaled CSS viewport（例如 1920x1080
  // 物理屏常报告 1280x720 CSS px），不能按物理像素阈值判断。
  return isAndroid && longSide >= 900 && shortSide >= 500;
}

export function installRuntimeBodyClasses(): void {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("echodesk-tv", isTvLikeViewport());
  document.documentElement.classList.toggle(
    "echodesk-public-native",
    shouldHideSharedPublicHistory(),
  );
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
  if (cachedBase !== null) return cachedBase;

  const configured = configuredBackendBase();
  if (configured) {
    cachedBase = configured;
    return cachedBase;
  }

  if (isNativeMobile()) {
    cachedBase = DEFAULT_ANDROID_BACKEND_BASE;
    return cachedBase;
  }

  // file:// 协议（Electron prod）→ 必须走绝对地址
  if (typeof window !== "undefined" && window.location.protocol === "file:") {
    const host =
      (await window.echo?.getBackendHost?.()) ?? "http://127.0.0.1:8769";
    cachedBase = host;
    return cachedBase;
  }
  // 其它情况：vite 代理
  cachedBase = "";
  return cachedBase;
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
  return "ws://127.0.0.1:8769/ws/echo";
}

export function apiPath(p: string): string {
  // 在 vite 代理场景下，/api 会被 rewrite 掉 /api 前缀 → 直达 backend
  // 在 Electron file:// 场景，apiPath 拼绝对 base，去掉 /api 前缀
  return `/api${p.startsWith("/") ? p : `/${p}`}`;
}

export async function apiUrl(p: string): Promise<string> {
  const base = await backendBase();
  if (base) {
    return base + (p.startsWith("/") ? p : `/${p}`);
  }
  return apiPath(p);
}
