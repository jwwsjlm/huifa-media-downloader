import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AddDialog } from './AddDialog';

const command = vi.hoisted(() => vi.fn());
vi.mock('./transport', () => ({ host: { command } }));
beforeEach(() => { command.mockReset(); });

it('distinguishes parsing from adding tasks and reports failed additions', async () => {
  let finishProbe!: (value: unknown) => void; let failAdd!: (reason: Error) => void;
  command.mockImplementation(name => name === 'probe.collection' ? new Promise(resolve => { finishProbe = resolve; }) : new Promise((_resolve, reject) => { failAdd = reject; }));
  const options = { contentMode: 'video', quality: 'best', sourceVideoCodec: 'auto' };
  const added = vi.fn();
  render(<AddDialog urls={['https://example.invalid/video']} options={options} t={(_zh, en) => en} run={work => { void work(); }} close={vi.fn()} added={added} />);
  expect((screen.getByRole('button', { name: 'Parsing…' }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => finishProbe({ entries: [{ url: 'https://example.invalid/video', title: 'Demo video' }] }));
  fireEvent.click(screen.getByRole('button', { name: 'Download selected items' }));
  expect((screen.getByRole('button', { name: 'Adding to queue…' }) as HTMLButtonElement).disabled).toBe(true);
  expect(command).toHaveBeenCalledWith('tasks.add', { urls: ['https://example.invalid/video'], options: { ...options, cookieId: 'auto', fragmentConcurrent: 4, collectionMode: 'single' }, report: true });
  await act(async () => failAdd(new Error('Queue unavailable')));
  expect(added).toHaveBeenCalledWith(expect.objectContaining({ results: [], issues: [{ url: 'https://example.invalid/video', error: 'Error: Queue unavailable' }] }));
});

it('continues after a failed collection entry and returns one final report', async () => {
  const urls = ['https://example.invalid/one', 'https://example.invalid/two', 'https://example.invalid/three']; const added = vi.fn();
  command.mockImplementation(async (name, args) => {
    if (name === 'tasks.add' && args.urls[0] === urls[1]) throw new Error('Second failed');
    if (name === 'tasks.add') return { ids: [args.urls[0]], results: [{ id: args.urls[0], status: 'queued', action: 'added' }] };
    return true;
  });
  render(<AddDialog urls={urls} initialEntries={urls.map(url => ({ url, title: url }))} options={{ contentMode: 'video' }} t={(_zh, en) => en} run={work => { void work(); }} close={vi.fn()} added={added} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Download selected items' }));
  await waitFor(() => expect(added).toHaveBeenCalled());
  expect(added.mock.calls[0][0].results).toHaveLength(2);
  expect(added.mock.calls[0][0].issues).toEqual([{ url: urls[1], error: 'Error: Second failed' }]);
  expect(command.mock.calls.filter(([name]) => name === 'tasks.add')).toHaveLength(3);
});

it('cancels in-flight identification and closes without queueing a task', async () => {
  const close = vi.fn(); command.mockImplementation(async name => name === 'probe.collection' ? new Promise(() => {}) : true);
  render(<AddDialog urls={['https://example.invalid/video']} options={{ contentMode: 'video' }} t={(_zh, en) => en} run={work => { void work(); }} close={close} added={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(close).toHaveBeenCalled(); expect(command).toHaveBeenCalledWith('probe.cancel', { id: expect.any(String) });
  expect(command.mock.calls.some(([name]) => name === 'tasks.add')).toBe(false);
});

it('skips an unreadable manual format and still adds the next item with its selected format', async () => {
  const urls = ['https://example.invalid/bad', 'https://example.invalid/good']; const added = vi.fn();
  command.mockImplementation(async (name, args) => {
    if (name === 'probe.video') { if (args.url === urls[0]) throw new Error('No media'); return { title: 'Good', formats: [{ formatId: 'v', vcodec: 'h264', acodec: 'none' }] }; }
    if (name === 'tasks.add') return { ids: ['good'], results: [{ id: 'good', status: 'queued', action: 'added' }] };
    return true;
  });
  render(<AddDialog urls={urls} selectItems={false} options={{ contentMode: 'manual' }} t={(_zh, en) => en} run={work => { void work(); }} close={vi.fn()} added={added} />);
  const download = await screen.findByRole('button', { name: 'Download selected items' });
  await waitFor(() => expect((download as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(download);
  await waitFor(() => expect(added).toHaveBeenCalled());
  expect(added.mock.calls[0][0].issues).toEqual([{ url: urls[0], error: 'Error: No media' }]);
  expect(command).toHaveBeenCalledWith('tasks.add', { urls: [urls[1]], options: expect.objectContaining({ videoFormatId: 'v' }), report: true });
  expect(command.mock.calls.filter(([name]) => name === 'tasks.add')).toHaveLength(1);
});
