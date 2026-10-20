import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const tauri = vi.hoisted(() => ({ invoke: vi.fn(), listen: vi.fn() }));
vi.mock('@tauri-apps/api/core', () => tauri);
vi.mock('@tauri-apps/api/event', () => ({ listen: tauri.listen }));
const fetchMock = vi.fn(); const writeText = vi.fn();
beforeEach(() => {
  vi.resetModules(); fetchMock.mockReset(); tauri.invoke.mockReset(); tauri.listen.mockReset(); writeText.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
});
afterEach(() => { Reflect.deleteProperty(window, '__TAURI_INTERNALS__'); vi.restoreAllMocks(); });
const web = async () => (await import('./transport')).host;
const desktop = async () => { Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: {} }); return web(); };
const task = { id: 'task-1', title: 'Example title', url: 'https://example.com/video' };

it('parses JSON responses and preserves readable API errors', async () => {
  const host = await web();
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ value: { title: 'Example' } })));
  await expect(host.command('task.get')).resolves.toEqual({ title: 'Example' });
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ error: 'Task not found' }), { status: 404 }));
  await expect(host.command('task.get')).rejects.toThrow('Task not found');
});

it.each([
  [200, '', 'Server returned an empty response. Please try again.'],
  [200, '<html>secret-password</html>', 'Server returned an invalid response. Please try again.'],
  [502, '<html>secret-password</html>', 'Request failed (HTTP 502). Please try again.'],
  [503, '', 'Service unavailable. Check that the server is running and try again.'],
  [503, '<html>secret-password</html>', 'Service unavailable. Check that the server is running and try again.'],
  [500, '{"error":"password=secret-password"}', 'Request failed (HTTP 500). Please try again.'],
])('reports a safe readable error for HTTP %s responses', async (status, body, message) => {
  fetchMock.mockResolvedValue(new Response(body, { status }));
  await expect((await web()).command('task.get')).rejects.toThrow(message);
});

it.each(['', '<html>secret-password</html>', '{"error":"secret-password"}'])('expires sessions for every unauthorized response body', async body => {
  const expired = vi.fn(); window.addEventListener('huifa-session-expired', expired);
  try {
    fetchMock.mockResolvedValue(new Response(body, { status: 401 }));
    await expect((await web()).command('task.get')).rejects.toThrow('Session expired. Sign in again.');
    expect(expired).toHaveBeenCalledOnce();
  } finally { window.removeEventListener('huifa-session-expired', expired); }
});

it('accepts independent MCP configuration and the previous boolean interface', async () => {
  const host = await web(); fetchMock.mockImplementation(async () => new Response('{}'));
  await host.mcpConfigure({ autoStart: true });
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ autoStart: true });
  await host.mcpConfigure(false);
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ enabled: false });
});

it('copies only a title or source URL in web mode', async () => {
  const host = await web();
  await host.copyTask(task, 'title'); await host.copyTask(task, 'source');
  expect(writeText.mock.calls).toEqual([['Example title'], ['https://example.com/video']]);
  await expect(host.copyTask(task, 'path')).rejects.toThrow('Local file paths are available in the desktop app only.');
  expect(fetchMock).not.toHaveBeenCalled();
});

it('reads clipboard text through the native host on desktop', async () => {
  const readText = vi.fn();
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { readText } });
  const host = await desktop(); tauri.invoke.mockResolvedValue('https://youtu.be/abcdefghijk');
  await expect(host.readClipboard()).resolves.toBe('https://youtu.be/abcdefghijk');
  expect(tauri.invoke).toHaveBeenCalledWith('command', { command: 'host.clipboard_read', args: {} });
  expect(readText).not.toHaveBeenCalled();
  expect(fetchMock).not.toHaveBeenCalled();
});

it('reads clipboard text locally in web mode and reports unavailable access', async () => {
  const readText = vi.fn().mockResolvedValue('https://example.test/video.mp4');
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { readText } });
  const host = await web();
  await expect(host.readClipboard()).resolves.toBe('https://example.test/video.mp4');
  expect(fetchMock).not.toHaveBeenCalled();
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
  await expect(host.readClipboard()).rejects.toThrow('Clipboard is unavailable');
});

it('resolves desktop copy values through the native command', async () => {
  const host = await desktop(); tauri.invoke.mockResolvedValue('D:\\downloads\\example.mp4');
  await host.copyTask(task, 'path');
  expect(tauri.invoke).toHaveBeenCalledWith('task_copy_value', { id: task.id, kind: 'path' });
  expect(writeText).toHaveBeenCalledWith('D:\\downloads\\example.mp4');
});

it('copies the original title without joining the task id in desktop mode', async () => {
  const host = await desktop(); tauri.invoke.mockResolvedValue(task.title);
  await host.copyTask(task, 'title');
  expect(tauri.invoke).toHaveBeenCalledWith('task_copy_value', { id: task.id, kind: 'title' });
  expect(writeText.mock.calls).toEqual([[task.title]]);
});

it('checks release status with the platform host and opens trusted web release pages', async () => {
  const host = await web(); const status = { currentVersion: '0.3.0', latestVersion: '0.3.1', updateAvailable: true, url: 'https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.3.1' };
  fetchMock.mockResolvedValue(new Response(JSON.stringify({ value: status })));
  await expect(host.releaseStatus()).resolves.toEqual(status);
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ command: 'app.release_status', args: {} });
  const open = vi.spyOn(window, 'open').mockReturnValue(null);
  await host.openRelease(status.url);
  expect(open).toHaveBeenCalledWith(status.url, '_blank', 'noopener,noreferrer');
});

