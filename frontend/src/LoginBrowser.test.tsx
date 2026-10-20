import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import { LoginBrowser } from './LoginBrowser';

const command = vi.hoisted(() => vi.fn());
vi.mock('./transport', () => ({ host: { command } }));
const inactive = { active: false, mode: null, systemBrowser: 'edge' };
const active = { ...inactive, active: true, mode: 'embedded', targetUrl: 'https://www.youtube.com/' };
beforeEach(() => { command.mockReset().mockImplementation(async name => name === 'browser.login.open' ? active : inactive); });
const run = (operation: () => Promise<unknown>) => { void operation(); };
function show(proxy?: string) { const saved = vi.fn(); render(<LoginBrowser t={(_zh, en) => en} run={run} saved={saved} proxy={proxy} />); return { saved, controls: within(screen.getByRole('group', { name: 'Browser sign-in' })) }; }

it('keeps one slow status poll across StrictMode, retries after settlement and stops on unmount', async () => {
  vi.useFakeTimers();
  try {
    let finish!: (value: unknown) => void;
    command.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })).mockResolvedValue(inactive);
    const view = render(<StrictMode><LoginBrowser t={(_zh, en) => en} run={run} saved={vi.fn()} /></StrictMode>);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(command).toHaveBeenCalledTimes(1);
    await act(async () => finish(active));
    expect(screen.getByRole('button', { name: 'Close sign-in window' })).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(command).toHaveBeenCalledTimes(2);
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(command).toHaveBeenCalledTimes(2);
  } finally { vi.useRealTimers(); }
});

