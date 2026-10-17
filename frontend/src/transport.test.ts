import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const tauri = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock('@tauri-apps/api/core', () => tauri);
const fetchMock = vi.fn(); const writeText = vi.fn();
beforeEach(() => {
  vi.resetModules(); fetchMock.mockReset(); tauri.invoke.mockReset(); writeText.mockReset();
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
