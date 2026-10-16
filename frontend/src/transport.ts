import type { Capabilities, CoreEvent, Json, Session } from './types';

const desktop = '__TAURI_INTERNALS__' in window;
let csrfToken = '';
let capabilities: Capabilities = { desktop, browserCookies: desktop, remoteBrowser: false, updates: desktop, directoryPicker: desktop };

async function response<T>(result: Response): Promise<T> {
  const value = await result.json();
  if (result.status === 401) window.dispatchEvent(new Event('huifa-session-expired'));
  if (!result.ok) throw new Error(value.error || `Request failed (${result.status})`);
  return value as T;
}
async function invoke<T>(name: string, args: Record<string, unknown> = {}): Promise<T> {
  return (await import('@tauri-apps/api/core')).invoke<T>(name, args);
}
async function post<T>(path: string, body: unknown): Promise<T> {
  return response<T>(await fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify(body) }));
}
export const host = {
  desktop,
  capabilities: () => capabilities,
  async session(): Promise<Session> {
    if (desktop) { capabilities = { ...capabilities, ...await invoke<Partial<Capabilities>>('capabilities') }; return { authenticated: true, capabilities }; }
    const result = await fetch('/api/session', { credentials: 'same-origin' });
    if (result.status === 401) return { authenticated: false };
    const value = await response<Session>(result);
    csrfToken = value.csrfToken || '';
    capabilities = { ...capabilities, ...value.capabilities };
    return value;
  },
  async login(account: string, password: string): Promise<void> {
    const value = await post<Session>('/api/login', { account, password });
    csrfToken = value.csrfToken || '';
    capabilities = { ...capabilities, ...value.capabilities };
  },
  async logout(): Promise<void> { await post('/api/logout', {}); csrfToken = ''; },
  async mcpStatus<T>(): Promise<T> { return response<T>(await fetch('/api/mcp', { credentials: 'same-origin', cache: 'no-store' })); },
  async mcpConfigure<T>(enabled: boolean): Promise<T> { return post<T>('/api/mcp', { enabled }); },
  async command<T = Json>(command: string, args: unknown = {}): Promise<T> {
    if (desktop) return invoke<T>('command', { command, args });
    return (await post<{ value: T }>('/api/command', { command, args })).value;
  },
  async subscribe(listener: (event: CoreEvent) => void, connection: (connected: boolean) => void): Promise<() => void> {
    if (desktop) {
      const { listen } = await import('@tauri-apps/api/event');
      const unlisten = await listen<CoreEvent>('huifa-event', event => listener(event.payload));
      const resync = await listen('huifa-resync', () => listener({ kind: 'snapshot.required', data: null }));
      const error = await listen<{ message: string }>('huifa-host-error', event => listener({ kind: 'host.error', data: event.payload }));
      connection(true);
      return () => { unlisten(); resync(); error(); };
    }
    const stream = new EventSource('/api/events');
    stream.onopen = () => connection(true);
    stream.onmessage = event => { try { listener(JSON.parse(event.data) as CoreEvent); } catch { connection(false); } };
    stream.onerror = () => connection(false);
    return () => stream.close();
  },
  assetUrl(id: string, kind = 'cover'): string {
    if (!desktop) return `/api/files/${encodeURIComponent(id)}?kind=${encodeURIComponent(kind)}`;
    return (navigator.userAgent.includes('Windows') ? 'http://huifa-media.localhost/' : 'huifa-media://localhost/') + encodeURIComponent(id) + '?kind=' + encodeURIComponent(kind);
  },
  async result(id: string, action: 'open' | 'reveal' | 'download', kind = 'video'): Promise<void> {
    if (desktop) { await invoke('result_action', { id, kind, action }); return; }
    const link = document.createElement('a');
    link.href = this.assetUrl(id, kind); link.download = ''; document.body.append(link); link.click(); link.remove();
  },
  async directory(defaultPath = ''): Promise<string | null> { return desktop ? invoke('choose_directory', { defaultPath }) : null; },
  async openDownloadDirectory(): Promise<void> { if (desktop) await invoke('open_download_directory'); },
  async paths(): Promise<{ downloadDir: string }> { return desktop ? invoke('command', { command: 'host.paths', args: {} }) : { downloadDir: '/downloads' }; },
  async upload(file: File): Promise<{ id: string; name: string; kind: string }> {
    if (desktop) {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = ''; for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
      return invoke('upload_asset', { name: file.name, data: btoa(binary) });
    }
    const body = new FormData(); body.set('file', file);
    return response(await fetch('/api/upload', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrfToken }, body }));
  },
  async checkUpdate(): Promise<{ version?: string; body?: string }> { return invoke('check_update'); },
  async installUpdate(): Promise<void> { await invoke('install_update'); },
  async notify(title: string, body: string): Promise<void> { if (desktop) await invoke('notify', { title, body }); },
  browserGet: async () => response<{ active: boolean; targetUrl?: string }>(await fetchResult('/api/browser')),
  browserStart: (url: string) => post('/api/browser/start', { url }),
  browserInput: (input: unknown) => post('/api/browser/input', input),
  browserSave: (name: string) => post('/api/browser/save', { name }),
  browserStop: () => post('/api/browser/stop', {}),
};

async function fetchResult(path: string): Promise<Response> { return fetch(path, { credentials: 'same-origin' }); }
