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

interface ElectronEchoBridge {
  isElectron?: boolean;
  isPublicDemo?: boolean;
  getBackendHost?: () => Promise<string>;
  getShareBackendHost?: () => Promise<string>;
  // Phase 1 P1.5/P1.6 BackendSupervisor IPC
  onBackendStatus?: (cb: (status: unknown) => void) => () => void;
  manualRestartBackend?: () => Promise<{ ok: boolean }>;
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

function normalizeBackendBase(raw: string | null | undefined): string | null {
  const v = raw?.trim().replace(/\/+$/, "");
  if (!v) return null;
  if (!/^https?:\/\//.test(v)) return `http://${v}`;
  return v;
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
  return (
    isPublicDesktopDemo() ||
    (isNativeMobile() && isDefaultPublicBackend(configured ?? DEFAULT_ANDROID_BACKEND_BASE))
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
