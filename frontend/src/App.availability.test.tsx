import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { AppProvider, useApp } from './state';
import { makeSnapshot, makeTask } from './test-fixtures';
import type { CoreEvent, Snapshot } from './types';
import type { CompressionPreview } from './compression';

const transport = vi.hoisted(() => ({ command: vi.fn(), result: vi.fn(), listener: null as null | ((event: CoreEvent) => void) }));
vi.mock('./transport', () => ({ host: {
  desktop: true, capabilities: () => ({ desktop: true, browserCookies: false, remoteBrowser: false, updates: false }),
  session: async () => ({ authenticated: true }),
  subscribe: async (listener: (event: CoreEvent) => void, connected: (value: boolean) => void) => { transport.listener = listener; connected(true); return () => { transport.listener = null; }; },
  command: transport.command, result: transport.result,
  assetUrl: (id: string) => `/files/${id}`,
  releaseStatus: async () => ({ currentVersion: '0.5.4', latestVersion: '0.5.4', updateAvailable: false }),
  paths: async () => ({ downloadDir: 'D:/downloads' }),
} }));

let snapshot: Snapshot;
let available: 'available' | 'missing' | 'unknown';
const preview: CompressionPreview = {
  source: { sizeBytes: 5 * 1024 ** 2, duration: 60, width: 1920, height: 1080, displayWidth: 1920, displayHeight: 1080, fps: '30/1', videoBitrateBps: 600000, videoBitrateEstimated: false, hdr: 'sdr', bitDepth: 8, audioTracks: 1, subtitleTracks: 0, attachmentTracks: 0 },
  plan: null, capabilities: [{ encoder: 'libx265', label: 'H.265 (software)', available: true, reason: null }], blockingReasons: [], warnings: [],
};
function library(count = 1): Snapshot {
  const result = makeSnapshot();
  result.tasks = Array.from({ length: count }, (_, index) => ({ ...makeTask(`task-${index}`, `Video ${index}`), status: 'completed' as const, stage: 'completed', progress: 100, stageProgress: 100, size: 5 * 1024 ** 2, mediaInfo: { width: 1920, height: 1080, fps: 30, duration: 60, videoCodec: 'h264', audioCodec: 'aac', container: 'matroska', size: 5 * 1024 ** 2 } }));
  result.media = result.tasks.map((task, index) => ({ id: `media-${index}`, taskId: task.id, title: task.title, filename: `video-${index}.mkv`, size: 5 * 1024 ** 2, duration: 60, mediaInfo: task.mediaInfo }));
  return result;
}
function RecordedResult() {
  const { state } = useApp(); const task = state.tasks['task-0']; const media = state.media['media-0'];
  return <output aria-label="Recorded result">{JSON.stringify({ status: task?.status, progress: task?.progress, size: task?.size, mediaSize: media?.size, error: task?.error })}</output>;
}
beforeEach(() => {
  snapshot = library(); available = 'missing'; transport.command.mockReset(); transport.result.mockReset().mockResolvedValue(undefined);
  transport.command.mockImplementation(async (command, args) => {
    if (command === 'snapshot') return snapshot;
    if (command === 'media.availability') return args.ids.map((id: string) => ({ id, status: available }));
    if (command === 'tasks.convertPreview') return structuredClone(preview);
    if (command === 'tasks.get' || command === 'tasks.info') return snapshot.tasks.find(task => task.id === args.id);
    return [];
  });
});

describe('availability in task and media views', () => {
  it('marks missing files without changing completion or size, blocks file operations, and recovers on focus', async () => {
    render(<AppProvider><App /><RecordedResult /></AppProvider>);
    const row = await screen.findByRole('option', { name: /Video 0/ });
    await waitFor(() => expect(row.textContent).toContain('Source file missing'));
    fireEvent.click(row);
    const detail = screen.getByRole('complementary', { name: 'Task details' });
    fireEvent.click(within(detail).getByText('More actions'));
    expect(within(detail).getAllByText('Completed').length).toBeGreaterThan(0);
    expect(within(detail).getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100');
    expect((within(detail).getByRole('button', { name: 'Reveal result file' }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(detail).getByRole('button', { name: 'Convert format' }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(detail).getByRole('button', { name: 'Compress video' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByLabelText('Recorded result').textContent).toBe(JSON.stringify({ status: 'completed', progress: 100, size: 5 * 1024 ** 2, mediaSize: 5 * 1024 ** 2, error: null }));
    fireEvent.contextMenu(row);
    expect((screen.getByRole('menuitem', { name: 'Compress video' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('menuitem', { name: 'Convert format' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    available = 'available';
    await act(async () => window.dispatchEvent(new Event('focus')));
    await waitFor(() => expect(row.textContent).not.toContain('Source file missing'));
    expect((within(detail).getByRole('button', { name: 'Reveal result file' }) as HTMLButtonElement).disabled).toBe(false);
    expect((within(detail).getByRole('button', { name: 'Compress video' }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(within(detail).getByRole('button', { name: 'Reveal result file' }));
    await waitFor(() => expect(transport.result).toHaveBeenCalledWith('media-0', 'reveal'));
  });

  it('opens compression from the completed result context menu', async () => {
    available = 'available';
    render(<AppProvider><App /></AppProvider>);
    const row = await screen.findByRole('option', { name: /Video 0/ });
    await waitFor(() => expect(transport.command.mock.calls.some(([command]) => command === 'media.availability')).toBe(true));
    fireEvent.contextMenu(row);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Compress video' }));
    expect(await screen.findByRole('dialog', { name: 'Compress video' })).toBeTruthy();
    await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tasks.convertPreview', { id: 'task-0', compression: {}, requestId: expect.any(String) }));
    expect(transport.command.mock.calls.some(([command]) => command === 'tasks.convert')).toBe(false);
  });

  it('checks only the rendered library window plus its offscreen selected result', async () => {
    snapshot = library(1000); available = 'available';
    render(<AppProvider><App /></AppProvider>);
    await screen.findByRole('listbox', { name: 'Download task list' });
    fireEvent.click(screen.getByLabelText('Completed', { selector: 'button' }));
    const list = await screen.findByRole('listbox', { name: 'Completed media list' });
    const visibleIds = () => within(list).getAllByRole('option').map(row => `media-${within(row).getByText(/^Video \d+$/).textContent!.split(' ')[1]}`);
    const checked = () => transport.command.mock.calls.filter(([command]) => command === 'media.availability').at(-1)?.[1].ids;
    await waitFor(() => expect(checked()).toEqual([...new Set([...visibleIds(), 'media-0'])].sort()));
    expect(checked().length).toBeLessThan(20);
    expect(checked()).not.toContain('media-500');
    const before = transport.command.mock.calls.filter(([command]) => command === 'media.availability').length;
    await act(async () => transport.listener?.({ kind: 'snapshot.required', data: null }));
    expect(transport.command.mock.calls.filter(([command]) => command === 'media.availability')).toHaveLength(before);
    Object.defineProperty(list, 'scrollTop', { writable: true, value: 5600 }); fireEvent.scroll(list);
    await waitFor(() => expect(checked()).toEqual([...new Set([...visibleIds(), 'media-0'])].sort()));
    expect(checked().length).toBeLessThan(24);
    expect(checked()).not.toContain('media-500');
  });
});
