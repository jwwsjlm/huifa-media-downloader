import { act, fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { AddDialog } from './AddDialog';

const command = vi.hoisted(() => vi.fn());
vi.mock('./transport', () => ({ host: { command } }));

it('distinguishes parsing from adding tasks and retains the submitted plan after a failure', async () => {
  let finishProbe!: (value: unknown) => void; let failAdd!: (reason: Error) => void;
  command.mockImplementation(name => name === 'probe.collection' ? new Promise(resolve => { finishProbe = resolve; }) : new Promise((_resolve, reject) => { failAdd = reject; }));
  const options = { contentMode: 'video', quality: 'best', sourceVideoCodec: 'auto' };
  render(<AddDialog urls={['https://example.invalid/video']} options={options} t={(_zh, en) => en} run={work => { void work(); }} close={vi.fn()} added={vi.fn()} />);
  expect((screen.getByRole('button', { name: 'Parsing…' }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => finishProbe({ entries: [{ url: 'https://example.invalid/video', title: 'Demo video' }] }));
  fireEvent.click(screen.getByRole('button', { name: 'Download selected items' }));
  expect((screen.getByRole('button', { name: 'Adding to queue…' }) as HTMLButtonElement).disabled).toBe(true);
  expect(command).toHaveBeenCalledWith('tasks.add', { urls: ['https://example.invalid/video'], options: { ...options, cookieId: 'auto', fragmentConcurrent: 4, collectionMode: 'single' }, report: true });
  await act(async () => failAdd(new Error('Queue unavailable')));
  expect(screen.getByRole('alert').textContent).toContain('Queue unavailable');
  expect((screen.getByRole('button', { name: 'Download selected items' }) as HTMLButtonElement).disabled).toBe(false);
});
