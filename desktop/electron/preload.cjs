/* eslint-disable @typescript-eslint/no-var-requires */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("echo", {
  isElectron: true,
  isPublicDemo: ipcRenderer.sendSync("echo:is-public-demo") === true,
  backendHost: ipcRenderer.sendSync("echo:backend-host-sync"),
  backendRouting: ipcRenderer.sendSync("echo:backend-routing-sync"),
  getBackendHost: () => ipcRenderer.invoke("echo:backend-host"),
  getBackendRouting: () => ipcRenderer.invoke("echo:backend-routing"),
  getBackendContract: () => ipcRenderer.invoke("echo:backend-contract"),
  getModelRuntimeIdentity: () => ipcRenderer.invoke("model-runtime:get-identity"),
  onModelRuntimeIdentity: (cb) => {
    const handler = (_event, payload) => cb(payload);
    ipcRenderer.on("model-runtime:identity", handler);
    return () => ipcRenderer.removeListener("model-runtime:identity", handler);
  },
  onModelRuntimeFallback: (cb) => {
    const handler = (_event, payload) => cb(payload);
    ipcRenderer.on("model-runtime:fallback", handler);
    return () => ipcRenderer.removeListener("model-runtime:fallback", handler);
  },
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

  // 更新检查：桌面打包版由主进程读取 GitHub prerelease API，按平台选择唯一
  // 资产并校验 API asset.digest；renderer 只能触发受控检查/安装状态机。
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

  // Desktop background residency. Renderer reports only a compact,
  // fail-closed projection; the main process owns tray/menu lifecycle.
  notifyCaptureState: (status) =>
    ipcRenderer.invoke("background:set-status", status),
  onCaptureCommand: (cb) => {
    const handler = (_event, command) => cb(command);
    ipcRenderer.on("background:command", handler);
    return () => ipcRenderer.removeListener("background:command", handler);
  },
  getLoginItemSettings: () =>
    ipcRenderer.invoke("background:get-login-item"),
  setOpenAtLogin: (openAtLogin) =>
    ipcRenderer.invoke("background:set-login-item", openAtLogin === true),

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
