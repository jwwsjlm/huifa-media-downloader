import { act, fireEvent, render, screen } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { RuntimeUpdatePanel, useRuntimeUpdates } from './RuntimeUpdates';
import type { Tool, ToolRelease } from './types';

const command = vi.hoisted(() => vi.fn());
vi.mock('./transport', () => ({ host: { command } }));
const tools: Tool[] = [{ name: 'yt-dlp', available: true, version: '2026.08.19' }, { name: 'qjs', available: true, version: '0.17.0' }, { name: 'ffmpeg', available: true, version: '7.1' }];
const release = (name: string): ToolRelease => ({ name, current: tools.find(tool => tool.name === name)!.version!, latest: name === 'yt-dlp' ? '2026.10.26' : 'v0.18.0', updateAvailable: true, channel: 'stable', platform: 'windows-x86_64', asset: `${name}.exe` });
function Fixture({ enabled = true, mode = 'compatibility', installed = tools }: { enabled?: boolean; mode?: string; installed?: Tool[] }) {
  const update = useRuntimeUpdates(enabled, installed, mode);
  return <RuntimeUpdatePanel update={update} disabled={false} t={(_zh, en) => en} />;
}
beforeEach(() => { vi.useFakeTimers(); command.mockReset().mockImplementation(async (name, args) => name === 'tools.latest' ? release(args.name) : tools); });
afterEach(() => vi.useRealTimers());
const startup = () => act(async () => { await vi.advanceTimersByTimeAsync(3000); });

it('checks once after startup, keeps compatibility FFmpeg pinned and installs only on click', async () => {
  const view = render(<StrictMode><Fixture /></StrictMode>);
  expect(command).not.toHaveBeenCalled();
  await startup();
  expect(command.mock.calls.map(([, args]) => args.name)).toEqual(['yt-dlp', 'qjs']);
  expect(command.mock.calls.every(([name]) => name === 'tools.latest')).toBe(true);
  expect(screen.getByText('2026.08.19 → 2026.10.26')).toBeTruthy();
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Install qjs v0.18.0' })));
  expect(command).toHaveBeenCalledWith('tools.update', { name: 'qjs', channel: 'stable', version: 'v0.18.0' });
  expect(screen.queryByRole('button', { name: 'Install qjs v0.18.0' })).toBeNull();
  view.rerender(<StrictMode><Fixture installed={[...tools]} /></StrictMode>);
  await startup();
  expect(command.mock.calls.filter(([name]) => name === 'tools.latest')).toHaveLength(2);
});

it('checks efficiency FFmpeg and does not let a failed check hide other updates', async () => {
  command.mockImplementation(async (_name, args) => { if (args.name === 'qjs') throw new Error('HTTP 429'); return release(args.name); });
  render(<Fixture mode="efficiency" />); await startup();
  expect(command).toHaveBeenCalledWith('tools.latest', { name: 'ffmpeg', mode: 'efficiency' }, expect.any(Object));
  expect(screen.getByRole('button', { name: 'Install yt-dlp 2026.10.26' })).toBeTruthy();
  expect(screen.getByRole('alert')).toBeTruthy();
  expect(screen.queryByText('No updates are available for the checked components.')).toBeNull();
});

it('retains a failed installation for retry and prevents duplicate installs', async () => {
  render(<Fixture />); await startup();
  let fail!: (reason: Error) => void;
  command.mockImplementation(() => new Promise((_resolve, reject) => { fail = reject; }));
  fireEvent.click(screen.getByRole('button', { name: 'Install yt-dlp 2026.10.26' }));
  expect((screen.getByRole('button', { name: 'Installing…' }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => fail(new Error('checksum mismatch')));
  expect(screen.getByRole('alert')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Install yt-dlp 2026.10.26' })).toBeTruthy();
});

it('does not check the web host and ignores results after unmount', async () => {
  const web = render(<Fixture enabled={false} />); await startup();
  expect(command).not.toHaveBeenCalled(); web.unmount();
  let finish!: (value: ToolRelease) => void;
  command.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const desktop = render(<Fixture installed={[tools[0]]} />); await startup();
  const signal = command.mock.calls[0][2].signal as AbortSignal;
  desktop.unmount(); expect(signal.aborted).toBe(true);
  await act(async () => finish(release('yt-dlp')));
});

it('hides a stale update after tools change without rechecking on progress renders', async () => {
  const view = render(<Fixture />); await startup();
  view.rerender(<Fixture installed={tools.map(tool => tool.name === 'yt-dlp' ? { ...tool, version: '2026.11.01' } : tool)} />);
  expect(screen.queryByRole('button', { name: 'Install yt-dlp 2026.10.26' })).toBeNull();
  await startup(); expect(command).toHaveBeenCalledTimes(2);
});
