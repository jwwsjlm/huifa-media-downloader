import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { SettingsPage } from './SettingsPage';
import { initialState } from './state';

const transport = vi.hoisted(() => ({ desktop: false, command: vi.fn(), paths: vi.fn() }));
vi.mock('./transport', () => ({ host: { get desktop() { return transport.desktop; }, command: transport.command, paths: transport.paths, capabilities: () => ({ updates: false }) } }));
beforeEach(() => { transport.desktop = false; transport.command.mockReset().mockResolvedValue([]); transport.paths.mockReset().mockResolvedValue({ downloadDir: '/downloads' }); });
const run = (operation: () => Promise<unknown>) => { void operation(); };
function show() { render(<SettingsPage state={{ ...initialState, tools: [{ name: 'yt-dlp', available: true, version: '2026.08.19', platform: 'windows-x86_64', asset: 'yt-dlp.exe' }], view: { ...initialState.view, settingsTab: 'tools' } }} dispatch={vi.fn()} t={(_zh, en) => en} run={run} message={vi.fn()} />); }
it('explains Docker image updates instead of offering a forbidden tool command', async () => {
  show();
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('cookies.list'));
  expect(screen.queryByRole('button', { name: 'Update / install' })).toBeNull();
  expect(screen.getByText(/Update the Docker image/)).toBeTruthy();
  expect(screen.queryByLabelText('yt-dlp update channel')).toBeNull();
  expect(screen.queryByLabelText('yt-dlp download source')).toBeNull();
  expect(screen.getByRole('button', { name: 'Check yt-dlp updates' })).toBeTruthy();
});
it('keeps runtime tool installation available on desktop', async () => {
  transport.desktop = true; show();
  await waitFor(() => expect(transport.paths).toHaveBeenCalled());
  expect(screen.getByRole('button', { name: 'Update / install' })).toBeTruthy();
  expect(screen.getByText(/Current 2026.08.19/)).toBeTruthy();
  expect(screen.getByText(/Selected for this system: windows-x86_64/)).toBeTruthy();
});
it('sends the chosen official channel and exact tag and clears a tag when switching channels', async () => {
  transport.desktop = true; show();
  await waitFor(() => expect(transport.paths).toHaveBeenCalled());
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'nightly' } });
  fireEvent.change(screen.getByLabelText(/Specific version/), { target: { value: ' 2026.10.10.233141 ' } });
  fireEvent.click(screen.getByRole('button', { name: 'Update / install' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'yt-dlp', channel: 'nightly', version: '2026.10.10.233141', downloadSource: 'auto' }));
  await waitFor(() => expect((screen.getByLabelText('yt-dlp update channel') as HTMLSelectElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'stable' } });
  expect((screen.getByLabelText(/Specific version/) as HTMLInputElement).value).toBe('');
  fireEvent.click(screen.getByRole('button', { name: 'Update / install' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'yt-dlp', channel: 'stable', version: '', downloadSource: 'auto' }));
});
it('checks GitHub for the selected channel and shows the matching version and package', async () => {
  transport.desktop = true; show();
  transport.command.mockImplementation(async command => command === 'tools.latest' ? { name: 'yt-dlp', channel: 'nightly', current: '2026.08.19', latest: '2026.10.10.233141', updateAvailable: true, platform: 'windows-x86_64', asset: 'yt-dlp.exe' } : []);
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'nightly' } });
  fireEvent.click(screen.getByRole('button', { name: 'Check yt-dlp updates' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.latest', { name: 'yt-dlp', channel: 'nightly' }));
  expect((await screen.findByRole('status')).textContent).toContain('Latest on GitHub 2026.10.10.233141');
  expect(screen.getByRole('status').textContent).toContain('An update is available');
  expect(screen.getByRole('status').textContent).toContain('windows-x86_64 · yt-dlp.exe');
  expect(transport.command.mock.calls.some(([command]) => command === 'tools.update')).toBe(false);
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'stable' } });
  expect(screen.queryByRole('status')).toBeNull();
});
it('lets the user explicitly select CDN downloads', async () => {
  transport.desktop = true; show();
  fireEvent.change(screen.getByLabelText('yt-dlp download source'), { target: { value: 'cdn' } });
  fireEvent.click(screen.getByRole('button', { name: 'Update / install' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'yt-dlp', channel: 'stable', version: '', downloadSource: 'cdn' }));
});
it.each([
  ['2026.08.19', 'Up to date for this channel.'],
  ['2026.10.10.233141', 'The installed version is newer than this channel'],
])('shows the comparison for installed version %s without suggesting a newer release', async (current, hint) => {
  transport.desktop = true; show();
  transport.command.mockImplementation(async command => command === 'tools.latest' ? { name: 'yt-dlp', channel: 'stable', current, latest: '2026.08.19', updateAvailable: false, platform: 'windows-x86_64', asset: 'yt-dlp.exe' } : []);
  fireEvent.click(screen.getByRole('button', { name: 'Check yt-dlp updates' }));
  expect((await screen.findByRole('status')).textContent).toContain(hint);
  expect(screen.getByRole('status').textContent).not.toContain('An update is available');
});
