import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { SettingsPage } from './SettingsPage';
import { initialState } from './state';

const transport = vi.hoisted(() => ({ desktop: false, command: vi.fn(), paths: vi.fn() }));
vi.mock('./transport', () => ({ host: { get desktop() { return transport.desktop; }, command: transport.command, paths: transport.paths, capabilities: () => ({ updates: false }) } }));
beforeEach(() => { transport.desktop = false; transport.command.mockResolvedValue([]); transport.paths.mockResolvedValue({ downloadDir: '/downloads' }); });
const run = (operation: () => Promise<unknown>) => { void operation(); };
function show() { render(<SettingsPage state={{ ...initialState, tools: [{ name: 'yt-dlp', available: true }], view: { ...initialState.view, settingsTab: 'tools' } }} dispatch={vi.fn()} t={(_zh, en) => en} run={run} message={vi.fn()} />); }
it('explains Docker image updates instead of offering a forbidden tool command', async () => {
  show();
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('cookies.list'));
  expect(screen.queryByRole('button', { name: 'Update / install' })).toBeNull();
  expect(screen.getByText(/Update the Docker image/)).toBeTruthy();
  expect(screen.queryByLabelText('yt-dlp update channel')).toBeNull();
});
it('keeps runtime tool installation available on desktop', async () => {
  transport.desktop = true; show();
  await waitFor(() => expect(transport.paths).toHaveBeenCalled());
  expect(screen.getByRole('button', { name: 'Update / install' })).toBeTruthy();
});
it('sends the chosen official channel and exact tag and clears a tag when switching channels', async () => {
  transport.desktop = true; show();
  await waitFor(() => expect(transport.paths).toHaveBeenCalled());
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'nightly' } });
  fireEvent.change(screen.getByLabelText(/Specific version/), { target: { value: ' 2026.10.10.233141 ' } });
  fireEvent.click(screen.getByRole('button', { name: 'Update / install' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'yt-dlp', channel: 'nightly', version: '2026.10.10.233141' }));
  await waitFor(() => expect((screen.getByLabelText('yt-dlp update channel') as HTMLSelectElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'stable' } });
  expect((screen.getByLabelText(/Specific version/) as HTMLInputElement).value).toBe('');
  fireEvent.click(screen.getByRole('button', { name: 'Update / install' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'yt-dlp', channel: 'stable', version: '' }));
});
