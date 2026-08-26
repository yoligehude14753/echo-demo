import { contextBridge, ipcRenderer } from 'electron'

// Expose a safe API to the renderer process via window.echo
contextBridge.exposeInMainWorld('echo', {
  // Workspace management
  workspace: {
    list: () => ipcRenderer.invoke('workspace:list'),
    add: () => ipcRenderer.invoke('workspace:add'),
    remove: (path: string) => ipcRenderer.invoke('workspace:remove', path),
  },

  // File operations (within authorized workspaces only)
  file: {
    readContext: (dirPath: string) => ipcRenderer.invoke('file:readContext', dirPath),
    readFile: (filePath: string, workspaces: string[]) =>
      ipcRenderer.invoke('file:readFile', filePath, workspaces),
    searchFiles: (query: string, topK?: number) =>
      ipcRenderer.invoke('file:searchFiles', query, topK),
  },

  // System
  system: {
    openExternal: (url: string) => ipcRenderer.invoke('system:openExternal', url),
    getVersion: () => ipcRenderer.invoke('system:getVersion'),
    getAutoLaunch: () => ipcRenderer.invoke('system:getAutoLaunch'),
    setAutoLaunch: (enabled: boolean) => ipcRenderer.invoke('system:setAutoLaunch', enabled),
  },

  // USB Serial provisioning
  serial: {
    list: () => ipcRenderer.invoke('serial:list'),
    scan: (port: string) => ipcRenderer.invoke('serial:scan', port),
    provision: (port: string, ssid: string, pass: string, wsUrl: string) =>
      ipcRenderer.invoke('serial:provision', port, ssid, pass, wsUrl),
  },
})

// Type declaration merged in renderer via global.d.ts
