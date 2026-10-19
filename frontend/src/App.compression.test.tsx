import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { App } from './App';
import { AppProvider, useApp } from './state';
import { makeSnapshot, makeTask } from './test-fixtures';
import type { CoreEvent, Json, Snapshot, Task } from './types';
import type { CompressionOptions, CompressionPreview } from './compression';

const transport = vi.hoisted(() => ({ command: vi.fn(), listener: null as null | ((event: CoreEvent) => void) }));
vi.mock('./transport', () => ({ host: {
  desktop: true, capabilities: () => ({ desktop: true, browserCookies: false, remoteBrowser: false, updates: false }),
  session: async () => ({ authenticated: true }),
  subscribe: async (listener: (event: CoreEvent) => void, connected: (value: boolean) => void) => { transport.listener = listener; connected(true); return () => { transport.listener = null; }; },
  command: transport.command, assetUrl: (id: string) => `/files/${id}`,
  releaseStatus: async () => ({ currentVersion: '0.5.4', latestVersion: '0.5.4', updateAvailable: false }),
  paths: async () => ({ downloadDir: 'D:/downloads' }),
} }));

const preview: CompressionPreview = {
  source: { sizeBytes: 250000000, duration: 120, width: 1920, height: 1080, displayWidth: 1920, displayHeight: 1080, fps: '60000/1001', videoBitrateBps: 16000000, videoBitrateEstimated: false, hdr: 'sdr', bitDepth: 8, audioTracks: 2, subtitleTracks: 1, attachmentTracks: 1 },
  plan: null, capabilities: [{ encoder: 'libx265', label: 'H.265 (software)', available: true, reason: null }], blockingReasons: [], warnings: [],
};
let snapshot: Snapshot;
let acceptCompression: ((task: Task) => void) | undefined;
let acceptCpuRetry: ((task: Task) => void) | undefined;

function SelectedTaskState() {
  const { state } = useApp();
  return <output aria-label="Selected task state">{JSON.stringify(state.tasks[state.view.taskSelection] || null)}</output>;
}
function selectedTask(): Task { return JSON.parse(screen.getByLabelText('Selected task state').textContent!); }
function emitTask(task: Task) { transport.listener?.({ kind: 'task.upsert', data: task as unknown as Json }); }

beforeEach(() => {
  snapshot = makeSnapshot();
  const original: Task = { ...makeTask('source', 'Original video'), status: 'completed', stage: 'completed', progress: 100, stageProgress: 100, outputPath: 'D:/downloads/source.mp4', size: 250000000, mediaInfo: { width: 1920, height: 1080, fps: 59.94, duration: 120, videoCodec: 'h264', audioCodec: 'aac', container: 'mp4', size: 250000000 } };
  snapshot.tasks = [original];
  snapshot.media = [{ id: 'media-source', taskId: original.id, title: original.title, filename: 'source.mp4', size: 250000000, duration: 120, mediaInfo: original.mediaInfo }];
  acceptCompression = undefined; acceptCpuRetry = undefined; transport.listener = null; transport.command.mockReset();
  transport.command.mockImplementation(async (command, args) => {
    if (command === 'snapshot') return snapshot;
    if (command === 'media.availability') return args.ids.map((id: string) => ({ id, status: 'available' }));
    if (command === 'tasks.get' || command === 'tasks.info') return snapshot.tasks.find(task => task.id === args.id);
    if (command === 'tasks.convert') return new Promise<Task>(resolve => { acceptCompression = resolve; });
    if (command === 'tasks.compressRetryCpu') return new Promise<Task>(resolve => { acceptCpuRetry = resolve; });
    if (command === 'tasks.convertPreview') {
      const compression: CompressionOptions = args.compression;
      if (!compression.targetBytes && !compression.videoBitrateBps) return structuredClone(preview);
      const videoBitrateBps = compression.videoBitrateBps || Math.floor(compression.targetBytes! * 8 / preview.source.duration) - 100000;
      return { ...structuredClone(preview), plan: { encoder: compression.encoder || 'libx265', videoBitrateBps, estimatedSizeBytes: compression.targetBytes || Math.round((videoBitrateBps + 100000) * preview.source.duration / 8), width: compression.maxWidth || 1920, height: compression.maxHeight || 1080, fps: compression.fps || '60000/1001', targetBytes: compression.targetBytes } };
    }
    return [];
  });
});

