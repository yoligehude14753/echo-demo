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
  getBackendHost?: () => Promise<string>;
  // Phase 1 P1.5/P1.6 BackendSupervisor IPC
  onBackendStatus?: (cb: (status: unknown) => void) => () => void;
  manualRestartBackend?: () => Promise<{ ok: boolean }>;
  // Phase 3 P3.5 麦克风权限
  getMicStatus?: () => Promise<ElectronMicStatus>;
  requestMic?: () => Promise<boolean>;
  openMicSystemPrefs?: () => Promise<{ ok: boolean; reason?: string }>;
}

declare global {
  interface Window {
    echo?: ElectronEchoBridge;
  }
}

let cachedBase: string | null = null;

export async function backendBase(): Promise<string> {
  if (cachedBase !== null) return cachedBase;

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
