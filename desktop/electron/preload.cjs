/* eslint-disable @typescript-eslint/no-var-requires */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("echo", {
  isElectron: true,
  getBackendHost: () => ipcRenderer.invoke("echo:backend-host"),

  // BackendSupervisor 状态推送（P1.5）
  // payload = {state, ...} 详见 main.cjs emitStatus
  // 注意：renderer mount 早于 backend ready，主进程会缓存最后一条 status，
  // 在 did-finish-load 时 replay，所以这里订阅一次就能拿到当前状态
  onBackendStatus: (cb) => {
    const handler = (_event, payload) => cb(payload);
    ipcRenderer.on("backend:status", handler);
    return () => ipcRenderer.removeListener("backend:status", handler);
  },

  // degraded UI 上"重启 backend"按钮触发；主进程清 backoff + 重新 spawn
  manualRestartBackend: () => ipcRenderer.invoke("backend:manual-restart"),

  // 麦克风权限（P3.5）
  // - getMicStatus: macOS systemPreferences.getMediaAccessStatus("microphone")
  //   返回 'not-determined'|'granted'|'denied'|'restricted'|'unknown'（非 mac → 'unknown'）
  // - requestMic: 触发 macOS 系统弹窗请求权限；返回 true=用户点允许，false=拒绝
  // - openMicSystemPrefs: 一键打开 macOS 隐私与安全-麦克风设置页
  getMicStatus: () => ipcRenderer.invoke("mic:status"),
  requestMic: () => ipcRenderer.invoke("mic:request"),
  openMicSystemPrefs: () => ipcRenderer.invoke("mic:open-system-prefs"),
});