it.each(['simple', 'advanced'] as const)('submits %s compression from a completed local video, selects the new task and preserves progress ahead of a late response', async mode => {
  render(<AppProvider><App /><SelectedTaskState /></AppProvider>);
  await screen.findByRole('listbox', { name: 'Download task list' });
  fireEvent.change(screen.getByRole('searchbox', { name: 'Search tasks' }), { target: { value: 'unrelated video' } });
  fireEvent.change(screen.getByRole('combobox', { name: 'Status filter' }), { target: { value: 'failed' } });
  fireEvent.click(screen.getByLabelText('Completed', { selector: 'button' }));
  const library = await screen.findByRole('listbox', { name: 'Completed media list' });
  fireEvent.contextMenu(within(library).getByRole('option', { name: /Original video/ }));
  const compress = screen.getByRole('menuitem', { name: 'Compress video' });
  await waitFor(() => expect((compress as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(compress);
  const dialog = within(await screen.findByRole('dialog', { name: 'Compress video' }));
  const target = await dialog.findByRole('textbox', { name: 'Target size' });
  let compression: CompressionOptions;
  if (mode === 'simple') {
    fireEvent.change(target, { target: { value: '100MB' } });
    compression = { targetBytes: 100000000, encoder: 'libx265' };
  } else {
    fireEvent.click(dialog.getByRole('button', { name: 'Advanced' }));
    fireEvent.change(dialog.getByRole('textbox', { name: 'Video bitrate' }), { target: { value: '4Mbps' } });
    fireEvent.change(dialog.getByRole('combobox', { name: 'Resolution' }), { target: { value: '720' } });
    fireEvent.change(dialog.getByRole('combobox', { name: 'Frame rate' }), { target: { value: '30000/1001' } });
    compression = { videoBitrateBps: 4000000, encoder: 'libx265', maxWidth: 1280, maxHeight: 720, fps: '30000/1001' };
  }
  const start = dialog.getByRole('button', { name: 'Start compression' });
  await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(start);
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.convert', { id: 'source', compression, requestId: expect.any(String) }));
  expect(acceptCompression).toBeTypeOf('function');
  const queued: Task = { ...makeTask('compressed', 'Compressed video'), conversion: true, compression, status: 'waiting_processing', stage: 'waiting_processing', progress: 0, stageProgress: null, stageIndeterminate: true };
  await act(async () => {
    emitTask({ ...queued, status: 'processing', stage: 'compressing', progress: 42, stageProgress: 42, stageIndeterminate: false });
    acceptCompression!(queued);
  });
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Compress video' })).toBeNull());
  const downloads = screen.getByRole('listbox', { name: 'Download task list' });
  expect(downloads.getAttribute('aria-activedescendant')).toBe(within(downloads).getByRole('option', { name: /Compressed video/ }).id);
  expect(within(downloads).getByRole('option', { name: /Original video/ }).textContent).toContain('Completed');
  expect((screen.getByRole('searchbox', { name: 'Search tasks' }) as HTMLInputElement).value).toBe('');
  expect((screen.getByRole('combobox', { name: 'Status filter' }) as HTMLSelectElement).value).toBe('all');
  expect(screen.queryByText('Selected task is hidden by the current filter.')).toBeNull();
  expect(selectedTask()).toMatchObject({ id: 'compressed', status: 'processing', progress: 42, compression });
  expect(within(screen.getByRole('complementary', { name: 'Task details' })).getByRole('progressbar').getAttribute('aria-valuenow')).toBe('42');
  expect(transport.command.mock.calls.filter(([command]) => command === 'tasks.convert')).toHaveLength(1);
  expect(transport.command.mock.calls.some(([command]) => command === 'tasks.add')).toBe(false);
});

it.each([false, true])('retries a failed GPU compression with CPU and protects a newer event: %s', async newerEvent => {
  const failed: Task = { ...makeTask('gpu', 'GPU compression'), conversion: true, compression: { targetBytes: 100000000, encoder: 'hevc_nvenc' }, status: 'processing_failed', stage: 'processing_failed', error: 'GPU compression encoder failed' };
  snapshot.tasks = [failed]; snapshot.media = [];
  render(<AppProvider><App /><SelectedTaskState /></AppProvider>);
  fireEvent.click(await screen.findByRole('option', { name: /GPU compression/ }));
  const detail = within(screen.getByRole('complementary', { name: 'Task details' }));
  fireEvent.click(detail.getByRole('button', { name: 'Retry with CPU' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.compressRetryCpu', { id: 'gpu' }));
  expect(acceptCpuRetry).toBeTypeOf('function');
  const queued: Task = { ...failed, compression: { ...failed.compression, encoder: 'libx265' }, status: 'waiting_processing', stage: 'waiting_processing', error: null, progress: 0, stageProgress: null, stageIndeterminate: true };
  await act(async () => {
    if (newerEvent) emitTask({ ...queued, status: 'processing', stage: 'compressing', progress: 42, stageProgress: 42, stageIndeterminate: false });
    acceptCpuRetry!(queued);
  });
  expect(selectedTask()).toMatchObject({ id: 'gpu', status: newerEvent ? 'processing' : 'waiting_processing', progress: newerEvent ? 42 : 0, error: null, compression: { targetBytes: 100000000, encoder: 'libx265' } });
  expect(detail.queryByRole('button', { name: 'Retry with CPU' })).toBeNull();
  expect(detail.queryByRole('alert')).toBeNull();
  if (newerEvent) expect(detail.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('42');
  expect(await screen.findByText('Request accepted. The task will follow the queue.', { selector: '[role="status"]' })).toBeTruthy();
});
