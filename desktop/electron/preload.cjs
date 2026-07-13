/* eslint-disable @typescript-eslint/no-var-requires */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("echo", {
  isElectron: true,
  isPublicDemo: ipcRenderer.sendSync("echo:is-public-demo") === true,
  backendHost: ipcRenderer.sendSync("echo:backend-host-sync"),
  getBackendHost: () => ipcRenderer.invoke("echo:backend-host"),
  getBackendContract: () => ipcRenderer.invoke("echo:backend-contract"),
  getShareBackendHost: () => ipcRenderer.invoke("echo:share-backend-host"),
  loadLocalLegacyHistory: () =>
    ipcRenderer.invoke("echo:load-local-legacy-history"),
  ensurePublicSession: () => ipcRenderer.invoke("credential:ensure-session"),
  renewPublicSession: () => ipcRenderer.invoke("credential:renew-session"),
  rotatePublicCredential: (sessionToken) =>
    ipcRenderer.invoke("credential:rotate", sessionToken),
  clearPublicCredential: () => ipcRenderer.invoke("credential:clear-public"),

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

  // 本机产物预览：main 仅接受受控生成根内的真实普通文件；public/remote
  // runtime 会拒绝此通道并由 renderer 改走 authenticated download。
  openArtifactInSystem: (filePath) =>
    ipcRenderer.invoke("echo:open-artifact-in-system", filePath),
  downloadArtifactBlob: (blobUrl, suggestedFilename) =>
    ipcRenderer.invoke(
      "echo:download-renderer-blob",
      blobUrl,
      suggestedFilename,
    ),

  // P4-fix-rag-chat（2026-05-28）：SettingsPanel "工作区目录" section 用。
  // 调系统 dialog.showOpenDialog 选目录；用户取消时 resolve(null)，
  // 选中时只返回 origin-bound opaque handle，绝对路径始终留在 main。
  pickDirectory: (context, opts) =>
    ipcRenderer.invoke("workspace:pick-directory", context ?? {}, opts ?? {}),
  getLocalWorkspaceStatus: (context) =>
    ipcRenderer.invoke("workspace:local-status", context ?? {}),
  addLocalWorkspaceDir: (context, dir) =>
    ipcRenderer.invoke("workspace:add-local-dir", context ?? {}, dir),
  removeLocalWorkspaceDir: (context, dir) =>
    ipcRenderer.invoke("workspace:remove-local-dir", context ?? {}, dir),
  scanLocalWorkspaces: (context) =>
    ipcRenderer.invoke("workspace:scan-local", context ?? {}),
  clearLocalWorkspaceDocs: (context) =>
    ipcRenderer.invoke("workspace:clear-local-docs", context ?? {}),
  cancelLocalWorkspaceOperations: (context) =>
    ipcRenderer.invoke("workspace:cancel-origin-operations", context ?? {}),
});
