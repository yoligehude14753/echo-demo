/* eslint-disable @typescript-eslint/no-var-requires */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("echo", {
  isElectron: true,
  isPublicDemo: ipcRenderer.sendSync("echo:is-public-demo") === true,
  getBackendHost: () => ipcRenderer.invoke("echo:backend-host"),
  getShareBackendHost: () => ipcRenderer.invoke("echo:share-backend-host"),
  loadLocalLegacyHistory: () =>
    ipcRenderer.invoke("echo:load-local-legacy-history"),

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

  // 更新检查：桌面打包版走 electron-updater；dev/浏览器/Android 由前端走 GitHub
  // Release fallback。桌面端后台下载完成后由 renderer 请求用户确认安装。
  checkForUpdates: () => ipcRenderer.invoke("updates:check"),
  getUpdateStatus: () => ipcRenderer.invoke("updates:last-status"),
  installUpdate: () => ipcRenderer.invoke("updates:download-and-install"),
  openReleasePage: () => ipcRenderer.invoke("updates:open-release"),
  openExternal: (url) => ipcRenderer.invoke("shell:open-external", url),
  onUpdateStatus: (cb) => {
    const handler = (_event, payload) => cb(payload);
    ipcRenderer.on("updates:status", handler);
    return () => ipcRenderer.removeListener("updates:status", handler);
  },

  // 麦克风权限（P3.5）
  // - getMicStatus: macOS systemPreferences.getMediaAccessStatus("microphone")
  //   返回 'not-determined'|'granted'|'denied'|'restricted'|'unknown'（非 mac → 'unknown'）
  // - requestMic: 触发 macOS 系统弹窗请求权限；返回 true=用户点允许，false=拒绝
  // - openMicSystemPrefs: 一键打开 macOS 隐私与安全-麦克风设置页
  getMicStatus: () => ipcRenderer.invoke("mic:status"),
  requestMic: () => ipcRenderer.invoke("mic:request"),
  openMicSystemPrefs: () => ipcRenderer.invoke("mic:open-system-prefs"),

  // P4.1 M4 产物预览：把 backend 落盘的绝对路径交给系统默认应用打开。
  // 主要用途：pptx 浏览器无法原生渲染，调 macOS Keynote / Office；
  //          docx / xlsx 用户也可以选择在系统应用打开做二次编辑。
  // 返回 Promise<void>；失败时 reject(new Error(reason))，前端 catch 后提示用户。
  openArtifactInSystem: (filePath) =>
    ipcRenderer.invoke("echo:open-artifact-in-system", filePath),

  // P4-fix-rag-chat（2026-05-28）：SettingsPanel "工作区目录" section 用。
  // 调系统 dialog.showOpenDialog 选目录；用户取消时 resolve(null)，
  // 选了一个目录 resolve(absolutePath)；调用失败 reject(Error)。
  pickDirectory: (opts) => ipcRenderer.invoke("workspace:pick-directory", opts ?? {}),
  getLocalWorkspaceStatus: () => ipcRenderer.invoke("workspace:local-status"),
  addLocalWorkspaceDir: (dir) => ipcRenderer.invoke("workspace:add-local-dir", dir),
  removeLocalWorkspaceDir: (dir) =>
    ipcRenderer.invoke("workspace:remove-local-dir", dir),
  scanLocalWorkspaces: () => ipcRenderer.invoke("workspace:scan-local"),
  clearLocalWorkspaceDocs: () => ipcRenderer.invoke("workspace:clear-local-docs"),
});
