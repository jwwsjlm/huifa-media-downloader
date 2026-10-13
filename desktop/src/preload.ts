import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('huifa', Object.freeze({
  call: (target: string, action: string, ...args: unknown[]) => ipcRenderer.invoke('huifa:command', { target, action, args }),
  subscribe: (listener: (event: HostEvent) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, payload: HostEvent) => listener(payload);
    ipcRenderer.on('huifa:event', handler);
    return () => ipcRenderer.removeListener('huifa:event', handler);
  },
  choosePath: (options: PathOptions) => ipcRenderer.invoke('huifa:path', options),
  openExternal: (url: string) => ipcRenderer.invoke('huifa:external', url),
}));
