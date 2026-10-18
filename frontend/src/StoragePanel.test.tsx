import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { StoragePanel } from './StoragePanel';

const command = vi.hoisted(() => vi.fn());
vi.mock('./transport', () => ({ host: { command } }));
const storage = { dataFree: 591 * 1024 ** 3, downloadFree: 6 * 1024 ** 4, processingFree: 591 * 1024 ** 3, completedCacheBytes: 0, recoverableCacheBytes: 3 * 1024 ** 3, unusedToolGroups: 2 };
beforeEach(() => command.mockReset().mockResolvedValue(storage));
const t = (_zh: string, en: string) => en;

it('groups all volume and cache statistics and refreshes their values', async () => {
  render(<StoragePanel t={t} />);
  const panel = screen.getByRole('region', { name: 'Storage and cache' });
  await screen.findByText('6.0 TiB');
  expect(panel.getAttribute('aria-busy')).toBe('false');
  expect(within(panel).getAllByRole('term')).toHaveLength(5);
  expect(within(panel).getByText('0 B')).toBeTruthy();
  command.mockResolvedValue({ ...storage, downloadFree: 0 });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await waitFor(() => expect(within(panel).getAllByText('0 B')).toHaveLength(2));
  expect(command.mock.calls.map(([name]) => name)).toEqual(['storage.status', 'storage.status']);
});

it('keeps cleanup disabled while running and reports preserved recovery files', async () => {
  render(<StoragePanel t={t} />);
  const clean = await screen.findByRole('button', { name: 'Clean completed caches and unused runtimes (2)' });
  let finish!: (value: unknown) => void;
  command.mockReturnValue(new Promise(resolve => { finish = resolve; }));
  fireEvent.click(clean);
  expect((clean as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(true);
  expect(command).toHaveBeenLastCalledWith('storage.cleanup');
  finish({ storage: { ...storage, unusedToolGroups: 0 }, failedTaskIds: ['recoverable-task'] });
  expect((await screen.findByRole('alert')).textContent).toContain('recovery files were preserved');
  expect((screen.getByRole('button', { name: 'Clean completed caches and unused runtimes' }) as HTMLButtonElement).disabled).toBe(false);
});
