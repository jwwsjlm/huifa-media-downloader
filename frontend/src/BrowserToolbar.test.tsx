import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { BrowserToolbar } from './BrowserToolbar';

const invoke = vi.hoisted(() => vi.fn());
vi.mock('@tauri-apps/api/core', () => ({ invoke }));
const initial = { url: 'https://example.com/', canGoBack: true, canGoForward: false };
beforeEach(() => {
  vi.spyOn(navigator, 'language', 'get').mockReturnValue('en-US');
  invoke.mockReset().mockResolvedValue(initial);
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

it('uses the dedicated command for navigation, history, and refresh', async () => {
  render(<BrowserToolbar />);
  const address = await screen.findByDisplayValue(initial.url);
  expect((screen.getByRole('button', { name: 'Forward' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'Back' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('login_navigation', { action: 'back' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(false));
  invoke.mockResolvedValue({ ...initial, canGoForward: true });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('login_navigation', { action: 'reload' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Forward' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Forward' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('login_navigation', { action: 'forward' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.change(address, { target: { value: '  www.example.org/video  ' } });
  fireEvent.submit(address.closest('form')!);
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('login_navigation', { action: 'navigate', url: 'www.example.org/video' }));
  expect(invoke.mock.calls.every(([command]) => command === 'login_navigation')).toBe(true);
});

it('updates the current URL without replacing an address being edited and supports Ctrl+L', async () => {
  vi.useFakeTimers();
  render(<BrowserToolbar />);
  await act(async () => {});
  const address = screen.getByRole('textbox', { name: 'Website address' }) as HTMLInputElement;
  fireEvent.focus(address);
  fireEvent.change(address, { target: { value: 'https://draft.example/' } });
  invoke.mockResolvedValue({ ...initial, url: 'https://redirected.example/' });
  await act(async () => { await vi.advanceTimersByTimeAsync(750); });
  expect(address.value).toBe('https://draft.example/');
  fireEvent.blur(address);
  await act(async () => { await vi.advanceTimersByTimeAsync(750); });
  expect(address.value).toBe('https://redirected.example/');
  fireEvent.keyDown(window, { key: 'l', ctrlKey: true });
  expect(document.activeElement).toBe(address);
  expect(address.selectionStart).toBe(0);
  expect(address.selectionEnd).toBe(address.value.length);
});

it('shows navigation failures and permits retry without losing the typed address', async () => {
  render(<BrowserToolbar />);
  const address = await screen.findByDisplayValue(initial.url) as HTMLInputElement;
  invoke.mockRejectedValueOnce(new Error('Only HTTP and HTTPS addresses are allowed'));
  fireEvent.change(address, { target: { value: 'file:///private' } });
  fireEvent.submit(address.closest('form')!);
  expect((await screen.findByRole('alert')).textContent).toBe('Only HTTP and HTTPS addresses are allowed');
  expect(address.value).toBe('file:///private');
  fireEvent.change(address, { target: { value: 'https://retry.example/' } });
  invoke.mockResolvedValue({ ...initial, url: 'https://retry.example/' });
  fireEvent.submit(address.closest('form')!);
  await waitFor(() => expect(address.value).toBe('https://retry.example/'));
  expect(screen.queryByRole('alert')).toBeNull();
});

it('does not overlap state requests and stops polling when unmounted', async () => {
  vi.useFakeTimers();
  let resolve!: (value: typeof initial) => void;
  invoke.mockImplementation(() => new Promise(done => { resolve = done; }));
  const { unmount } = render(<BrowserToolbar />);
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(invoke).toHaveBeenCalledTimes(1);
  unmount();
  await act(async () => { resolve(initial); await vi.advanceTimersByTimeAsync(3000); });
  expect(invoke).toHaveBeenCalledTimes(1);
});
