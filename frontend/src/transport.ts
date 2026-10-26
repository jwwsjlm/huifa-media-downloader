import type { Capabilities, CoreEvent, Json, Session, Task, UpdateStatus } from './types';

type McpConfiguration = { enabled?: boolean; autoStart?: boolean; port?: number };
type ReleaseStatus = { currentVersion: string; latestVersion: string; updateAvailable: boolean; url: string; publishedAt?: string };
export type RequestOptions = { signal?: AbortSignal; timeoutMs?: number };

const desktop = '__TAURI_INTERNALS__' in window;
let csrfToken = '';
let capabilities: Capabilities = { desktop, browserCookies: desktop, remoteBrowser: false, updates: desktop, directoryPicker: desktop };
const nativeReadCommands = new Set(['snapshot', 'state.sync', 'tools.check']);
type NativeWaiter = { resolve: (value: unknown) => void; reject: (reason: unknown) => void; cleanup: () => void };
type NativeRead = { waiters: Set<NativeWaiter> };
const nativeReads = new Map<string, NativeRead>();

async function response<T>(result: Response): Promise<T> {
  if (result.status === 401) window.dispatchEvent(new Event('huifa-session-expired'));
  const body = await result.text();
  let value: unknown;
  try { value = JSON.parse(body); } catch { /* Proxies may return an empty body or HTML. */ }
  if (result.status === 401) throw new Error('Session expired. Sign in again.');
  if (result.status === 503) throw new Error('Service unavailable. Check that the server is running and try again.');
  if (!result.ok) {
    const message = value && typeof value === 'object' && 'error' in value ? value.error : undefined;
    if (typeof message === 'string' && message && !/<[^>]*>|(?:authorization|password|api[-_ ]?key|token)\s*[:=]/i.test(message)) throw new Error(message);
    throw new Error(`Request failed (HTTP ${result.status}). Please try again.`);
  }
  if (!body.trim()) throw new Error('Server returned an empty response. Please try again.');
  if (value === undefined) throw new Error('Server returned an invalid response. Please try again.');
  return value as T;
}
async function invoke<T>(name: string, args: Record<string, unknown> = {}): Promise<T> {
  return (await import('@tauri-apps/api/core')).invoke<T>(name, args);
}
function nativeCommand<T>(command: string, args: unknown, signal?: AbortSignal): Promise<T> {
  if (!nativeReadCommands.has(command)) return invoke<T>('command', { command, args });
  if (signal?.aborted) return Promise.reject(signal.reason);
  let pending = nativeReads.get(command);
  if (!pending) {
    pending = { waiters: new Set() }; nativeReads.set(command, pending);
    const entry = pending;
    const settle = (value: unknown, failed: boolean) => {
      nativeReads.delete(command);
      for (const waiter of entry.waiters) { waiter.cleanup(); if (failed) waiter.reject(value); else waiter.resolve(value); }
      entry.waiters.clear();
    };
    // Exactly one observer owns the uncancellable invoke. Timed-out callers are
    // removed from the set instead of retaining a reaction on its pending promise.
    void invoke<T>('command', { command, args }).then(value => settle(value, false), reason => settle(reason, true));
  }
  const entry = pending;
  return new Promise<T>((resolve, reject) => {
    const abort = () => { entry.waiters.delete(waiter); waiter.cleanup(); reject(signal?.reason); };
    const waiter: NativeWaiter = { resolve: value => resolve(value as T), reject, cleanup: () => signal?.removeEventListener('abort', abort) };
    entry.waiters.add(waiter); signal?.addEventListener('abort', abort, { once: true });
  });
}
async function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return response<T>(await fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify(body), signal }));
}
async function request<T>(operation: (signal: AbortSignal) => Promise<T>, options: RequestOptions): Promise<T> {
  const controller = new AbortController(); let timer: ReturnType<typeof setTimeout> | undefined;
  let rejectCancelled: (reason: unknown) => void = () => {};
  const cancelled = new Promise<never>((_resolve, reject) => { rejectCancelled = reject; });
  const cancel = (reason: DOMException) => { controller.abort(reason); rejectCancelled(reason); };
  const abort = () => cancel(new DOMException('Request cancelled.', 'AbortError'));
  if (options.signal?.aborted) throw new DOMException('Request cancelled.', 'AbortError');
  options.signal?.addEventListener('abort', abort, { once: true });
  if (options.timeoutMs !== undefined) timer = setTimeout(() => cancel(new DOMException('Request timed out.', 'TimeoutError')), options.timeoutMs);
  try { return await Promise.race([operation(controller.signal), cancelled]); }
  finally { if (timer !== undefined) clearTimeout(timer); options.signal?.removeEventListener('abort', abort); }
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
  async mcpConfigure<T>(config: boolean | McpConfiguration): Promise<T> { return post<T>('/api/mcp', typeof config === 'boolean' ? { enabled: config } : config); },
  async command<T = Json>(command: string, args: unknown = {}, options?: RequestOptions): Promise<T> {
    const execute = async (signal?: AbortSignal) => desktop ? nativeCommand<T>(command, args, signal) : (await post<{ value: T }>('/api/command', { command, args }, signal)).value;
    // Native invokes cannot be interrupted, but callers can stop waiting and
    // discard their result. Fetch receives the same cancellation signal.
    return options ? request(execute, options) : execute();
  },
  async subscribe(listener: (event: CoreEvent) => void, connection: (connected: boolean) => void): Promise<() => void> {
    if (desktop) {
      const { listen } = await import('@tauri-apps/api/event');
      const cleanups: (() => void)[] = [];
      const dispose = () => { for (const cleanup of cleanups.splice(0)) cleanup(); };
      try {
        cleanups.push(await listen<CoreEvent>('huifa-event', event => listener(event.payload)));
        cleanups.push(await listen('huifa-resync', () => listener({ kind: 'snapshot.required', data: null })));
        cleanups.push(await listen<{ message: string }>('huifa-host-error', event => listener({ kind: 'host.error', data: event.payload })));
        connection(true);
        return dispose;
      } catch (reason) { dispose(); throw reason; }
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
  async copyTask(task: Pick<Task, 'id' | 'title' | 'url'>, kind: 'title' | 'source' | 'path'): Promise<void> {
    if (!desktop && kind === 'path') throw new Error('Local file paths are available in the desktop app only.');
    const value = desktop ? await invoke<string>('task_copy_value', { id: task.id, kind }) : kind === 'title' ? task.title : task.url;
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard is unavailable. Allow clipboard access and try again.');
    await navigator.clipboard.writeText(value);
  },
  async readClipboard(): Promise<string> {
    if (desktop) return invoke('command', { command: 'host.clipboard_read', args: {} });
    if (!navigator.clipboard?.readText) throw new Error('Clipboard is unavailable. Allow clipboard access and try again.');
    return navigator.clipboard.readText();
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
  async updateStatus(): Promise<UpdateStatus> { return invoke('update_status'); },
  async checkUpdate(): Promise<UpdateStatus> { return invoke('check_update'); },
  async downloadUpdate(): Promise<UpdateStatus> { return invoke('download_update'); },
  async subscribeUpdate(listener: (status: UpdateStatus) => void): Promise<() => void> {
    const { listen } = await import('@tauri-apps/api/event');
    return listen<UpdateStatus>('huifa-update', event => listener(event.payload));
  },
  async installUpdate(): Promise<void> { await invoke('install_update'); },
  async releaseStatus<T = ReleaseStatus>(): Promise<T> { return desktop ? invoke<T>('release_status') : this.command<T>('app.release_status'); },
  async openProject(): Promise<void> {
    if (desktop) await invoke('open_project');
    else window.open('https://github.com/jwwsjlm/huifa-media-downloader', '_blank', 'noopener,noreferrer');
  },
  async openRelease(url: string): Promise<void> {
    let release: URL;
    try { release = new URL(url); } catch { throw new Error('Unsupported release URL.'); }
    if (release.protocol !== 'https:' || release.hostname !== 'github.com' || release.port || release.username || release.password || release.search || release.hash || !/^\/jwwsjlm\/huifa-media-downloader\/releases(?:\/latest|\/tag\/[^/]+)?\/?$/.test(release.pathname)) throw new Error('Unsupported release URL.');
    if (desktop) await invoke('open_release', { url: release.href });
    else window.open(release.href, '_blank', 'noopener,noreferrer');
  },
  async notify(title: string, body: string): Promise<void> { if (desktop) await invoke('notify', { title, body }); },
  browserGet: async () => response<{ active: boolean; targetUrl?: string }>(await fetchResult('/api/browser')),
  browserStart: (url: string) => post('/api/browser/start', { url }),
  browserInput: (input: unknown) => post('/api/browser/input', input),
  browserSave: (name: string) => post('/api/browser/save', { name }),
  browserStop: () => post('/api/browser/stop', {}),
};

async function fetchResult(path: string): Promise<Response> { return fetch(path, { credentials: 'same-origin' }); }
