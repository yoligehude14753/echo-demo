/* eslint-disable @typescript-eslint/no-var-requires */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("echo", {
  isElectron: true,
  getBackendHost: () => ipcRenderer.invoke("echo:backend-host"),
});
