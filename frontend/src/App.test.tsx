import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { AppProvider } from './state';
import { makeSnapshot } from './test-fixtures';
import type { CoreEvent } from './types';

const transport = vi.hoisted(() => ({ listener: null as null | ((event: CoreEvent) => void), command: vi.fn(), releaseStatus: vi.fn(), openRelease: vi.fn() }));
vi.mock('./transport', () => ({ host: {
  desktop: false, capabilities: () => ({ browserCookies: false, remoteBrowser: false, updates: false }),
  session: async () => ({ authenticated: true }), subscribe: async (listener: (event: CoreEvent) => void, connection: (value: boolean) => void) => { transport.listener = listener; connection(true); return () => { transport.listener = null; }; },
  command: transport.command, assetUrl: (id: string, kind = 'cover') => `/api/files/${id}?kind=${kind}`, notify: async () => {}, releaseStatus: transport.releaseStatus, openRelease: transport.openRelease, copyTask: async () => {},
} }));
beforeEach(() => {
  transport.command.mockReset(); transport.command.mockImplementation(async command => command === 'snapshot' ? makeSnapshot() : command === 'cookies.list' ? [] : true);
  transport.releaseStatus.mockReset().mockResolvedValue({ currentVersion:'0.3.0', latestVersion:'0.3.0', updateAvailable:false, url:'https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.3.0' });
  transport.openRelease.mockReset().mockResolvedValue(undefined);
});
describe('live interface stability', () => {
  it('shows actual completed quality and bytes with separate source and result details', async () => {
    const snapshot = makeSnapshot();
    Object.assign(snapshot.tasks[0], { status: 'completed', stage: 'completed', size: 10485760, sourceInfo: { width: 3840, height: 2160, fps: 59.94, videoCodec: 'vp9', audioCodec: 'opus', size: 20971520, duration: 60, container: 'webm' }, mediaInfo: { width: 1920, height: 1080, fps: 29.97, videoCodec: 'h264', audioCodec: 'aac', size: 10485760, duration: 60, container: 'mp4' } });
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
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
  it('reads legacy completed file information once and updates the row without using its quality preset', async () => {
    const snapshot = makeSnapshot();
    Object.assign(snapshot.tasks[0], { status: 'completed', stage: 'completed', size: 1048576, options: { quality: 'best' } });
    let finish!: (value: unknown) => void;
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'tasks.info' ? new Promise(resolve => { finish = resolve; }) : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
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
    expect(await screen.findByText('Completed · 1080p')).toBeTruthy();
    expect(screen.getByText('1080p · 1920 × 1080')).toBeTruthy();
    expect(screen.queryByText('Reading local file information…')).toBeNull();
    expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.info')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Downloads' }));
    expect(screen.queryByRole('option', { name: /Retained video/ })).toBeNull();
  });
  it('offers retry and a concrete error tooltip for legacy media information', async () => {
    const snapshot = makeSnapshot();
    snapshot.media = [{ id: 'old-media', taskId: 'old-task', title: 'Old video', filename: 'old.mp4', size: 10485760, duration: 60 }];
    transport.command.mockImplementation(async command => { if (command === 'snapshot') return snapshot; if (command === 'tasks.info') throw new Error('fixture local file is missing'); return true; });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getAllByRole('button', { name: 'Completed' })[0]);
    const error = await screen.findByText(/Result information could not be read/);
    expect(error.getAttribute('title')).toContain('fixture local file is missing');
    expect(screen.queryByRole('alert')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.info')).toHaveLength(2));
  });
  it('shows a task cover during download before a completed media record exists', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    const detail = screen.getByRole('complementary', { name: 'Task details' });
    expect(detail.querySelector('.thumbnail img')).toBeNull();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'downloading', hasThumbnail: true } }));
    await waitFor(() => expect(detail.querySelector('.thumbnail img')?.getAttribute('src')).toBe('/api/files/a?kind=taskThumbnail'));
  });
  it('opens task actions with right click and acts on that task rather than the previous selection', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
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
  it('supports keyboard context menus, status-specific actions and dismissing stale virtual rows', async () => {
    render(<AppProvider><App /></AppProvider>); const list = await screen.findByRole('listbox');
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
    expect((await dialog.findByRole('alert')).textContent).toContain('Unable to save preference');
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
  it('updates processing step counts in both the task row and selected task details', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'processing', stage: 'muxing', stepIndex: 2, stepCount: 4, stageProgress: 35 } }));
    const row = within(screen.getByRole('option', { name: /Alpha/ }));
    const detail = within(screen.getByRole('complementary', { name: 'Task details' }));
    expect(row.getByText('2/4 Merging video and audio')).toBeTruthy();
    expect(detail.getByText('2/4 Merging video and audio')).toBeTruthy();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', stage: 'verifying_output', stepIndex: 3, stageProgress: 80 } }));
    expect(row.getByText('3/4 Validating result file')).toBeTruthy();
    expect(detail.getByText('3/4 Validating result file')).toBeTruthy();
    expect(row.queryByText('2/4 Merging video and audio')).toBeNull();
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
    const confirm = dialog.getByRole('button', { name: 'Download selected items' });
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
    let calls = 0; transport.command.mockImplementation(async name => { if (name === 'snapshot') return makeSnapshot(); if (name === 'tasks.add' && ++calls === 2) throw new Error('Queue fixture failed'); return true; });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    const button = screen.getByRole('button', { name: 'Download clipboard' }); fireEvent.click(button); fireEvent.click(button);
    expect(readText).toHaveBeenCalledTimes(1);
    await act(async () => release('https://youtu.be/abcdefghijk\nhttps://youtu.be/abcdefghijk\nhttps://youtu.be/zyxwvutsrqp'));
    expect((await screen.findByRole('alert')).textContent).toContain('Added 1 tasks');
    expect((screen.getByRole('textbox', { name: 'Download links' }) as HTMLTextAreaElement).value).toBe('https://youtu.be/zyxwvutsrqp');
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });
  it('recovers from clipboard permission failures without starting a download', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { readText: vi.fn().mockRejectedValue(new Error('Denied')) } });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox'); fireEvent.click(screen.getByRole('button', { name: 'Download clipboard' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Paste the links manually');
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.add')).toBe(false);
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
  it('gives recovery guidance for failed downloads while keeping the diagnostic', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'failed', error: 'yt-dlp.exe exited with code 1: Sign in to confirm you are not a bot' } }));
    expect(screen.getByRole('alert').textContent).toContain('Open browser sign-in');
    expect(screen.getByRole('alert').textContent).toContain('yt-dlp.exe exited with code 1');
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
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: 'still editing' } });
    const next = makeSnapshot(); next.tasks[0].progress = 92; next.tasks[0].stageProgress = 92; next.tasks.reverse();
    transport.command.mockImplementation(async command => command === 'snapshot' ? next : []);
    act(() => transport.listener?.({ kind: 'snapshot.required', data: null }));
    await waitFor(() => expect(screen.getAllByText('92.0%').length).toBeGreaterThan(0));
    expect((screen.getByRole('textbox', { name: 'Download links' }) as HTMLTextAreaElement).value).toBe('still editing');
    expect(within(screen.getByRole('listbox')).getByRole('option', { selected: true }).textContent).toContain('Alpha');
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