it('does not let an old poll reopen the UI or overwrite a close result, and skips polls during close', async () => {
  vi.useFakeTimers();
  try {
    let finishPoll!: (value: unknown) => void;
    let finishClose!: (value: unknown) => void;
    command.mockResolvedValueOnce(active)
      .mockImplementationOnce(() => new Promise(resolve => { finishPoll = resolve; }))
      .mockImplementationOnce(() => new Promise(resolve => { finishClose = resolve; }))
      .mockResolvedValue(inactive);
    show();
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    fireEvent.click(screen.getByRole('button', { name: 'Close sign-in window' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(command).toHaveBeenCalledTimes(3);
    await act(async () => finishClose(true));
    expect(command).toHaveBeenCalledTimes(4);
    expect(screen.queryByRole('button', { name: 'Close sign-in window' })).toBeNull();
    await act(async () => finishPoll(active));
    expect(screen.queryByRole('button', { name: 'Close sign-in window' })).toBeNull();
    expect((screen.getByRole('button', { name: 'Open built-in browser' }) as HTMLButtonElement).disabled).toBe(false);
  } finally { vi.useRealTimers(); }
});

it.each(['Open built-in browser', 'Local browser sign-in'])('passes the current download proxy to %s', async button => {
  const { controls } = show('http://127.0.0.1:7890');
  const open = controls.getByRole('button', { name: button });
  await waitFor(() => expect((open as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(open);
  await waitFor(() => expect(command).toHaveBeenCalledWith('browser.login.open', { url: 'https://www.youtube.com/', mode: button === 'Open built-in browser' ? 'embedded' : 'system', proxy: 'http://127.0.0.1:7890' }));
});

it.each([undefined, ''])('preserves an omitted saved proxy or explicitly clears it (%j)', async proxy => {
  const { controls } = show(proxy);
  const open = controls.getByRole('button', { name: 'Open built-in browser' });
  await waitFor(() => expect((open as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(open);
  await waitFor(() => expect(command).toHaveBeenCalledWith('browser.login.open', { url: 'https://www.youtube.com/', mode: 'embedded', ...(proxy === undefined ? {} : { proxy }) }));
});

it('opens the native browser directly without a second dialog and syncs after it closes', async () => {
  let status: object = inactive;
  const cookie = { id: 'new', name: 'Download login', count: 3, domains: ['youtube.com'] };
  command.mockImplementation(async name => name === 'browser.login.open' ? active : status);
  const { controls, saved } = show();
  expect(screen.queryByRole('dialog')).toBeNull();
  const open = controls.getByRole('button', { name: 'Open built-in browser' });
  await waitFor(() => expect((open as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(open);
  await waitFor(() => expect(command).toHaveBeenCalledWith('browser.login.open', { url: 'https://www.youtube.com/', mode: 'embedded' }));
  status = { ...inactive, cookie };
  expect(await controls.findByText('3 cookies saved automatically and matched to each download website.', {}, { timeout: 2500 })).toBeTruthy();
  expect(controls.queryByRole('combobox')).toBeNull();
  expect(command.mock.calls.some(([name]) => name === 'browser.login.save')).toBe(false);
  expect(saved).not.toHaveBeenCalled();
  expect(controls.queryByRole('button', { name: 'Close sign-in window' })).toBeNull();
});

it('restores the saved website and mode without polling over an unsubmitted address', async () => {
  command.mockResolvedValue({ ...inactive, preferredUrl: 'https://www.douyin.com/', preferredMode: 'system' });
  const { controls } = show();
  const input = controls.getByLabelText('Sign-in website URL') as HTMLInputElement;
  await waitFor(() => expect(input.value).toBe('https://www.douyin.com/'));
  const local = controls.getByRole('button', { name: 'Local browser sign-in' });
  expect(local.className).toContain('primary');
  fireEvent.change(input, { target: { value: 'https://www.bilibili.com/' } });
  vi.useFakeTimers();
  try { await act(async () => { vi.advanceTimersByTime(1500); }); } finally { vi.useRealTimers(); }
  expect(input.value).toBe('https://www.bilibili.com/');
  fireEvent.click(local);
  await waitFor(() => expect(command).toHaveBeenCalledWith('browser.login.open', { url: 'https://www.bilibili.com/', mode: 'system' }));
});

it('prefers the active browser address over the saved website', async () => {
  command.mockResolvedValue({ ...active, preferredUrl: 'https://www.douyin.com/', preferredMode: 'system' });
  const { controls } = show();
  await waitFor(() => expect((controls.getByLabelText('Sign-in website URL') as HTMLInputElement).value).toBe('https://www.youtube.com/'));
});

it('reports preference storage failures separately without disabling browser sign-in', async () => {
  command.mockResolvedValue({ ...inactive, preferenceError: 'Disk is full' });
  const { controls } = show();
  const alert = await controls.findByRole('alert');
  expect(alert.textContent).toContain('The last address and browser choice could not be saved: There is not enough disk space.');
  expect(alert.textContent).not.toContain('Disk is full');
  expect(alert.textContent).not.toContain('Cookie sync failed');
  expect((controls.getByRole('button', { name: 'Open built-in browser' }) as HTMLButtonElement).disabled).toBe(false);
});

it('keeps the login window and sync retry available after a failed export', async () => {
  command.mockResolvedValue(active);
  const { controls, saved } = show();
  const save = await controls.findByRole('button', { name: 'Sync now' });
  await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
  command.mockRejectedValueOnce(new Error('No cookies to save'));
  fireEvent.click(save);
  expect(await controls.findByRole('alert')).toBeTruthy();
  expect(saved).not.toHaveBeenCalled();
  expect((controls.getByRole('button', { name: 'Sync now' }) as HTMLButtonElement).disabled).toBe(false);
  expect((controls.getByRole('button', { name: 'Close sign-in window' }) as HTMLButtonElement).disabled).toBe(false);
});

it.each([
  [inactive, 'Reopen the login browser to retry syncing.'],
  [active, 'Choose Sync now to retry, or close the browser.'],
])('shows the available sync recovery and close actions (%j)', async (status, retryHint) => {
  command.mockResolvedValue({ ...status, saveError: 'fixture sync failure' });
  const { controls } = show();
  const alert = await controls.findByRole('alert');
  expect(alert.textContent).toContain(retryHint);
  expect(alert.textContent).toContain('Previously saved cookies are kept.');
  expect(alert.textContent).not.toContain('Keep the browser open');
  expect(Boolean(controls.queryByRole('button', { name: 'Sync now' }))).toBe(status.active);
  expect(Boolean(controls.queryByRole('button', { name: 'Close sign-in window' }))).toBe(status.active);
});

it('offers compatible sign-in and prevents duplicate opens while starting', async () => {
  const { controls } = show();
  const open = controls.getByRole('button', { name: 'Local browser sign-in' });
  await waitFor(() => expect((open as HTMLButtonElement).disabled).toBe(false));
  let resolve!: (value: unknown) => void;
  command.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  fireEvent.click(open);
  expect(command).toHaveBeenCalledWith('browser.login.open', { url: 'https://www.youtube.com/', mode: 'system' });
  expect((open as HTMLButtonElement).disabled).toBe(true);
  expect(controls.getByRole('button', { name: 'Opening…' })).toBe(open);
  expect((controls.getByRole('button', { name: 'Open built-in browser' }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => { resolve(active); });
  expect((open as HTMLButtonElement).disabled).toBe(true);
});

it('closes only the managed browser and keeps inline controls available', async () => {
  command.mockResolvedValue(active);
  const { controls } = show();
  const close = await controls.findByRole('button', { name: 'Close sign-in window' });
  command.mockResolvedValue(inactive);
  fireEvent.click(close);
  await waitFor(() => expect(command).toHaveBeenCalledWith('browser.login.close'));
  await waitFor(() => expect((controls.getByRole('button', { name: 'Open built-in browser' }) as HTMLButtonElement).disabled).toBe(false));
});

it('disables unavailable compatible mode', async () => {
  command.mockResolvedValue({ ...inactive, systemBrowser: null });
  const { controls } = show();
  await controls.findByText(/was not found/);
  expect((controls.getByRole('button', { name: 'Local browser sign-in' }) as HTMLButtonElement).disabled).toBe(true);
});

it('uses local browser sign-in when the platform does not offer an embedded browser', async () => {
  command.mockResolvedValue({ ...inactive, embeddedAvailable: false, preferredMode: 'embedded' });
  const { controls } = show('http://127.0.0.1:7890');
  await controls.findByText('Sign in with Chrome, Edge or Chromium; cookies save automatically.');
  expect(controls.queryByRole('button', { name: 'Open built-in browser' })).toBeNull();
  const local = controls.getByRole('button', { name: 'Local browser sign-in' });
  expect(local.className).toContain('primary');
  expect((local as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(local);
  await waitFor(() => expect(command).toHaveBeenCalledWith('browser.login.open', { url: 'https://www.youtube.com/', mode: 'system', proxy: 'http://127.0.0.1:7890' }));
});

it('requires a Chromium browser when the platform supports only local sign-in', async () => {
  command.mockResolvedValue({ ...inactive, embeddedAvailable: false, systemBrowser: null });
  const { controls } = show();
  await controls.findByText(/was not found/);
  expect(controls.queryByRole('button', { name: 'Open built-in browser' })).toBeNull();
  expect((controls.getByRole('button', { name: 'Local browser sign-in' }) as HTMLButtonElement).disabled).toBe(true);
});

it('does not ask users to name or select cookies or read saved cookie values', async () => {
  command.mockResolvedValue({ ...inactive, cookie: { id: 'saved', name: 'Hidden identity', count: 5 } });
  const { controls } = show();
  await controls.findByText('5 cookies saved automatically and matched to each download website.');
  expect(controls.queryByLabelText('Cookie name')).toBeNull();
  expect(controls.queryByRole('combobox')).toBeNull();
  expect(controls.queryByText('Hidden identity')).toBeNull();
  expect(controls.queryByRole('button', { name: 'View cookies' })).toBeNull();
  expect(command.mock.calls.some(([name]) => name === 'cookies.list' || name === 'cookies.read')).toBe(false);
});

it('retries automatic syncing with an internal name and reports the saved settings', async () => {
  const result = { cookie: { id: 'saved', count: 3 }, settings: { downloadOptions: { cookieId: 'auto' } } };
  command.mockImplementation(async name => name === 'browser.login.save' ? result : active);
  const { controls, saved } = show();
  fireEvent.click(await controls.findByRole('button', { name: 'Sync now' }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith(result));
  expect(command).toHaveBeenCalledWith('browser.login.save', { name: 'Download login' });
  expect(controls.queryByLabelText('Cookie name')).toBeNull();
});
