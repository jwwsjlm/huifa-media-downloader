import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { LoginBrowser } from './LoginBrowser';

const command = vi.hoisted(() => vi.fn());
vi.mock('./transport', () => ({ host: { command } }));
const inactive = { active: false, mode: null, systemBrowser: 'edge' };
const active = { ...inactive, active: true, mode: 'embedded', targetUrl: 'https://www.youtube.com/' };
beforeEach(() => { command.mockReset().mockImplementation(async name => name === 'cookies.list' ? [] : name === 'browser.login.open' ? active : inactive); });
const run = (operation: () => Promise<unknown>) => { void operation(); };
function show(cookieId = '') { const close = vi.fn(); const saved = vi.fn(); render(<LoginBrowser t={(_zh, en) => en} run={run} close={close} saved={saved} cookieId={cookieId} />); return { close, saved, dialog: within(screen.getByRole('dialog')) }; }

it('opens a native login window and shows automatic cookies after the window closes without a save click', async () => {
  let status: object = inactive;
  const cookie = { id: 'new', name: 'Download login', count: 3, domains: ['youtube.com'] };
  command.mockImplementation(async name => name === 'cookies.list' ? 'cookie' in status ? [cookie] : [] : name === 'browser.login.open' ? active : status);
  const { dialog, saved, close } = show();
  const open = dialog.getByRole('button', { name: 'Open built-in browser' });
  await waitFor(() => expect((open as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(open);
  await waitFor(() => expect(command).toHaveBeenCalledWith('browser.login.open', { url: 'https://www.youtube.com/', mode: 'embedded', name: 'Download login' }));
  status = { ...inactive, cookie };
  expect(await dialog.findByText('3 cookies saved automatically and matched to each download website.', {}, { timeout: 2500 })).toBeTruthy();
  expect(await dialog.findByRole('option', { name: 'Download login' })).toBeTruthy();
  expect(command.mock.calls.some(([name]) => name === 'browser.login.save')).toBe(false);
  expect(saved).not.toHaveBeenCalled();
  fireEvent.click(dialog.getByRole('button', { name: 'Done' }));
  await waitFor(() => expect(command).toHaveBeenCalledWith('browser.login.close'));
  expect(close).toHaveBeenCalledOnce();
});

it('keeps the login window and save action available after a failed export', async () => {
  command.mockImplementation(async name => name === 'cookies.list' ? [] : active);
  const { dialog, saved, close } = show();
  const save = await dialog.findByRole('button', { name: 'Sync now' });
  await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
  command.mockRejectedValueOnce(new Error('No cookies to save'));
  fireEvent.click(save);
  expect(await dialog.findByRole('alert')).toBeTruthy();
  expect(saved).not.toHaveBeenCalled(); expect(close).not.toHaveBeenCalled();
  expect((dialog.getByRole('button', { name: 'Sync now' }) as HTMLButtonElement).disabled).toBe(false);
});

it('offers compatible sign-in and prevents duplicate opens while starting', async () => {
  const { dialog } = show();
  const open = dialog.getByRole('button', { name: 'Local browser sign-in' });
  await waitFor(() => expect((open as HTMLButtonElement).disabled).toBe(false));
  let resolve!: (value: unknown) => void;
  command.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  fireEvent.click(open);
  expect(command).toHaveBeenCalledWith('browser.login.open', { url: 'https://www.youtube.com/', mode: 'system', name: 'Download login' });
  expect((open as HTMLButtonElement).disabled).toBe(true);
  expect(dialog.getByRole('button', { name: 'Opening…' })).toBe(open);
  expect((dialog.getByRole('button', { name: 'Done' }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => { resolve(active); });
  expect((open as HTMLButtonElement).disabled).toBe(true);
});

it('closes the managed browser on cancel and disables unavailable compatible mode', async () => {
  command.mockImplementation(async name => name === 'cookies.list' ? [] : { ...inactive, systemBrowser: null });
  const { dialog, close } = show();
  await dialog.findByText(/was not found/);
  expect((dialog.getByRole('button', { name: 'Local browser sign-in' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(dialog.getByRole('button', { name: 'Done' }));
  await waitFor(() => expect(command).toHaveBeenCalledWith('browser.login.close'));
  expect(close).toHaveBeenCalledOnce();
});

const entries = [{ id: 'old', name: 'Bilibili' }, { id: 'current', name: 'YouTube' }];
const cookieText = '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tlogin-value\n';

it('shows reading feedback only on the cookie action and keeps explanations collapsed', async () => {
  let finishRead!: (value: string) => void;
  command.mockImplementation(name => name === 'cookies.list' ? Promise.resolve(entries) : name === 'cookies.read' ? new Promise<string>(resolve => { finishRead = resolve; }) : Promise.resolve(active));
  const { dialog } = show('current');
  await dialog.findByLabelText('Select saved cookies');
  const disclosure = dialog.getByText('More information').closest('details')!;
  expect(disclosure.open).toBe(false);
  fireEvent.click(dialog.getByRole('button', { name: 'View cookies' }));
  expect((dialog.getByRole('button', { name: 'Reading…' }) as HTMLButtonElement).disabled).toBe(true);
  expect(dialog.getByRole('button', { name: 'Sync now' })).toBeTruthy();
  expect(dialog.queryByRole('button', { name: 'Saving…' })).toBeNull();
  await act(async () => finishRead(cookieText));
  expect((dialog.getByRole('button', { name: 'View cookies' }) as HTMLButtonElement).disabled).toBe(false);
});

it('selects the enabled identity and only reveals saved cookie values on request', async () => {
  command.mockImplementation(async name => name === 'cookies.list' ? entries : name === 'cookies.read' ? cookieText : inactive);
  const { dialog, saved, close } = show('current');
  const select = await dialog.findByLabelText('Select saved cookies') as HTMLSelectElement;
  expect(select.value).toBe('current');
  expect(dialog.getByRole('option', { name: 'YouTube' })).toBeTruthy();
  expect(dialog.queryByLabelText('Cookie contents (read-only)')).toBeNull();
  expect(command).not.toHaveBeenCalledWith('cookies.read', expect.anything());
  fireEvent.click(dialog.getByRole('button', { name: 'View cookies' }));
  const contents = await dialog.findByLabelText('Cookie contents (read-only)') as HTMLTextAreaElement;
  expect(command).toHaveBeenCalledWith('cookies.read', { id: 'current' });
  expect(contents.value).toBe(cookieText);
  expect(contents.readOnly).toBe(true);
  expect(saved).not.toHaveBeenCalled(); expect(close).not.toHaveBeenCalled();
  expect(command.mock.calls.some(([name]) => name === 'settings.save' || name === 'browser.login.open')).toBe(false);
  fireEvent.click(dialog.getByRole('button', { name: 'Hide cookies' }));
  expect(dialog.queryByLabelText('Cookie contents (read-only)')).toBeNull();
});

it('clears displayed values on selection changes and recovers after a read failure', async () => {
  command.mockImplementation(async name => name === 'cookies.list' ? entries : name === 'cookies.read' ? cookieText : inactive);
  const { dialog, saved } = show('current');
  await dialog.findByLabelText('Select saved cookies');
  const read = dialog.getByRole('button', { name: 'View cookies' });
  fireEvent.click(read);
  await dialog.findByLabelText('Cookie contents (read-only)');
  await waitFor(() => expect((read as HTMLButtonElement).disabled).toBe(false));
  fireEvent.change(dialog.getByLabelText('Select saved cookies'), { target: { value: 'old' } });
  expect(dialog.queryByLabelText('Cookie contents (read-only)')).toBeNull();
  command.mockRejectedValueOnce(new Error('Credential no longer exists'));
  fireEvent.click(read);
  expect((await dialog.findByRole('alert')).textContent).toContain('Credential no longer exists');
  expect(dialog.queryByLabelText('Cookie contents (read-only)')).toBeNull();
  expect((read as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(read);
  await dialog.findByLabelText('Cookie contents (read-only)');
  expect(command).toHaveBeenLastCalledWith('cookies.read', { id: 'old' });
  expect(saved).not.toHaveBeenCalled();
});

it('refreshes saved cookies and removes content belonging to a deleted identity', async () => {
  let stored = entries;
  command.mockImplementation(async name => name === 'cookies.list' ? stored : name === 'cookies.read' ? cookieText : inactive);
  const { dialog } = show('current');
  await dialog.findByLabelText('Select saved cookies');
  fireEvent.click(dialog.getByRole('button', { name: 'View cookies' }));
  await dialog.findByLabelText('Cookie contents (read-only)');
  const refresh = dialog.getByRole('button', { name: 'Refresh saved cookies' });
  await waitFor(() => expect((refresh as HTMLButtonElement).disabled).toBe(false));
  stored = [entries[0]];
  fireEvent.click(refresh);
  await waitFor(() => expect((dialog.getByLabelText('Select saved cookies') as HTMLSelectElement).value).toBe('old'));
  expect(dialog.queryByLabelText('Cookie contents (read-only)')).toBeNull();
  expect(dialog.queryByRole('option', { name: 'YouTube' })).toBeNull();
  await waitFor(() => expect((refresh as HTMLButtonElement).disabled).toBe(false));
  stored = [];
  fireEvent.click(refresh);
  expect(await dialog.findByText(/No saved cookies yet/)).toBeTruthy();
  expect(dialog.queryByRole('button', { name: 'View cookies' })).toBeNull();
});
