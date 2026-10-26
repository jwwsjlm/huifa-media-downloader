import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AppUpdateDialog, AppUpdatePanel, useAppUpdate } from './AppUpdate';
import type { UpdateStatus } from './types';

const native = vi.hoisted(() => ({ listener: null as null | ((status: UpdateStatus) => void), status: vi.fn(), check: vi.fn(), download: vi.fn(), install: vi.fn(), unsubscribe: vi.fn() }));
vi.mock('./transport', () => ({ host: {
  updateStatus: native.status, checkUpdate: native.check, downloadUpdate: native.download, installUpdate: native.install,
  subscribeUpdate: async (listener: (status: UpdateStatus) => void) => { native.listener = listener; return native.unsubscribe; },
} }));
const idle: UpdateStatus = { revision: 0, phase: 'idle', downloadedBytes: 0, canDownload: false, canInstall: false, restartRequired: false };
const available: UpdateStatus = { ...idle, revision: 2, phase: 'available', version: '0.6.0', body: 'Release details', canDownload: true };
const ready: UpdateStatus = { ...available, revision: 6, phase: 'ready', downloadedBytes: 4096, totalBytes: 4096, canDownload: false, canInstall: true };
const t = (_zh: string, en: string) => en;
function Interface() {
  const update = useAppUpdate(true); const [page, setPage] = useState(true);
  return <><button onClick={() => setPage(value => !value)}>Switch page</button>{page && <AppUpdatePanel update={update} t={t} />}<AppUpdateDialog update={update} t={t} /></>;
}
beforeEach(() => {
  native.listener = null; native.unsubscribe.mockReset();
  native.status.mockReset().mockResolvedValue(idle); native.check.mockReset().mockResolvedValue(available);
  native.download.mockReset().mockResolvedValue(ready); native.install.mockReset().mockResolvedValue(undefined);
});

it('checks once, downloads across page changes and waits for signature verification before offering installation', async () => {
  let finish!: (status: UpdateStatus) => void;
  native.download.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  render(<Interface />);
  const download = await screen.findByRole('button', { name: 'Download update' });
  expect(native.check).toHaveBeenCalledOnce(); expect(native.download).not.toHaveBeenCalled();
  fireEvent.click(download); fireEvent.click(download);
  expect(native.download).toHaveBeenCalledOnce();
  act(() => native.listener?.({ ...available, revision: 3, phase: 'downloading', canDownload: false, downloadedBytes: 1024 }));
  expect(screen.getByRole('progressbar').hasAttribute('value')).toBe(false);
  expect(screen.queryByRole('button', { name: 'Restart and install' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Switch page' }));
  act(() => native.listener?.({ ...available, revision: 4, phase: 'downloading', canDownload: false, downloadedBytes: 2048, totalBytes: 4096 }));
  fireEvent.click(screen.getByRole('button', { name: 'Switch page' }));
  expect((screen.getByRole('progressbar') as HTMLProgressElement).value).toBe(50);
  act(() => native.listener?.({ ...available, revision: 5, phase: 'verifying', canDownload: false, downloadedBytes: 4096, totalBytes: 4096 }));
  expect(screen.getByText(/Verifying signature/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Restart and install' })).toBeNull();
  await act(async () => finish(ready));
  expect(screen.getByRole('button', { name: 'Restart and install' })).toBeTruthy();
  expect(native.install).not.toHaveBeenCalled(); expect(native.check).toHaveBeenCalledOnce();
});

it('restores a verified cache, allows later installation and only installs after confirmation', async () => {
  native.check.mockResolvedValue(ready);
  render(<Interface />);
  fireEvent.click(await screen.findByRole('button', { name: 'Restart and install' }));
  expect(screen.getByRole('dialog').textContent).toContain('Install version 0.6.0?');
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Later' }));
  expect(native.install).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Restart and install' }));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Restart and install' }));
  await waitFor(() => expect(native.install).toHaveBeenCalledOnce());
  expect(native.download).not.toHaveBeenCalled();
});

it('shows check and download failures and retries without claiming to be up to date', async () => {
  native.check.mockResolvedValueOnce({ ...idle, revision: 2, phase: 'error', error: 'HTTP 429: retry later' });
  native.download.mockResolvedValueOnce({ ...available, revision: 4, phase: 'error', error: 'Connection interrupted' });
  render(<Interface />);
  expect((await screen.findByRole('alert')).textContent).toContain('HTTP 429');
  expect(screen.queryByText('You have the latest stable release.')).toBeNull();
  native.check.mockResolvedValue({ ...available, revision: 3 });
  fireEvent.click(screen.getByRole('button', { name: 'Check application update' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Download update' }));
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Connection interrupted'));
  fireEvent.click(screen.getByRole('button', { name: 'Download update' }));
  expect(await screen.findByRole('button', { name: 'Restart and install' })).toBeTruthy();
  expect(native.download).toHaveBeenCalledTimes(2);
});

it('ignores stale progress and releases the update subscription on unmount', async () => {
  const view = render(<Interface />); await screen.findByRole('button', { name: 'Download update' });
  act(() => { native.listener?.(ready); native.listener?.({ ...available, revision: 3, phase: 'downloading', canDownload: false }); });
  expect(screen.getByRole('button', { name: 'Restart and install' })).toBeTruthy();
  expect(screen.queryByRole('progressbar')).toBeNull();
  view.unmount(); expect(native.unsubscribe).toHaveBeenCalledOnce();
});

it('keeps the shutdown failure visible and blocks continuing through the installation dialog', async () => {
  native.check.mockResolvedValue(ready); render(<Interface />);
  await screen.findByRole('button', { name: 'Restart and install' });
  act(() => native.listener?.({ ...ready, revision: 8, phase: 'error', canInstall: false, restartRequired: true, error: 'Configuration backup failed' }));
  expect(screen.getByRole('dialog').textContent).toContain('Close and reopen the application');
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close dialog' }));
  expect(screen.getByRole('dialog')).toBeTruthy(); expect(native.install).not.toHaveBeenCalled();
});