it('uses native release commands on desktop', async () => {
  const host = await desktop(); tauri.invoke.mockResolvedValue({ updateAvailable: false });
  await host.releaseStatus();
  expect(tauri.invoke).toHaveBeenCalledWith('release_status', {});
  const url = 'https://github.com/jwwsjlm/huifa-media-downloader/releases/latest';
  await host.openRelease(url);
  expect(tauri.invoke).toHaveBeenCalledWith('open_release', { url });
});

it.each([
  'https://github.com/other/repo/releases/latest',
  'https://github.com.evil.example/jwwsjlm/huifa-media-downloader/releases/latest',
  'https://user:password@github.com/jwwsjlm/huifa-media-downloader/releases/latest',
  'https://github.com/jwwsjlm/huifa-media-downloader/issues',
  'javascript:alert(1)',
])('rejects untrusted release URL %s', async url => {
  const open = vi.spyOn(window, 'open').mockReturnValue(null);
  await expect((await web()).openRelease(url)).rejects.toThrow('Unsupported release URL.');
  expect(open).not.toHaveBeenCalled();
});

it('aborts a hung fetch on timeout and removes timers and abort listeners', async () => {
  vi.useFakeTimers();
  try {
    const host = await web(); const controller = new AbortController();
    const remove = vi.spyOn(controller.signal, 'removeEventListener');
    fetchMock.mockReturnValue(new Promise(() => {}));
    const request = host.command('snapshot', {}, { signal: controller.signal, timeoutMs: 50 });
    const rejected = expect(request).rejects.toMatchObject({ name: 'TimeoutError' });
    await vi.advanceTimersByTimeAsync(50); await rejected;
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true); expect(vi.getTimerCount()).toBe(0);
    expect(remove).toHaveBeenCalledWith('abort', expect.any(Function));
  } finally { vi.useRealTimers(); }
});

it('cancels a native read without waiting for its late result', async () => {
  const host = await desktop(); const controller = new AbortController(); let finish!: (value: unknown) => void;
  tauri.invoke.mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const request = host.command('state.sync', { since: 1 }, { signal: controller.signal });
  const rejected = expect(request).rejects.toMatchObject({ name: 'AbortError' });
  controller.abort(); await rejected; finish({ revision: 2 });
  const cancelled = new AbortController(); cancelled.abort();
  await expect(host.command('snapshot', {}, { signal: cancelled.signal })).rejects.toMatchObject({ name: 'AbortError' });
  expect(tauri.invoke).toHaveBeenCalledTimes(1);
});

it('reuses native state reads without retaining promise observers or abort waiters across 30 caller timeouts', async () => {
  vi.useFakeTimers();
  const originalThen = Promise.prototype.then;
  try {
    const observers = new Map<Promise<unknown>, number>(); const activeSignals = new Set<AbortSignal>();
    const originalAdd = AbortSignal.prototype.addEventListener; const originalRemove = AbortSignal.prototype.removeEventListener;
    Promise.prototype.then = function (this: Promise<unknown>, ...handlers: Parameters<typeof originalThen>) {
      observers.set(this, (observers.get(this) || 0) + 1); return Reflect.apply(originalThen, this, handlers);
    } as typeof originalThen;
    vi.spyOn(AbortSignal.prototype, 'addEventListener').mockImplementation(function (this: AbortSignal, ...args: Parameters<typeof originalAdd>) {
      if (args[0] === 'abort') activeSignals.add(this); return Reflect.apply(originalAdd, this, args);
    });
    vi.spyOn(AbortSignal.prototype, 'removeEventListener').mockImplementation(function (this: AbortSignal, ...args: Parameters<typeof originalRemove>) {
      if (args[0] === 'abort') activeSignals.delete(this); return Reflect.apply(originalRemove, this, args);
    });
    const host = await desktop(); const finish: ((value: unknown) => void)[] = [];
    tauri.invoke.mockImplementation(() => new Promise(resolve => { finish.push(resolve); }));
    for (let cycle = 0; cycle < 30; cycle++) {
      const snapshot = host.command('snapshot', {}, { timeoutMs: 10 }).catch(reason => reason);
      await vi.advanceTimersByTimeAsync(10); expect(await snapshot).toMatchObject({ name: 'TimeoutError' });
      const sync = host.command('state.sync', { since: cycle }, { timeoutMs: 10 }).catch(reason => reason);
      await vi.advanceTimersByTimeAsync(10); expect(await sync).toMatchObject({ name: 'TimeoutError' });
      expect(tauri.invoke).toHaveBeenCalledTimes(2); expect(vi.getTimerCount()).toBe(0);
      expect(activeSignals.size).toBe(0);
      expect(Math.max(...observers.values())).toBeLessThanOrEqual(2);
    }
    finish.forEach(resolve => resolve({ revision: 100 })); await Promise.resolve(); await Promise.resolve();
    tauri.invoke.mockResolvedValue({ revision: 101 });
    await host.command('state.sync', { since: 100 }, { timeoutMs: 10 });
    expect(tauri.invoke).toHaveBeenCalledTimes(3); expect(vi.getTimerCount()).toBe(0);
  } finally { Promise.prototype.then = originalThen; vi.useRealTimers(); }
});

it('cleans successful and partially failed desktop subscriptions for 30 open/close cycles', async () => {
  const host = await desktop(); let active = 0;
  for (let cycle = 0; cycle < 30; cycle++) {
    const failAt = cycle % 3; let call = 0;
    tauri.listen.mockImplementation(async () => {
      if (failAt && ++call === failAt + 1) throw new Error('subscription failed');
      active++; return () => { active--; };
    });
    if (failAt) await expect(host.subscribe(vi.fn(), vi.fn())).rejects.toThrow('subscription failed');
    else { const stop = await host.subscribe(vi.fn(), vi.fn()); expect(active).toBe(3); stop(); stop(); }
    expect(active).toBe(0);
  }
});
