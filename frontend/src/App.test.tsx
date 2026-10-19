import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { AppProvider } from './state';
import { makeSnapshot } from './test-fixtures';
import type { CoreEvent } from './types';
import { version as buildVersion } from '../package.json';

const transport = vi.hoisted(() => ({ desktop: false, listener: null as null | ((event: CoreEvent) => void), command: vi.fn(), releaseStatus: vi.fn(), openRelease: vi.fn(), openProject: vi.fn(), copyTask: vi.fn(), result: vi.fn(), notify: vi.fn(), readClipboard: vi.fn() }));
vi.mock('./transport', () => ({ host: {
  get desktop() { return transport.desktop; }, capabilities: () => ({ browserCookies: false, remoteBrowser: false, updates: false }),
  session: async () => ({ authenticated: true }), subscribe: async (listener: (event: CoreEvent) => void, connection: (value: boolean) => void) => { transport.listener = listener; connection(true); return () => { transport.listener = null; }; },
  command: (command: string, args?: { ids?: string[] }) => command === 'media.availability' ? Promise.resolve((args?.ids || []).map(id => ({ id, status: 'available' }))) : args === undefined ? transport.command(command) : transport.command(command, args), assetUrl: (id: string, kind = 'cover') => `/api/files/${id}?kind=${kind}`, notify: transport.notify, releaseStatus: transport.releaseStatus, openRelease: transport.openRelease, openProject: transport.openProject, copyTask: transport.copyTask, result: transport.result, readClipboard: transport.readClipboard, paths: async () => ({ downloadDir: 'D:/downloads' }),
} }));
beforeEach(() => {
  transport.desktop = false; transport.readClipboard.mockReset().mockImplementation(async () => navigator.clipboard.readText());
  transport.command.mockReset(); transport.command.mockImplementation(async command => command === 'snapshot' ? makeSnapshot() : command === 'cookies.list' ? [] : true);
  transport.releaseStatus.mockReset().mockResolvedValue({ currentVersion:'0.3.0', latestVersion:'0.3.0', updateAvailable:false, url:'https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.3.0' });
  transport.openRelease.mockReset().mockResolvedValue(undefined);
  transport.openProject.mockReset().mockResolvedValue(undefined);
  transport.copyTask.mockReset().mockResolvedValue(undefined); transport.result.mockReset().mockResolvedValue(undefined); transport.notify.mockReset().mockResolvedValue(undefined);
});
afterEach(() => { vi.useRealTimers(); });
describe('live interface stability', () => {
  it('starts clipboard monitoring only after saving and imports new copied links once using current settings', async () => {
    vi.useFakeTimers(); transport.desktop = true;
    const snapshot = makeSnapshot(); snapshot.settings.downloadOptions = { contentMode: 'video', quality: '1080p', collectionMode: 'single' };
    let copied = 'plain text without a link'; transport.readClipboard.mockImplementation(async () => copied);
    transport.command.mockImplementation(async (command, args) => command === 'snapshot' ? snapshot : command === 'settings.save' ? (snapshot.settings = args.settings) : command === 'tasks.add' ? { ids: [args.urls[0]], results: [{ id: args.urls[0], status: 'queued', action: 'added' }] } : []);
    render(<AppProvider><App /></AppProvider>); await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(transport.readClipboard).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Automatically download links copied to the clipboard' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(transport.readClipboard).not.toHaveBeenCalled();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save settings' })); });
    expect(transport.readClipboard).toHaveBeenCalledTimes(1);
    expect(transport.command.mock.calls.some(([command]) => command === 'tasks.add')).toBe(false);
    copied = 'Watch https://youtu.be/abcdefghijk and https://youtu.be/zyxwvutsrqp';
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(transport.command).toHaveBeenCalledWith('tasks.add', { urls: ['https://youtu.be/abcdefghijk'], options: expect.objectContaining({ quality: '1080p', collectionMode: 'single' }), report: true });
    expect(transport.command).toHaveBeenCalledWith('tasks.add', { urls: ['https://youtu.be/zyxwvutsrqp'], options: expect.objectContaining({ quality: '1080p' }), report: true });
    copied = 'https://youtu.be/zyxwvutsrqp\nhttps://youtu.be/abcdefghijk\nhttps://youtu.be/abcdefghijk';
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(transport.command.mock.calls.filter(([command]) => command === 'tasks.add')).toHaveLength(2);
    snapshot.settings = { ...snapshot.settings, downloadOptions: { ...snapshot.settings.downloadOptions, quality: '720p' } };
    act(() => transport.listener?.({ kind: 'settings.updated', data: snapshot.settings }));
    copied = 'https://example.test/new.mp4';
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(transport.command).toHaveBeenCalledWith('tasks.add', { urls: [copied], options: expect.objectContaining({ quality: '720p' }), report: true });
  });
  it('keeps a monitored link for a later tick when manual adding or a dialog is busy', async () => {
    vi.useFakeTimers(); transport.desktop = true;
    const snapshot = makeSnapshot(); snapshot.settings.clipboardMonitoring = true; snapshot.settings.downloadOptions = { collectionMode: 'single' };
    const copied = 'https://youtu.be/zyxwvutsrqp'; const manual = 'https://youtu.be/abcdefghijk';
    let finishRead!: (value: string) => void; let finishAdd!: (value: unknown) => void;
    transport.readClipboard.mockImplementationOnce(() => new Promise(resolve => { finishRead = resolve; })).mockResolvedValue(copied);
    transport.command.mockImplementation(async (command, args) => command === 'snapshot' ? snapshot : command === 'tasks.add' ? args.urls[0] === manual ? new Promise(resolve => { finishAdd = resolve; }) : { ids: [copied], results: [{ id: copied, status: 'queued', action: 'added' }] } : []);
    render(<AppProvider><App /></AppProvider>); await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(transport.readClipboard).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: manual } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Add downloads' })); finishRead(copied); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(transport.readClipboard).toHaveBeenCalledTimes(1);
    expect(transport.command.mock.calls.filter(([command]) => command === 'tasks.add')).toHaveLength(1);
    await act(async () => finishAdd({ ids: ['manual'], results: [{ id: 'manual', status: 'queued', action: 'added' }] }));
    fireEvent.click(screen.getByRole('button', { name: 'Quick options' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(transport.readClipboard).toHaveBeenCalledTimes(1);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(transport.readClipboard).toHaveBeenCalledTimes(2);
    expect(transport.command).toHaveBeenCalledWith('tasks.add', { urls: [copied], options: expect.anything(), report: true });
  });
  it.each(['disable', 'unmount'])('discards a pending clipboard read after %s', async action => {
    vi.useFakeTimers(); transport.desktop = true;
    const snapshot = makeSnapshot(); snapshot.settings.clipboardMonitoring = true;
    let finish!: (value: string) => void; transport.readClipboard.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : []);
    const view = render(<AppProvider><App /></AppProvider>); await act(async () => {});
    expect(transport.readClipboard).toHaveBeenCalledTimes(1);
    if (action === 'disable') {
      snapshot.settings = { ...snapshot.settings, clipboardMonitoring: false };
      act(() => transport.listener?.({ kind: 'settings.updated', data: snapshot.settings }));
    } else view.unmount();
    await act(async () => { finish('https://youtu.be/abcdefghijk'); await vi.advanceTimersByTimeAsync(3000); });
    expect(transport.command.mock.calls.some(([command]) => command === 'tasks.add')).toBe(false);
    expect(transport.readClipboard).toHaveBeenCalledTimes(1);
  });
  it('reports clipboard read failures once and recovers while the desktop app is in the background', async () => {
    vi.useFakeTimers(); transport.desktop = true; vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    const snapshot = makeSnapshot(); snapshot.settings.clipboardMonitoring = true;
    transport.readClipboard.mockRejectedValueOnce(new Error('Permission denied at secret-path')).mockRejectedValueOnce(new Error('Permission denied')).mockResolvedValue('https://youtu.be/abcdefghijk');
    transport.command.mockImplementation(async (command, args) => command === 'snapshot' ? snapshot : command === 'tasks.add' ? { ids: ['new'], results: [{ id: 'new', status: 'queued', action: 'added' }] } : []);
    render(<AppProvider><App /></AppProvider>); await act(async () => {});
    expect(screen.getByRole('status').textContent).toBe('Clipboard monitoring could not read text. Check clipboard access; the app will keep trying.');
    expect(screen.queryByText(/secret-path/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss message' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.queryByRole('status')).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(transport.command).toHaveBeenCalledWith('tasks.add', { urls: ['https://youtu.be/abcdefghijk'], options: expect.anything(), report: true });
    expect(transport.readClipboard).toHaveBeenCalledTimes(3);
  });
  it('does not monitor the clipboard on the web even when the saved setting is enabled', async () => {
    vi.useFakeTimers(); const snapshot = makeSnapshot(); snapshot.settings.clipboardMonitoring = true;
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : []);
    render(<AppProvider><App /></AppProvider>); await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(transport.readClipboard).not.toHaveBeenCalled();
  });
  it('keeps clipboard reads single-flight when monitoring is disabled and enabled before a read finishes', async () => {
    vi.useFakeTimers(); transport.desktop = true;
    const snapshot = makeSnapshot(); snapshot.settings.clipboardMonitoring = true;
    let finish!: (value: string) => void;
    transport.readClipboard.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })).mockResolvedValue('https://youtu.be/zyxwvutsrqp');
    transport.command.mockImplementation(async (command, args) => command === 'snapshot' ? snapshot : command === 'tasks.add' ? { ids: ['new'], results: [{ id: 'new', status: 'queued', action: 'added' }] } : []);
    render(<AppProvider><App /></AppProvider>); await act(async () => {});
    snapshot.settings = { ...snapshot.settings, clipboardMonitoring: false };
    act(() => transport.listener?.({ kind: 'settings.updated', data: snapshot.settings }));
    snapshot.settings = { ...snapshot.settings, clipboardMonitoring: true };
    act(() => transport.listener?.({ kind: 'settings.updated', data: snapshot.settings }));
    expect(transport.readClipboard).toHaveBeenCalledTimes(1);
    await act(async () => finish('https://youtu.be/abcdefghijk'));
    expect(transport.command.mock.calls.some(([command]) => command === 'tasks.add')).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(transport.readClipboard).toHaveBeenCalledTimes(2);
    expect(transport.command).toHaveBeenCalledWith('tasks.add', { urls: ['https://youtu.be/zyxwvutsrqp'], options: expect.anything(), report: true });
  });
  it('keeps monitored manual formats in the existing confirmation flow and does not reopen a cancelled choice', async () => {
    vi.useFakeTimers(); transport.desktop = true;
    const snapshot = makeSnapshot(); snapshot.settings.clipboardMonitoring = true; snapshot.settings.downloadOptions = { contentMode: 'manual' };
    transport.readClipboard.mockResolvedValue('https://youtu.be/abcdefghijk');
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'probe.video' ? { title: 'Manual video', formats: [{ formatId: '137', vcodec: 'h264', acodec: 'none' }] } : []);
    render(<AppProvider><App /></AppProvider>); await act(async () => {});
    expect(screen.getByRole('dialog', { name: 'Choose download format' })).toBeTruthy();
    expect(transport.command.mock.calls.some(([command]) => command === 'tasks.add')).toBe(false);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(transport.command.mock.calls.filter(([command]) => command === 'probe.video')).toHaveLength(1);
  });
  it('shows completion time beside the row status using the saved format, only after completion', async () => {
    const snapshot = makeSnapshot(); snapshot.settings.completedTimeFormat = 'YYMMDD HH:mm';
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    const row = screen.getByRole('option', { name: /Alpha/ });
    const completedAt = new Date(2026, 9, 18, 23, 7, 59).toISOString();
    expect(row.querySelector('time')).toBeNull();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'completed', stage: 'completed', completedAt } }));
    expect(row.querySelector('time')?.textContent).toBe(' · 261018 23:07');
    expect(row.querySelector('time')?.dateTime).toBe(completedAt);
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'queued', completedAt: null } }));
    expect(row.querySelector('time')).toBeNull();
  });
  it('updates downloaded sizes through stream changes and pause, then shows the final completed file size', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    const row = within(screen.getByRole('option', { name: /Alpha/ }));
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', downloadedBytes: 1048576, totalBytes: null } }));
    expect(row.getByText('1.0 MiB')).toBeTruthy();
    expect(row.getByText('Downloaded')).toBeTruthy();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', downloadedBytes: 2097152, totalBytes: 4194304, totalBytesEstimated: true } }));
    expect(row.getByText('2.0 MiB')).toBeTruthy(); expect(row.getByText('/ ≈ 4.0 MiB')).toBeTruthy();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', stage: 'downloading_audio_2', downloadedBytes: 3145728, totalBytes: 4194304, totalBytesEstimated: false } }));
    expect(row.getByText('3.0 MiB')).toBeTruthy(); expect(row.getByText('/ 4.0 MiB')).toBeTruthy();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'paused' } }));
    expect(row.getByText('3.0 MiB')).toBeTruthy();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'completed', stage: 'completed', size: 5242880 } }));
    expect(row.getByText('5.0 MiB')).toBeTruthy(); expect(row.queryByText('/ 4.0 MiB')).toBeNull();
  });
  it('updates total download speed independently of the visible task filter', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    const total = screen.getByLabelText('Total download speed');
    expect(total.textContent).toContain('2.0 KiB/s');
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search tasks' }), { target: { value: 'Alpha' } });
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'b', speed: 3072 } }));
    expect(total.textContent).toContain('4.0 KiB/s');
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'b', status: 'paused' } }));
    expect(total.textContent).toContain('1.0 KiB/s');
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'completed' } }));
    expect(total.textContent).toContain('0 B/s');
  });
  it('loads tasks before slow runtime and release checks and keeps tools through fast refreshes', async () => {
    const snapshot = makeSnapshot(); delete snapshot.tools;
    let finishTools!: (value: unknown) => void; let finishRelease!: (value: unknown) => void;
    transport.releaseStatus.mockImplementation(() => new Promise(resolve => { finishRelease = resolve; }));
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'tools.check' ? new Promise(resolve => { finishTools = resolve; }) : true);
    render(<AppProvider><App /></AppProvider>);
    expect(await screen.findByRole('listbox')).toBeTruthy();
    expect(transport.command).toHaveBeenCalledWith('snapshot', { includeTools: false });
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.check'));
    await act(async () => finishTools([{ name: 'qjs', available: true, version: '0.17.0' }]));
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Tools & updates' }));
    expect(screen.getByText('qjs')).toBeTruthy();
    act(() => transport.listener?.({ kind: 'snapshot.required', data: null }));
    await waitFor(() => expect(transport.command.mock.calls.filter(([name]) => name === 'snapshot')).toHaveLength(2));
    expect(screen.getByText('qjs')).toBeTruthy();
    expect(transport.command.mock.calls.filter(([name]) => name === 'tools.check')).toHaveLength(1);
    await act(async () => finishRelease({ currentVersion: '0.2.0', latestVersion: '0.2.0', updateAvailable: false, url: 'https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.2.0' }));
  });
  it('keeps the task list available when the background runtime check fails', async () => {
    const snapshot = makeSnapshot(); delete snapshot.tools;
    transport.command.mockImplementation(async command => { if (command === 'snapshot') return snapshot; if (command === 'tools.check') throw new Error('fixture tools unavailable'); return true; });
    render(<AppProvider><App /></AppProvider>);
    expect(await screen.findByRole('listbox')).toBeTruthy();
    expect((await screen.findAllByText('Runtime components could not be checked. Retry under Tools & updates.')).length).toBeGreaterThan(0);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(transport.command.mock.calls.filter(([name]) => name === 'tools.check')).toHaveLength(1);
  });
  it('shows the packaged version while the snapshot is loading and then uses the runtime version', async () => {
    let finish!: (value: unknown) => void;
    transport.command.mockImplementation(async command => command === 'snapshot' ? new Promise(resolve => { finish = resolve; }) : true);
    render(<AppProvider><App /></AppProvider>);
    expect(await screen.findByRole('button', { name: `v${buildVersion}` })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'v0.3.0' })).toBeNull();
    const snapshot = makeSnapshot(); snapshot.version = '1.2.3';
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('snapshot', { includeTools: false }));
    await act(async () => finish(snapshot));
    expect(await screen.findByRole('button', { name: 'v1.2.3' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: `v${buildVersion}` })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'v1.2.3' }));
    expect(screen.getByText('Current version 1.2.3 · Latest release 0.3.0')).toBeTruthy();
  });
  it('keeps the packaged version if the initial snapshot fails', async () => {
    transport.command.mockImplementation(async command => { if (command === 'snapshot') throw new Error('fixture snapshot unavailable'); return true; });
    render(<AppProvider><App /></AppProvider>);
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByRole('button', { name: `v${buildVersion}` })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'v0.3.0' })).toBeNull();
  });
  it('shows actual completed quality and bytes with separate source and result details', async () => {
    const snapshot = makeSnapshot();
    Object.assign(snapshot.tasks[0], { status: 'completed', stage: 'completed', stepIndex: 6, stepCount: 6, size: 10485760, sourceInfo: { width: 3840, height: 2160, fps: 59.94, videoCodec: 'vp9', audioCodec: 'opus', size: 20971520, duration: 60, container: 'webm' }, mediaInfo: { width: 1920, height: 1080, fps: 29.97, videoCodec: 'h264', audioCodec: 'aac', size: 10485760, duration: 60, container: 'mp4' } });
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    const row = within(screen.getByRole('option', { name: /Alpha/ }));
    expect(row.getByText('Completed · 1080p')).toBeTruthy();
    expect(row.getByText('10.0 MiB')).toBeTruthy();
    expect(row.queryByText(/4K/)).toBeNull();
    const source = within(screen.getByRole('region', { name: 'Source video information' }));
    const result = within(screen.getByRole('region', { name: 'Result file information' }));
    expect(source.getByText('4K · 3840 × 2160')).toBeTruthy();
    expect(source.getByText('59.94 fps')).toBeTruthy(); expect(source.getByText('vp9 / opus')).toBeTruthy();
    expect(result.getByText('1080p · 1920 × 1080')).toBeTruthy();
    expect(result.getByText('29.97 fps')).toBeTruthy(); expect(result.getByText('h264 / aac')).toBeTruthy();
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.info')).toBe(false);
  });
  it('uses the completed result frame rate in both task and library badges', async () => {
    const snapshot = makeSnapshot(); const mediaInfo = { width: 1920, height: 1080, fps: 59.94, size: 1048576 };
    Object.assign(snapshot.tasks[0], { status: 'completed', stage: 'completed', options: { quality: 'best' }, mediaInfo, sourceInfo: { width: 3840, height: 2160, fps: 120 } });
    snapshot.media = [{ id: 'media-a', taskId: 'a', title: 'Alpha', filename: 'alpha.mp4', size: 1048576, duration: 60, mediaInfo }];
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    const row = within(screen.getByRole('option', { name: /Alpha/ }));
    expect(row.getByText('Completed · 1080p60')).toBeTruthy(); expect(row.queryByText(/4K120/)).toBeNull();
    const result = within(screen.getByRole('region', { name: 'Result file information' }));
    expect(result.getByText('1080p · 1920 × 1080')).toBeTruthy(); expect(result.getByText('59.94 fps')).toBeTruthy();
    fireEvent.click(screen.getAllByRole('button', { name: 'Completed' })[0]);
    expect(await screen.findByText('Completed · 1080p60')).toBeTruthy();
  });
  it('reads legacy completed file information once and updates the row without using its quality preset', async () => {
    const snapshot = makeSnapshot();
    Object.assign(snapshot.tasks[0], { status: 'completed', stage: 'completed', size: 1048576, options: { quality: 'best' } });
    let finish!: (value: unknown) => void;
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'tasks.info' ? new Promise(resolve => { finish = resolve; }) : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    const row = within(screen.getByRole('option', { name: /Alpha/ }));
    expect(row.getByText('1.0 MiB')).toBeTruthy(); expect(row.queryByText(/4K/)).toBeNull();
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.info', { id: 'a' }));
    expect(screen.getByText('Reading local file information…')).toBeTruthy();
    await act(async () => finish({ ...snapshot.tasks[0], mediaInfo: { width: 720, height: 1280, fps: 30, size: 1048576 } }));
    expect(row.getByText('Completed · 720p')).toBeTruthy();
    expect(screen.queryByText('Reading local file information…')).toBeNull();
    expect(screen.getByText('No information available')).toBeTruthy();
    expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.info')).toHaveLength(1);
  });
  it('keeps failed local metadata reads unobtrusive and offers retry', async () => {
    const snapshot = makeSnapshot(); Object.assign(snapshot.tasks[0], { status: 'completed', stage: 'completed' });
    transport.command.mockImplementation(async command => { if (command === 'snapshot') return snapshot; if (command === 'tasks.info') throw new Error('fixture probe failed'); return true; });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    expect(await screen.findByText(/Result information could not be read/)).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.info')).toHaveLength(2));
  });
  it('shows the actual resolution in the completed media library', async () => {
    const snapshot = makeSnapshot();
    snapshot.media = [{ id: 'media-a', taskId: 'a', title: 'Alpha', filename: 'alpha.mp4', size: 10485760, duration: 60, mediaInfo: { width: 2160, height: 3840, fps: 30, videoCodec: 'hevc', audioCodec: 'aac', container: 'mp4' } }];
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getAllByRole('button', { name: 'Completed' })[0]);
    expect(await screen.findByText('Completed · 4K')).toBeTruthy();
    expect(screen.getByText('4K · 2160 × 3840')).toBeTruthy();
    expect(screen.getByText('hevc / aac')).toBeTruthy();
  });
  it('fills legacy media information when opening completed media without selecting its task', async () => {
    const snapshot = makeSnapshot();
    snapshot.media = [{ id: 'retained-media', taskId: 'retained-task', title: 'Retained video', filename: 'video.mp4', size: 10485760, duration: 60 }];
    let finish!: (value: unknown) => void;
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'tasks.info' ? new Promise(resolve => { finish = resolve; }) : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getAllByRole('button', { name: 'Completed' })[0]);
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.info', { id: 'retained-task' }));
    expect(screen.getByText('Reading local file information…')).toBeTruthy();
    await act(async () => finish({ id: 'retained-task', status: 'retained', mediaInfo: { width: 1920, height: 1080, fps: 60, videoCodec: 'h264', audioCodec: 'aac' } }));
    expect(await screen.findByText('Completed · 1080p60')).toBeTruthy();
    expect(screen.getByText('1080p · 1920 × 1080')).toBeTruthy();
    expect(screen.queryByText('Reading local file information…')).toBeNull();
    expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.info')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Downloads' }));
    expect(screen.queryByRole('option', { name: /Retained video/ })).toBeNull();
  });
  it('offers retry and a concise error tooltip for legacy media information', async () => {
    const snapshot = makeSnapshot();
    snapshot.media = [{ id: 'old-media', taskId: 'old-task', title: 'Old video', filename: 'old.mp4', size: 10485760, duration: 60 }];
    transport.command.mockImplementation(async command => { if (command === 'snapshot') return snapshot; if (command === 'tasks.info') throw new Error('fixture local file is missing'); return true; });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getAllByRole('button', { name: 'Completed' })[0]);
    const error = await screen.findByText(/Result information could not be read/);
    expect(error.getAttribute('title')).toContain('result file is missing');
    expect(error.getAttribute('title')).not.toContain('fixture');
    expect(screen.queryByRole('alert')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.info')).toHaveLength(2));
  });
  it('shows a task cover during download before a completed media record exists', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    const detail = screen.getByRole('complementary', { name: 'Task details' });
    expect(detail.querySelector('.thumbnail img')).toBeNull();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'downloading', hasThumbnail: true } }));
    await waitFor(() => expect(detail.querySelector('.thumbnail img')?.getAttribute('src')).toBe('/api/files/a?kind=taskThumbnail'));
  });
  it('prefers task covers in compact rows, falls back to media covers, and keeps an image failure inside its 56px row', async () => {
    const snapshot = makeSnapshot(); snapshot.tasks[0].hasThumbnail = true;
    snapshot.media = [
      { id: 'media-a', taskId: 'a', title: 'Alpha', filename: 'a.mp4', size: 1024, duration: 60 },
      { id: 'media-b', taskId: 'b', title: 'Beta', filename: 'b.mp4', size: 1024, duration: 60 },
    ];
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    const alpha = screen.getByRole('option', { name: /Alpha/ }); const beta = screen.getByRole('option', { name: /Beta/ });
    expect(alpha.querySelector('.thumbnail img')?.getAttribute('src')).toBe('/api/files/a?kind=taskThumbnail');
    expect(beta.querySelector('.thumbnail img')?.getAttribute('src')).toBe('/api/files/media-b?kind=thumbnail');
    expect(alpha.style.height).toBe('56px'); expect(beta.style.height).toBe('56px');
    fireEvent.error(alpha.querySelector('.thumbnail img')!);
    expect(alpha.querySelector('.thumbnail img')).toBeNull(); expect(alpha.querySelector('.thumbnail svg')).toBeTruthy(); expect(alpha.style.height).toBe('56px');
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', hasThumbnail: false } }));
    await waitFor(() => expect(alpha.querySelector('.thumbnail img')?.getAttribute('src')).toBe('/api/files/media-a?kind=thumbnail'));
  });
  it('uses the same context actions on completed media and supports keyboard dismissal and focus', async () => {
    const snapshot = makeSnapshot(); const mediaInfo = { width: 1920, height: 1080, fps: 60 };
    for (const task of snapshot.tasks) Object.assign(task, { status: 'completed', stage: 'completed', mediaInfo });
    snapshot.media = snapshot.tasks.map(task => ({ id: `media-${task.id}`, taskId: task.id, title: task.title, filename: `${task.id}.mp4`, size: 1024, duration: 60, mediaInfo }));
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'tasks.logs' ? { text: 'Raw log for Alpha', start: 0, end: 17, total: 17, hasMore: false, raw: true, legacy: false } : command === 'tasks.convertPreview' ? { strategy: 'remux', encoder: 'copy', audioEncoder: 'copy', reason: 'compatible codecs' } : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getAllByRole('button', { name: 'Completed' })[0]);
    const list = await screen.findByRole('listbox', { name: 'Completed media list' });
    const alpha = within(list).getByRole('option', { name: /Alpha/ });
    const menu = () => { expect(fireEvent.contextMenu(alpha)).toBe(false); return within(screen.getByRole('menu', { name: 'Task actions' })); };
    let actions = menu();
    expect(alpha.getAttribute('aria-selected')).toBe('true');
    expect(actions.getByRole('menuitem', { name: 'Convert format' })).toBeTruthy(); expect(actions.getByRole('menuitem', { name: 'Delete task' })).toBeTruthy();
    fireEvent.click(actions.getByRole('menuitem', { name: 'Copy video title' }));
    await waitFor(() => expect(transport.copyTask).toHaveBeenCalledWith(expect.objectContaining({ id: 'a' }), 'title'));
    actions = menu(); fireEvent.click(actions.getByRole('menuitem', { name: 'Copy source' }));
    await waitFor(() => expect(transport.copyTask).toHaveBeenCalledWith(expect.objectContaining({ id: 'a' }), 'source'));
    actions = menu(); fireEvent.click(actions.getByRole('menuitem', { name: 'Download result file' }));
    await waitFor(() => expect(transport.result).toHaveBeenCalledWith('media-a', 'download'));
    actions = menu(); fireEvent.click(actions.getByRole('menuitem', { name: 'View logs' }));
    let dialog = within(await screen.findByRole('dialog', { name: 'Task logs' }));
    expect(await dialog.findByText('Raw log for Alpha')).toBeTruthy(); expect(transport.command).toHaveBeenCalledWith('tasks.logs', { id: 'a', limit: 65536 });
    fireEvent.click(dialog.getByRole('button', { name: 'Close' }));
    actions = menu(); fireEvent.click(actions.getByRole('menuitem', { name: 'Convert format' }));
    dialog = within(await screen.findByRole('dialog', { name: 'Convert video format' }));
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.convertPreview', expect.objectContaining({ id: 'a' })));
    fireEvent.click(dialog.getByRole('button', { name: 'Close dialog' }));
    actions = menu(); fireEvent.click(actions.getByRole('menuitem', { name: 'Delete task' }));
    dialog = within(await screen.findByRole('dialog', { name: 'Delete task' }));
    expect(dialog.getByText(/Delete “Alpha”/)).toBeTruthy(); fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.delete')).toBe(false);
    list.focus(); fireEvent.keyDown(list, { key: 'F10', shiftKey: true });
    const keyboardMenu = screen.getByRole('menu', { name: 'Task actions' });
    fireEvent.keyDown(keyboardMenu, { key: 'End' }); expect(document.activeElement).toBe(within(keyboardMenu).getByRole('menuitem', { name: 'Delete task' }));
    fireEvent.keyDown(keyboardMenu, { key: 'Escape' }); expect(screen.queryByRole('menu')).toBeNull(); expect(document.activeElement).toBe(list);
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.get')).toBe(false);
  });
  it('loads a retained media task on demand for its menu without restoring its deleted task row', async () => {
    const snapshot = makeSnapshot(); const mediaInfo = { width: 1920, height: 1080, fps: 60 };
    snapshot.media = [{ id: 'retained-media', taskId: 'retained-task', title: 'Retained video', filename: 'retained.mp4', size: 1024, duration: 60, mediaInfo }];
    let finish!: (value: unknown) => void;
    const retained = { ...snapshot.tasks[0], id: 'retained-task', title: 'Retained video', status: 'retained', stage: 'completed', mediaInfo };
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'tasks.get' ? new Promise(resolve => { finish = resolve; }) : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getAllByRole('button', { name: 'Completed' })[0]);
    const list = await screen.findByRole('listbox', { name: 'Completed media list' });
    fireEvent.contextMenu(within(list).getByRole('option', { name: /Retained video/ }));
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.get', { id: 'retained-task' }));
    expect(screen.queryByRole('menu')).toBeNull();
    await act(async () => finish(retained));
    const menu = within(await screen.findByRole('menu', { name: 'Task actions' }));
    expect(menu.getByRole('menuitem', { name: 'Convert format' })).toBeTruthy(); expect(menu.getByRole('menuitem', { name: 'View logs' })).toBeTruthy();
    fireEvent.click(menu.getByRole('menuitem', { name: 'Copy source' }));
    await waitFor(() => expect(transport.copyTask).toHaveBeenCalledWith(expect.objectContaining({ id: 'retained-task' }), 'source'));
    fireEvent.click(screen.getByRole('button', { name: 'Downloads' }));
    expect(screen.getByRole('listbox', { name: 'Download task list' })).toBeTruthy();
    expect(screen.queryByRole('option', { name: /Retained video/ })).toBeNull(); expect(within(screen.getByRole('listbox', { name: 'Download task list' })).getAllByRole('option')).toHaveLength(2);
    expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.get')).toHaveLength(1);
  });
  it('opens task actions with right click and acts on that task rather than the previous selection', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    const row = screen.getByRole('option', { name: /Beta/ });
    const nativeMenu = fireEvent.contextMenu(row, { clientX: 800, clientY: 500 });
    expect(nativeMenu).toBe(false);
    const menu = screen.getByRole('menu', { name: 'Task actions' });
    expect(within(menu).getByRole('menuitem', { name: 'Pause' })).toBeTruthy();
    expect(row.getAttribute('aria-selected')).toBe('true');
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Pause' }));
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.pause', { id: 'b' }));
    expect(screen.queryByRole('menu')).toBeNull();
  });
  it('clears a failed retry notice and shows the accepted download queue without waiting for an event', async () => {
    const snapshot = makeSnapshot(); snapshot.tasks[0].status = 'failed';
    const queued = { ...snapshot.tasks[0], status: 'queued', stage: 'queued', error: null };
    let accept!: (value: unknown) => void; let attempts = 0;
    transport.command.mockImplementation(async command => {
      if (command === 'snapshot') return snapshot;
      if (command === 'tasks.retry') {
        if (++attempts === 1) throw new Error('Unknown retry failure at C:\\Users\\fixture\\token-secret');
        return new Promise(resolve => { accept = resolve; });
      }
      return true;
    });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    const detail = within(screen.getByRole('complementary', { name: 'Task details' }));
    fireEvent.click(detail.getByRole('button', { name: 'Retry download' }));
    expect((await screen.findByRole('alert')).textContent).toBe('The operation could not be completed. View the logs for details.');
    expect(screen.queryByText(/token-secret/)).toBeNull();
    fireEvent.click(detail.getByRole('button', { name: 'Retry download' }));
    await waitFor(() => expect(accept).toBeTypeOf('function'));
    expect(screen.queryByRole('alert')).toBeNull();
    await act(async () => accept(queued));
    expect(detail.getByText('Download queued', { selector: 'strong' })).toBeTruthy();
    expect(detail.queryByRole('button', { name: 'Retry download' })).toBeNull();
    expect((await screen.findByRole('status')).textContent).toBe('Request accepted. The task will follow the queue.');
    expect(transport.command).toHaveBeenCalledWith('tasks.retry', { id: 'a' });
  });
  it('keeps a newer running event when the retry response arrives in the same render batch', async () => {
    const snapshot = makeSnapshot(); snapshot.tasks[0].status = 'failed';
    const queued = { ...snapshot.tasks[0], status: 'queued', stage: 'queued', error: null };
    let accept!: (value: unknown) => void;
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'tasks.retry' ? new Promise(resolve => { accept = resolve; }) : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    const detail = within(screen.getByRole('complementary', { name: 'Task details' }));
    fireEvent.click(detail.getByRole('button', { name: 'Retry download' }));
    await waitFor(() => expect(accept).toBeTypeOf('function'));
    await act(async () => {
      transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'downloading', stage: 'download', error: null } });
      accept(queued);
    });
    expect(detail.getByText('Downloading', { selector: '.badge' })).toBeTruthy();
    expect(detail.queryByText('Download queued')).toBeNull();
  });
  it('opens logs for the right-clicked task and retains a details entry after closing', async () => {
    transport.command.mockImplementation(async command => command === 'snapshot' ? makeSnapshot() : command === 'tasks.logs' ? { text: 'Fixture warning for Beta', start: 0, end: 24, total: 24, hasMore: false, raw: true, legacy: false } : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.contextMenu(screen.getByRole('option', { name: /Beta/ }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'View logs' }));
    const dialog = within(await screen.findByRole('dialog', { name: 'Task logs' }));
    expect(await dialog.findByText(/Fixture warning for Beta/)).toBeTruthy();
    expect(transport.command).toHaveBeenCalledWith('tasks.logs', { id: 'b', limit: 65536 });
    expect(screen.queryByRole('menu')).toBeNull();
    fireEvent.click(dialog.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(within(screen.getByRole('complementary', { name: 'Task details' })).getByRole('button', { name: 'View logs' }));
    expect(await screen.findByRole('dialog', { name: 'Task logs' })).toBeTruthy();
  });
  it('supports keyboard context menus, status-specific actions and dismissing stale virtual rows', async () => {
    render(<AppProvider><App /></AppProvider>); const list = await screen.findByRole('listbox');
    fireEvent.click(within(list).getByRole('option', { name: /Alpha/ }));
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'paused' } }));
    list.focus(); fireEvent.keyDown(list, { key: 'F10', shiftKey: true });
    let menu = screen.getByRole('menu', { name: 'Task actions' });
    expect(within(menu).getByRole('menuitem', { name: 'Resume' })).toBeTruthy();
    fireEvent.keyDown(menu, { key: 'End' });
    expect(document.activeElement).toBe(within(menu).getByRole('menuitem', { name: 'Delete task' }));
    fireEvent.keyDown(menu, { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull(); expect(document.activeElement).toBe(list);
    fireEvent.contextMenu(screen.getByRole('option', { name: /Alpha/ }));
    fireEvent.scroll(list, { target: { scrollTop: 56 } });
    expect(screen.queryByRole('menu')).toBeNull();
    fireEvent.contextMenu(screen.getByRole('option', { name: /Alpha/ }));
    menu = screen.getByRole('menu');
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Delete task' }));
    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.delete')).toBe(false);
  });
  it('closes the task menu when a filter removes its task', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.contextMenu(screen.getByRole('option', { name: /Alpha/ }));
    expect(screen.getByRole('menu')).toBeTruthy();
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search tasks' }), { target: { value: 'Beta' } });
    expect(screen.queryByRole('menu')).toBeNull();
  });
  it('persists the delete-files preference even when cancelled and restores it after reloading', async () => {
    const snapshot = makeSnapshot();
    transport.command.mockImplementation(async (command, args) => {
      if (command === 'snapshot') return snapshot;
      if (command === 'settings.preferences') { snapshot.settings.uiPreferences = args.preferences; return snapshot.settings; }
      return true;
    });
    const openDelete = async () => {
      await screen.findByRole('listbox');
      fireEvent.contextMenu(screen.getByRole('option', { name: /Alpha/ }));
      fireEvent.click(screen.getByRole('menuitem', { name: 'Delete task' }));
      return within(screen.getByRole('dialog'));
    };
    const first = render(<AppProvider><App /></AppProvider>);
    let dialog = await openDelete();
    const checkbox = dialog.getByRole('checkbox') as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
    fireEvent.click(checkbox);
    await waitFor(() => expect(checkbox.disabled).toBe(false));
    expect(transport.command).toHaveBeenCalledWith('settings.preferences', { preferences: { deleteMediaOnTaskDelete: true } });
    fireEvent.click(dialog.getByRole('button', { name: 'Cancel' }));
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.delete' || name === 'settings.save')).toBe(false);
    dialog = await openDelete();
    expect((dialog.getByRole('checkbox') as HTMLInputElement).checked).toBe(true);
    first.unmount();
    render(<AppProvider><App /></AppProvider>);
    dialog = await openDelete();
    expect((dialog.getByRole('checkbox') as HTMLInputElement).checked).toBe(true);
    fireEvent.click(dialog.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.delete', { id: 'a', deleteFiles: true }));
  });
  it('restores the previous delete-files choice if saving the preference fails', async () => {
    transport.command.mockImplementation(async command => {
      if (command === 'snapshot') return makeSnapshot();
      if (command === 'settings.preferences') throw new Error('Unable to save preference');
      return true;
    });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.contextMenu(screen.getByRole('option', { name: /Alpha/ }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete task' }));
    const dialog = within(screen.getByRole('dialog')); const checkbox = dialog.getByRole('checkbox') as HTMLInputElement;
    fireEvent.click(checkbox);
    expect((await dialog.findByRole('alert')).textContent).toContain('previous setting was kept');
    await waitFor(() => expect(checkbox.checked).toBe(false));
    fireEvent.click(dialog.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.delete', { id: 'a', deleteFiles: false }));
  });
  it.each(['https://www.youtube.com/watch?v=abcdefghijk&list=PL123', 'https://www.douyin.com/video/7695747024182590437', 'https://www.douyin.com/note/7695747024182590437'])('immediately queues an explicit single media URL with automatic quality: %s', async url => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: url } });
    fireEvent.click(screen.getByRole('button', { name: 'Add downloads' }));
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.add', { urls: [url], options: expect.objectContaining({ quality: 'best', cookieId: 'auto', fragmentConcurrent: 4, collectionMode: 'single' }), report: true }));
    expect(screen.queryByRole('dialog')).toBeNull(); expect(transport.command.mock.calls.some(([name]) => name.startsWith('probe.'))).toBe(false);
  });
  it.each(['downloading', 'paused'] as const)('selects the existing %s task instead of adding another task', async status => {
    const snapshot = makeSnapshot(); const url = 'https://youtu.be/abcdefghijk';
    snapshot.tasks[1].status = status; snapshot.tasks[1].url = url;
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'tasks.add' ? { ids: ['b'], results: [{ id: 'b', status, action: 'existing' }] } : true);
    render(<AppProvider><App /></AppProvider>); const list = await screen.findByRole('listbox');
    expect(within(list).queryByRole('option', { selected: true })).toBeNull();
    fireEvent.click(within(list).getByRole('option', { name: /Alpha/ }));
    expect(within(list).getByRole('option', { selected: true }).textContent).toContain('Alpha');
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: url } });
    fireEvent.click(screen.getByRole('button', { name: 'Add downloads' }));
    const message = await screen.findByRole('status');
    expect(message.textContent).toBe(status === 'paused' ? 'This task is paused. Resume it from task details.' : 'This task already exists: Downloading. No duplicate was added.');
    expect(within(list).getAllByRole('option')).toHaveLength(2);
    expect(within(list).getByRole('option', { selected: true }).textContent).toContain('Beta');
    const detail = within(screen.getByRole('complementary', { name: 'Task details' }));
    expect(detail.getByRole('heading', { name: 'Beta' })).toBeTruthy();
    expect((screen.getByRole('textbox', { name: 'Download links' }) as HTMLTextAreaElement).value).toBe('');
    expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.add')).toHaveLength(1);
    expect(transport.command).toHaveBeenCalledWith('tasks.add', { urls: [url], options: expect.any(Object), report: true });
    if (status === 'paused') {
      fireEvent.click(detail.getByRole('button', { name: 'Resume' }));
      await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.resume', { id: 'b' }));
    } else expect(detail.getByRole('button', { name: 'Pause' })).toBeTruthy();
  });
  it('retains paused positions and replaces stale stages with terminal status in both the row and details', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'processing', stage: 'muxing', stepIndex: 2, stepCount: 4, stageProgress: 35 } }));
    const row = within(screen.getByRole('option', { name: /Alpha/ }));
    const detail = within(screen.getByRole('complementary', { name: 'Task details' }));
    expect(row.getByText('2/4 Merging video and audio')).toBeTruthy();
    expect(detail.getByText('2/4 Merging video and audio')).toBeTruthy();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', stage: 'verifying_output', stepIndex: 3, stageProgress: 80 } }));
    expect(row.getByText('3/4 Validating result file')).toBeTruthy();
    expect(detail.getByText('3/4 Validating result file')).toBeTruthy();
    expect(row.queryByText('2/4 Merging video and audio')).toBeNull();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'paused' } }));
    expect(row.getByText('3/4 Validating result file')).toBeTruthy();
    expect(detail.getByText('3/4 Validating result file')).toBeTruthy();
    for (const [status, stage, label] of [['failed', 'probing', 'Download failed'], ['processing_failed', 'muxing', 'Processing failed'], ['cancelled', 'downloading_video_1', 'Cancelled']]) {
      act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status, stage } }));
      expect(row.queryByText(/3\/4/)).toBeNull();
      expect(detail.queryByText(/3\/4/)).toBeNull();
      expect(row.getAllByText(label)).toHaveLength(2);
      expect(detail.getByText(label, { selector: 'strong' })).toBeTruthy();
    }
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'completed', stage: 'completed', stepIndex: 4 } }));
    expect(row.queryByText(/4\/4/)).toBeNull();
    expect(detail.queryByText(/4\/4/)).toBeNull();
    expect(row.getAllByText('Completed')).toHaveLength(2);
    expect(detail.getByText('Completed', { selector: 'strong' })).toBeTruthy();
  });
  it('returns collection duplicate reports to the task list and selects the existing paused task', async () => {
    const snapshot = makeSnapshot(); snapshot.tasks[1].status = 'paused';
    const entries = [snapshot.tasks[1], snapshot.tasks[0]].map(task => ({ url: task.url, title: task.title }));
    transport.command.mockImplementation(async (command, args) => {
      if (command === 'snapshot') return snapshot;
      if (command === 'probe.collection') return { kind: 'collection', entries };
      if (command === 'tasks.add') {
        const task = snapshot.tasks.find(task => task.url === args.urls[0])!;
        return { ids: [task.id], results: [{ id: task.id, status: task.status, action: 'existing' }] };
      }
      return true;
    });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: 'https://example.test/collection' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add downloads' }));
    const dialog = within(await screen.findByRole('dialog'));
    const confirm = await dialog.findByRole('button', { name: 'Download selected items' });
    await waitFor(() => expect((confirm as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(confirm);
    expect((await screen.findByRole('status')).textContent).toBe('This task is paused. Resume it from task details.');
    expect(screen.queryByRole('dialog')).toBeNull();
    const list = within(screen.getByRole('listbox'));
    expect(list.getAllByRole('option')).toHaveLength(2);
    expect(list.getByRole('option', { selected: true }).textContent).toContain('Beta');
    expect(within(screen.getByRole('complementary', { name: 'Task details' })).getByRole('button', { name: 'Resume' })).toBeTruthy();
    expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.add')).toHaveLength(2);
    for (const entry of entries) expect(transport.command).toHaveBeenCalledWith('tasks.add', { urls: [entry.url], options: expect.any(Object), report: true });
  });
  it('previews the selected local conversion container and method before submitting structured options', async () => {
    const snapshot = makeSnapshot(); snapshot.tasks[0].status = 'completed';
    snapshot.media = [{ id: 'media-a', taskId: 'a', title: 'Alpha', filename: 'alpha.mp4', size: 1024, duration: 60 }];
    let finishPreview!: (value: unknown) => void;
    transport.command.mockImplementation(async (command, args) => {
      if (command === 'snapshot') return snapshot;
      if (command === 'tasks.convertPreview') {
        if (args.options.container === 'mkv' && args.strategy === 'remux') return new Promise(resolve => { finishPreview = resolve; });
        return { strategy: 'remux', encoder: 'copy', audioEncoder: 'copy', reason: 'compatible codecs' };
      }
      return true;
    });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    const detail = within(screen.getByRole('complementary', { name: 'Task details' }));
    fireEvent.click(detail.getByText('More actions'));
    fireEvent.click(detail.getByRole('button', { name: 'Convert format' }));
    const dialog = within(await screen.findByRole('dialog'));
    await dialog.findByText('Will remux and preserve the source codecs.');
    fireEvent.change(dialog.getByRole('combobox', { name: 'Target format' }), { target: { value: 'mkv' } });
    fireEvent.click(dialog.getByText('Advanced options'));
    fireEvent.change(dialog.getByRole('combobox', { name: 'Conversion method' }), { target: { value: 'remux' } });
    const start = dialog.getByRole('button', { name: 'Start conversion' }) as HTMLButtonElement;
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.convertPreview', { id: 'a', options: { container: 'mkv' }, strategy: 'remux' }));
    expect(start.disabled).toBe(true);
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.convert')).toBe(false);
    await act(async () => finishPreview({ strategy: 'remux', encoder: 'copy', audioEncoder: 'copy', reason: 'compatible codecs' }));
    expect(dialog.getByText('Will remux and preserve the source codecs.')).toBeTruthy();
    expect(start.disabled).toBe(false);
    fireEvent.click(start);
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.convert', { id: 'a', options: { container: 'mkv' }, strategy: 'remux' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
  it('opens version details from an up-to-date sidebar link and lets the user recheck', async () => {
    const snapshot = makeSnapshot(); snapshot.version = '0.4.1';
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : true);
    transport.releaseStatus.mockResolvedValue({ currentVersion: '0.4.1', latestVersion: '0.4.1', updateAvailable: false, url: 'https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.1' });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    await waitFor(() => expect(transport.releaseStatus).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole('button', { name: 'v0.4.1' }));
    expect(await screen.findByText('You have the latest stable release.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Open new release page' })).toBeNull();
    expect(transport.openRelease).not.toHaveBeenCalled();
    const project = screen.getByRole('link', { name: 'github.com/jwwsjlm/huifa-media-downloader' });
    expect(project.getAttribute('href')).toBe('https://github.com/jwwsjlm/huifa-media-downloader');
    fireEvent.click(project);
    await waitFor(() => expect(transport.openProject).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
    await waitFor(() => expect(transport.releaseStatus).toHaveBeenCalledTimes(2));
  });
  it('opens the detected release directly from the sidebar update badge', async () => {
    const snapshot = makeSnapshot(); snapshot.version = '0.4.0';
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : true);
    const url = 'https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.1';
    transport.releaseStatus.mockResolvedValue({ currentVersion: '0.4.0', latestVersion: '0.4.1', updateAvailable: true, url });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    const version = await screen.findByRole('button', { name: /v0\.4\.0.*Update/ });
    expect(version.title).toBe('Open new release page');
    fireEvent.click(version);
    await waitFor(() => expect(transport.openRelease).toHaveBeenCalledWith(url));
    expect(transport.releaseStatus).toHaveBeenCalledOnce();
    expect(screen.getByRole('listbox')).toBeTruthy();
  });
  it('keeps the app usable after update checks fail and allows a successful manual retry', async () => {
    const snapshot = makeSnapshot(); snapshot.version = '0.4.0';
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : true);
    transport.releaseStatus.mockRejectedValue(new Error('fixture GitHub unavailable'));
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    expect(screen.queryByRole('alert')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'v0.4.0' }));
    expect(await screen.findByText('Cannot check for updates right now. Try again later.')).toBeTruthy();
    expect(screen.getByText('Error: fixture GitHub unavailable')).toBeTruthy();
    const recheck = screen.getByRole('button', { name: 'Check for updates' }) as HTMLButtonElement;
    fireEvent.click(recheck);
    await waitFor(() => expect(transport.releaseStatus).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(recheck.disabled).toBe(false));
    expect(screen.queryByRole('alert')).toBeNull();
    const url = 'https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.1';
    transport.releaseStatus.mockResolvedValue({ currentVersion: '0.4.0', latestVersion: '0.4.1', updateAvailable: true, url });
    fireEvent.click(recheck);
    const release = await screen.findByRole('button', { name: 'Open new release page' });
    expect(screen.queryByText('Cannot check for updates right now. Try again later.')).toBeNull();
    fireEvent.click(release);
    await waitFor(() => expect(transport.openRelease).toHaveBeenCalledWith(url));
    expect(transport.releaseStatus).toHaveBeenCalledTimes(3);
  });
  it.each([
    ['GitHub release request failed: HTTP 403 rate limit exceeded', 'GitHub has temporarily limited update checks. Retry after the limit resets.'],
    ['GitHub release request failed: HTTP 429 Too Many Requests', 'GitHub has temporarily limited update checks. Retry after the limit resets.'],
    ['GitHub release request timed out', 'The update request timed out. Check GitHub connectivity and retry.'],
  ])('shows update-specific advice and the concrete cause for %s', async (error, hint) => {
    transport.releaseStatus.mockRejectedValue(error);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('button', { name: 'v0.2.0' }));
    expect(await screen.findByText(hint)).toBeTruthy();
    expect(screen.getByText(error)).toBeTruthy();
    expect(screen.queryByText(/Check permissions, cookies/)).toBeNull();
    expect(screen.queryByText(/Connection to the source site failed/)).toBeNull();
  });
  it('does not claim to be current after a newer update check fails', async () => {
    transport.releaseStatus.mockResolvedValue({ currentVersion: '0.4.1', latestVersion: '0.4.1', updateAvailable: false, url: 'https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.1' });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('button', { name: 'v0.2.0' }));
    expect(await screen.findByText('You have the latest stable release.')).toBeTruthy();
    transport.releaseStatus.mockRejectedValue('GitHub release request failed: HTTP 403 rate limit exceeded');
    fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
    expect(await screen.findByText('GitHub has temporarily limited update checks. Retry after the limit resets.')).toBeTruthy();
    expect(screen.queryByText('You have the latest stable release.')).toBeNull();
  });
  it.each(['video','collection'])('uses backend classification for a one-item %s', async kind => {
    const url = 'https://example.test/unknown';
    transport.command.mockImplementation(async name => name === 'snapshot' ? makeSnapshot() : name === 'probe.collection' ? { kind, entries: [{ url, title: 'One item' }] } : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: url } }); fireEvent.click(screen.getByRole('button', { name: 'Add downloads' }));
    if (kind === 'collection') { expect(await screen.findByRole('dialog')).toBeTruthy(); expect(transport.command.mock.calls.some(([name]) => name === 'tasks.add')).toBe(false); }
    else { await waitFor(() => expect(transport.command.mock.calls.some(([name]) => name === 'tasks.add')).toBe(true)); expect(screen.queryByRole('dialog')).toBeNull(); }
  });
  it('opens only the format picker for a manually configured single video', async () => {
    const snapshot = makeSnapshot(); snapshot.settings.downloadOptions = { contentMode: 'manual', quality: 'best' };
    transport.command.mockImplementation(async name => name === 'snapshot' ? snapshot : name === 'probe.video' ? { title: 'Manual video', formats: [{ id:'137', formatId:'137', vcodec:'h264', acodec:'none' }] } : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: 'https://youtu.be/abcdefghijk' } }); fireEvent.click(screen.getByRole('button', { name: 'Add downloads' }));
    expect(await screen.findByRole('heading', { name: 'Choose download format' })).toBeTruthy(); expect(screen.queryByRole('heading', { name: 'Choose download items' })).toBeNull();
    expect(transport.command.mock.calls.some(([name]) => name === 'probe.collection')).toBe(false);
  });
  it('queues clipboard links once and preserves remaining links after a partial failure', async () => {
    let release!: (value: string) => void; const readText = vi.fn(() => new Promise<string>(resolve => { release = resolve; }));
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { readText } });
    let calls = 0; transport.command.mockImplementation(async name => { if (name === 'snapshot') return makeSnapshot(); if (name === 'tasks.add') { if (++calls === 2) throw new Error('Queue fixture failed'); return { ids: ['new'], results: [{ id: 'new', status: 'queued', action: 'added' }] }; } return true; });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    const button = screen.getByRole('button', { name: 'Download clipboard' }); fireEvent.click(button); fireEvent.click(button);
    expect(readText).toHaveBeenCalledTimes(1);
    await act(async () => release('https://youtu.be/abcdefghijk\nhttps://youtu.be/abcdefghijk\nhttps://youtu.be/zyxwvutsrqp'));
    expect(await screen.findByRole('heading', { name: 'Batch results' })).toBeTruthy();
    expect(screen.getByText('1 tasks added or located, 1 issues or decisions needed.')).toBeTruthy();
    expect((screen.getByRole('textbox', { name: 'Download links' }) as HTMLTextAreaElement).value).toBe('https://youtu.be/zyxwvutsrqp');
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });
  it('adds successive downloads when plain HTTP does not expose randomUUID', async () => {
    const original = Object.getOwnPropertyDescriptor(crypto, 'randomUUID');
    Object.defineProperty(crypto, 'randomUUID', { configurable: true, value: undefined });
    try {
      render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
      const button = screen.getByRole('button', { name: 'Add downloads' }) as HTMLButtonElement;
      for (const [index, url] of ['https://youtu.be/abcdefghijk', 'https://youtu.be/zyxwvutsrqp'].entries()) {
        fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: url } });
        fireEvent.click(button);
        await waitFor(() => expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.add')).toHaveLength(index + 1));
        await waitFor(() => expect(screen.queryByRole('button', { name: 'Adding to queue…' })).toBeNull());
      }
    } finally { if (original) Object.defineProperty(crypto, 'randomUUID', original); else Reflect.deleteProperty(crypto, 'randomUUID'); }
  });
  it('recovers from clipboard permission failures without starting a download', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { readText: vi.fn().mockRejectedValue(new Error('Denied')) } });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox'); fireEvent.click(screen.getByRole('button', { name: 'Download clipboard' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Paste the links manually');
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.add')).toBe(false);
  });
  it('continues a mixed batch after identification fails and summarizes the failed link', async () => {
    const urls = ['https://example.test/one', 'https://example.test/bad', 'https://example.test/three'];
    transport.command.mockImplementation(async (name, args) => {
      if (name === 'snapshot') return makeSnapshot();
      if (name === 'probe.collection') { if (args.url === urls[1]) throw new Error('Unreadable source'); return { kind: 'video', entries: [{ url: args.url, title: args.url }] }; }
      if (name === 'tasks.add') return { ids: [args.urls[0]], results: [{ id: args.urls[0], status: 'queued', action: 'added' }] };
      return true;
    });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: urls.join('\n') } }); fireEvent.click(screen.getByRole('button', { name: 'Add downloads' }));
    expect(await screen.findByRole('heading', { name: 'Batch results' })).toBeTruthy();
    expect(screen.getByText('2 tasks added or located, 1 issues or decisions needed.')).toBeTruthy(); expect(screen.getByText(/View the logs for details/)).toBeTruthy();
    expect(screen.queryByText(/Unreadable source/)).toBeNull();
    expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.add').map(([, args]) => args.urls[0])).toEqual([urls[0], urls[2]]);
    expect((screen.getByRole('textbox', { name: 'Download links' }) as HTMLTextAreaElement).value).toBe(urls[1]);
  });
  it('cancels in-flight batch checks without adding the identified or remaining links', async () => {
    let fail!: (error: Error) => void;
    transport.command.mockImplementation(async name => {
      if (name === 'snapshot') return makeSnapshot();
      if (name === 'probe.collection') return new Promise((_resolve, reject) => { fail = reject; });
      if (name === 'probe.cancel') { fail(new Error('Operation interrupted')); return true; }
      return true;
    });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: 'https://example.test/slow\nhttps://example.test/next' } }); fireEvent.click(screen.getByRole('button', { name: 'Add downloads' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel checks' }));
    expect(await screen.findByRole('heading', { name: 'Batch results' })).toBeTruthy();
    expect(screen.getByText('Remaining checks and additions were cancelled. Created tasks keep running.')).toBeTruthy();
    expect(transport.command).toHaveBeenCalledWith('probe.cancel', { id: expect.any(String) });
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.add')).toBe(false);
  });
  it('requires the user to adjust unmet requirements before restarting the same source', async () => {
    const snapshot = makeSnapshot(); Object.assign(snapshot.tasks[0], { url: 'https://example.test/watch', status: 'failed', error: 'Requirements unmet: video=4k / 60 fps; available: 1080p / 30 fps', options: { quality: '4k', videoFps: '60' } });
    transport.command.mockImplementation(async name => name === 'snapshot' ? snapshot : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox'); fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Adjust requirements and retry' }));
    const dialog = within(await screen.findByRole('dialog')); expect(dialog.getAllByText(/source does not meet/i)).toHaveLength(2);
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.add')).toBe(false);
    fireEvent.change(dialog.getByRole('combobox', { name: 'Quality selection' }), { target: { value: '1080p' } });
    fireEvent.change(dialog.getByRole('combobox', { name: 'Frame rate requirement' }), { target: { value: '30' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Retry with these requirements' }));
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.retryCurrent', { id: 'a', options: expect.objectContaining({ quality: '1080p', videoFps: '30', cookieId: 'auto', collectionMode: 'single' }) }));
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.add')).toBe(false);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
  it('waits for the other batch tasks before reporting download failures once', async () => {
    const snapshot = makeSnapshot();
    transport.command.mockImplementation(async (name, args) => {
      if (name === 'snapshot') return snapshot;
      if (name === 'tasks.add') { const task = args.urls[0].includes('abcdefghijk') ? snapshot.tasks[0] : snapshot.tasks[1]; return { ids: [task.id], results: [{ id: task.id, status: 'queued', action: 'added' }] }; }
      return true;
    });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: 'https://youtu.be/abcdefghijk\nhttps://youtu.be/zyxwvutsrqp' } }); fireEvent.click(screen.getByRole('button', { name: 'Add downloads' }));
    await waitFor(() => expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.add')).toHaveLength(2));
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'failed', error: 'Requirements unmet: subtitles=ja' } }));
    expect(screen.queryByRole('heading', { name: 'Batch results' })).toBeNull();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'b', status: 'completed', stage: 'completed' } }));
    const report = within(await screen.findByRole('dialog')); expect(report.getByText(/requested subtitles are unavailable/)).toBeTruthy();
    expect(report.queryByText(/Requirements unmet: subtitles=ja/)).toBeNull();
    fireEvent.click(report.getByRole('button', { name: 'Close' }));
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'b', progress: 100 } }));
    expect(screen.queryByRole('heading', { name: 'Batch results' })).toBeNull();
  });
  it.each(['best', 'worst'])('adds a batch with %s quality without opening the manual format picker', async quality => {
    const snapshot = makeSnapshot();
    snapshot.settings.downloadOptions = { contentMode: 'manual', quality: 'best', formatId: 'old', videoFormatId: 'old-video', audioFormatIds: ['old-audio'] };
    const entries = [{ url: 'https://example.test/first', title: 'First video' }, { url: 'https://example.test/second', title: 'Second video' }];
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'probe.collection' ? { entries } : true);
    render(<AppProvider><App /></AppProvider>);
    const selector = await screen.findByRole('combobox', { name: 'Quality' }) as HTMLSelectElement;
    expect(selector.value).toBe('manual');
    fireEvent.change(selector, { target: { value: quality } });
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', progress: 60 } }));
    expect(selector.value).toBe(quality);
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: 'https://example.test/collection' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add downloads' }));
    const confirm = await screen.findByRole('button', { name: 'Download selected items' });
    await waitFor(() => expect(confirm.hasAttribute('disabled')).toBe(false));
    fireEvent.click(confirm);
    await waitFor(() => expect(transport.command.mock.calls.filter(([command]) => command === 'tasks.add')).toHaveLength(2));
    for (const entry of entries) expect(transport.command).toHaveBeenCalledWith('tasks.add', { urls: [entry.url], options: expect.objectContaining({ quality, contentMode: 'video', collectionMode: 'single', formatId: '', videoFormatId: '', audioFormatIds: [] }), report: true });
    expect(transport.command.mock.calls.some(([command]) => command === 'probe.video')).toBe(false);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(selector.value).toBe(quality);
  });
  it('saves automatic quality from quick options and keeps audio-only settings', async () => {
    const snapshot = makeSnapshot();
    snapshot.settings.downloadOptions = { contentMode: 'audio', quality: 'best', formatId: 'old', videoFormatId: 'old-video', audioFormatIds: ['old-audio'], audioFormat: 'mp3' };
    transport.command.mockImplementation(async (command, args) => command === 'snapshot' ? snapshot : command === 'settings.save' ? args.settings : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('button', { name: 'Quick options' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Quality selection' }), { target: { value: 'worst' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save batch plan' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(transport.command).toHaveBeenCalledWith('settings.save', { settings: expect.objectContaining({ downloadOptions: expect.objectContaining({ quality: 'worst', contentMode: 'audio', audioFormat: 'mp3', formatId: '', videoFormatId: '', audioFormatIds: [] }) }) });
    expect((screen.getByRole('combobox', { name: 'Quality' }) as HTMLSelectElement).value).toBe('worst');
  });
  it('gives concise recovery guidance for failed downloads and keeps the raw diagnostic in logs', async () => {
    const raw = 'yt-dlp.exe exited with code 1: Sign in to confirm you are not a bot';
    transport.command.mockImplementation(async command => command === 'snapshot' ? makeSnapshot() : command === 'tasks.logs' ? { text: raw, start: 0, end: raw.length, total: raw.length, legacy: false } : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'failed', error: raw } }));
    expect(screen.getByRole('alert').textContent).toContain('Open browser sign-in');
    expect(screen.getByRole('alert').textContent).not.toContain('yt-dlp.exe exited with code 1');
    fireEvent.click(screen.getByRole('button', { name: 'View logs' }));
    expect((await screen.findByLabelText('Task log entries')).textContent).toBe(raw);
  });
  it('keeps URL drafts, caret, filter, selected hidden task and virtual viewport during progress updates', async () => {
    render(<AppProvider><App /></AppProvider>);
    await screen.findByRole('listbox', { name: 'Download task list' });
    const draft = screen.getByRole('textbox', { name: 'Download links' }) as HTMLTextAreaElement;
    fireEvent.change(draft, { target: { value: 'https://example.test/not-submitted' } }); draft.focus(); draft.setSelectionRange(10, 18);
    const search = screen.getByRole('searchbox', { name: 'Search tasks' }); fireEvent.change(search, { target: { value: 'Beta' } });
    fireEvent.change(search, { target: { value: '' } }); fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    fireEvent.change(search, { target: { value: 'Beta' } }); draft.focus(); draft.setSelectionRange(10, 18);
    const viewport = screen.getByRole('listbox', { name: 'Download task list' });
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', progress: 77, stageProgress: 77 } }));
    expect(draft.value).toBe('https://example.test/not-submitted'); expect(document.activeElement).toBe(draft); expect(draft.selectionStart).toBe(10); expect(draft.selectionEnd).toBe(18);
    expect((search as HTMLInputElement).value).toBe('Beta'); expect(screen.getByText('Selected task is hidden by the current filter.')).toBeTruthy(); expect(screen.getByRole('listbox')).toBe(viewport);
  });
  it('keeps unsaved settings input identity and text when progress and settings events arrive', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    await waitFor(() => expect(screen.getByLabelText(/Filename template/)).toBeTruthy());
    const input = screen.getByLabelText(/Filename template/) as HTMLInputElement; fireEvent.change(input, { target: { value: '%(title)s-custom.%(ext)s' } }); input.focus();
    act(() => { transport.listener?.({ kind: 'task.upsert', data: { id: 'b', progress: 44, stageProgress: 44 } }); transport.listener?.({ kind: 'settings.updated', data: { uiLanguage: 'en-US', downloadOptions: { filenameTemplate: 'remote-template' } } }); });
    expect(screen.getByLabelText(/Filename template/)).toBe(input); expect(input.value).toBe('%(title)s-custom.%(ext)s'); expect(document.activeElement).toBe(input); expect(screen.getByText('Unsaved changes')).toBeTruthy();
  });
  it('refreshes a missed snapshot while preserving the selected task and URL draft', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: 'still editing' } });
    const next = makeSnapshot(); next.tasks[0].progress = 92; next.tasks[0].stageProgress = 92; next.tasks.reverse();
    transport.command.mockImplementation(async command => command === 'snapshot' ? next : []);
    act(() => transport.listener?.({ kind: 'snapshot.required', data: null }));
    await waitFor(() => expect(screen.getAllByText('92.0%').length).toBeGreaterThan(0));
    expect((screen.getByRole('textbox', { name: 'Download links' }) as HTMLTextAreaElement).value).toBe('still editing');
    expect(within(screen.getByRole('listbox')).getByRole('option', { selected: true }).textContent).toContain('Alpha');
  });
  it('reconciles missed live progress and cleared errors in the list and details every two seconds', async () => {
    vi.useFakeTimers(); vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    const initial = makeSnapshot(); Object.assign(initial.tasks[0], { progress: 14.7, stageProgress: 14.7, error: 'Previous download failed' });
    const latest = makeSnapshot(); Object.assign(latest.tasks[0], { progress: 25.9, stageProgress: 25.9, error: null });
    let snapshots = 0;
    transport.command.mockImplementation(async command => command === 'snapshot' ? ++snapshots === 1 ? initial : latest : []);
    render(<AppProvider><App /></AppProvider>); await act(async () => {});
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: 'still editing' } });
    const row = within(screen.getByRole('option', { name: /Alpha/ }));
    const detail = within(screen.getByRole('complementary', { name: 'Task details' }));
    expect(row.getByText('14.7%')).toBeTruthy(); expect(detail.getByText('14.7%')).toBeTruthy();
    expect(detail.getByRole('alert')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(1999); });
    expect(snapshots).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(row.getByText('25.9%')).toBeTruthy(); expect(detail.getByText('25.9%')).toBeTruthy();
    expect(detail.queryByRole('alert')).toBeNull();
    expect((screen.getByRole('textbox', { name: 'Download links' }) as HTMLTextAreaElement).value).toBe('still editing');
    expect(within(screen.getByRole('listbox')).getByRole('option', { selected: true }).textContent).toContain('Alpha');
    expect(transport.command.mock.calls.filter(([command]) => command === 'snapshot').every(([, args]) => args.includeTools === false)).toBe(true);
    expect(transport.command.mock.calls.filter(([command]) => command === 'tools.check')).toHaveLength(1);
  });
  it('reconciles on return to the visible app without overlapping refreshes or losing buffered events', async () => {
    vi.useFakeTimers(); let visibility: DocumentVisibilityState = 'visible';
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
    let snapshots = 0; let finish!: (value: unknown) => void;
    const complete = makeSnapshot(); for (const task of complete.tasks) Object.assign(task, { status: 'completed', stage: 'completed', error: null, mediaInfo: { width: 1920, height: 1080 } });
    transport.command.mockImplementation(async command => {
      if (command !== 'snapshot') return [];
      if (++snapshots === 1) return makeSnapshot();
      if (snapshots === 2) return new Promise(resolve => { finish = resolve; });
      return complete;
    });
    const view = render(<AppProvider><App /></AppProvider>); await act(async () => {});
    fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(snapshots).toBe(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); fireEvent.focus(window); fireEvent(document, new Event('visibilitychange')); });
    expect(snapshots).toBe(2);
    const older = makeSnapshot(); Object.assign(older.tasks[0], { progress: 25, stageProgress: 25 });
    await act(async () => {
      transport.listener?.({ kind: 'task.upsert', data: { id: 'a', progress: 42, stageProgress: 42 } });
      finish(older);
    });
    expect(within(screen.getByRole('option', { name: /Alpha/ })).getByText('42.0%')).toBeTruthy();
    expect(within(screen.getByRole('complementary', { name: 'Task details' })).getByText('42.0%')).toBeTruthy();
    expect(snapshots).toBe(2);
    visibility = 'hidden'; fireEvent(document, new Event('visibilitychange'));
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(snapshots).toBe(2);
    visibility = 'visible'; await act(async () => { fireEvent(document, new Event('visibilitychange')); });
    expect(snapshots).toBe(3);
    expect(screen.getByRole('option', { name: /Alpha/ }).textContent).toContain('Completed');
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(snapshots).toBe(3);
    await act(async () => { fireEvent.focus(window); });
    expect(snapshots).toBe(4);
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); fireEvent.focus(window); fireEvent(document, new Event('visibilitychange')); });
    expect(snapshots).toBe(4); expect(transport.listener).toBeNull();
  });
  it('notifies once for completion during a periodic refresh while leaving initial completed history quiet', async () => {
    vi.useFakeTimers(); vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    const initial = makeSnapshot(); initial.settings.desktopNotifications = true; initial.tasks[1].status = 'completed';
    const completed = { ...initial, tasks: initial.tasks.map(task => ({ ...task, status: 'completed' as const, stage: 'completed' })) };
    let snapshots = 0; let finish!: (value: unknown) => void;
    transport.command.mockImplementation(async command => {
      if (command !== 'snapshot') return [];
      if (++snapshots === 1) return initial;
      if (snapshots === 2) return new Promise(resolve => { finish = resolve; });
      return completed;
    });
    render(<AppProvider><App /></AppProvider>); await act(async () => {});
    await act(async () => { transport.listener?.({ kind: 'task.upsert', data: { id: 'b', title: 'Beta', status: 'completed' } }); });
    expect(transport.notify).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    await act(async () => { transport.listener?.({ kind: 'task.upsert', data: { id: 'a', title: 'Alpha', status: 'completed' } }); });
    expect(transport.notify).toHaveBeenCalledExactlyOnceWith('Download complete', 'Alpha');
    await act(async () => {
      finish(completed);
      transport.listener?.({ kind: 'task.upsert', data: { id: 'a', title: 'Alpha', status: 'completed' } });
    });
    await act(async () => { fireEvent.focus(window); });
    expect(transport.notify).toHaveBeenCalledTimes(1);
  });
  it('still notifies once when a periodic snapshot sees completion before the completion event', async () => {
    vi.useFakeTimers(); vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    const initial = makeSnapshot(); initial.settings.desktopNotifications = true; initial.tasks[1].status = 'completed';
    const completed = { ...initial, tasks: initial.tasks.map(task => ({ ...task, status: 'completed' as const, stage: 'completed' })) };
    let snapshots = 0;
    transport.command.mockImplementation(async command => command === 'snapshot' ? ++snapshots === 1 ? initial : completed : []);
    render(<AppProvider><App /></AppProvider>); await act(async () => {});
    expect(transport.notify).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByRole('option', { name: /Alpha/ }).textContent).toContain('Completed');
    expect(transport.notify).not.toHaveBeenCalled();
    await act(async () => { transport.listener?.({ kind: 'task.upsert', data: { id: 'a', title: 'Alpha', status: 'completed' } }); });
    expect(transport.notify).toHaveBeenCalledExactlyOnceWith('Download complete', 'Alpha');
    await act(async () => {
      transport.listener?.({ kind: 'task.upsert', data: { id: 'a', title: 'Alpha', status: 'completed' } });
      fireEvent.focus(window);
    });
    expect(transport.notify).toHaveBeenCalledTimes(1);
  });
  it('fetches a new snapshot when more events are missed during an in-flight refresh', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    let finish!: (value: unknown) => void;
    transport.command.mockImplementation(async command => command === 'snapshot' ? new Promise(resolve => { finish = resolve; }) : []);
    act(() => transport.listener?.({ kind: 'snapshot.required', data: null }));
    await waitFor(() => expect(typeof finish).toBe('function'));
    const latest = makeSnapshot(); latest.tasks[0].status = 'completed'; latest.tasks[0].stage = 'completed';
    transport.command.mockImplementation(async command => command === 'snapshot' ? latest : []);
    act(() => { transport.listener?.({ kind: 'snapshot.required', data: null }); transport.listener?.({ kind: 'snapshot.required', data: null }); });
    expect(transport.command.mock.calls.filter(([name]) => name === 'snapshot')).toHaveLength(2);
    await act(async () => finish(makeSnapshot()));
    await waitFor(() => expect(screen.getByRole('option', { name: /Alpha/ }).textContent).toContain('Completed'));
    expect(transport.command.mock.calls.filter(([name]) => name === 'snapshot')).toHaveLength(3);
  });
  it('updates filtered and sorted rows when metadata changes after progress-only events', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    const rows = () => within(screen.getByRole('listbox')).getAllByRole('option');
    fireEvent.change(screen.getByRole('combobox', { name: 'Sort' }), { target: { value: 'title' } });
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'b', progress: 45 } }));
    expect(rows()[0].textContent).toContain('Alpha');
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'b', title: 'Aardvark' } }));
    expect(rows()[0].textContent).toContain('Aardvark');
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search tasks' }), { target: { value: 'needle' } });
    expect(screen.getByText('No matching tasks')).toBeTruthy();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', url: 'https://example.test/needle' } }));
    expect(rows()[0].textContent).toContain('Alpha');
    fireEvent.change(screen.getByRole('combobox', { name: 'Status filter' }), { target: { value: 'paused' } });
    expect(screen.getByText('No matching tasks')).toBeTruthy();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'paused' } }));
    expect(rows()[0].textContent).toContain('Alpha');
    expect(screen.getByText('2 tasks · 1 active')).toBeTruthy();
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search tasks' }), { target: { value: '' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'Status filter' }), { target: { value: 'all' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'Sort' }), { target: { value: 'newest' } });
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'b', createdAt: '2030-01-01T00:00:00Z' } }));
    expect(rows()[0].textContent).toContain('Aardvark');
  });
});
