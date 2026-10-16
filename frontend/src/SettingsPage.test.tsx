import { render, screen, waitFor } from '@testing-library/react';
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
});
it('keeps runtime tool installation available on desktop', async () => {
  transport.desktop = true; show();
  await waitFor(() => expect(transport.paths).toHaveBeenCalled());
  expect(screen.getByRole('button', { name: 'Update / install' })).toBeTruthy();
});
