import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { AppProvider } from './state';
import { makeSnapshot } from './test-fixtures';
import type { CoreEvent } from './types';

const transport = vi.hoisted(() => ({ listener: null as null | ((event: CoreEvent) => void), command: vi.fn() }));
vi.mock('./transport', () => ({ host: {
  desktop: false, capabilities: () => ({ browserCookies: false, remoteBrowser: false, updates: false }),
  session: async () => ({ authenticated: true }), subscribe: async (listener: (event: CoreEvent) => void, connection: (value: boolean) => void) => { transport.listener = listener; connection(true); return () => { transport.listener = null; }; },
  command: transport.command, assetUrl: (id: string) => `/api/files/${id}`, notify: async () => {},
} }));
beforeEach(() => { transport.command.mockReset(); transport.command.mockImplementation(async command => command === 'snapshot' ? makeSnapshot() : command === 'cookies.list' ? [] : true); });
describe('live interface stability', () => {
  it.each(['best', 'worst'])('adds a batch with %s quality without opening the manual format picker', async quality => {
    const snapshot = makeSnapshot();
    snapshot.settings.downloadOptions = { contentMode: 'manual', quality: 'best', formatId: 'old', videoFormatId: 'old-video', audioFormatIds: ['old-audio'] };
    const entries = [{ url: 'https://example.test/first', title: 'First video' }, { url: 'https://example.test/second', title: 'Second video' }];
    transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'probe.collection' ? { entries } : true);
    render(<AppProvider><App /></AppProvider>);
    const selector = await screen.findByRole('combobox', { name: 'Quality' }) as HTMLSelectElement;
    expect(selector.value).toBe('manual');
    fireEvent.change(selector, { target: { value: quality } });
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', progress: 60 } }));
    expect(selector.value).toBe(quality);
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: 'https://example.test/collection' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add downloads' }));
    const confirm = await screen.findByRole('button', { name: 'Download selected items' });
    await waitFor(() => expect(confirm.hasAttribute('disabled')).toBe(false));
    fireEvent.click(confirm);
    await waitFor(() => expect(transport.command.mock.calls.filter(([command]) => command === 'tasks.add')).toHaveLength(2));
    for (const entry of entries) expect(transport.command).toHaveBeenCalledWith('tasks.add', { urls: [entry.url], options: expect.objectContaining({ quality, contentMode: 'video', collectionMode: 'single', formatId: '', videoFormatId: '', audioFormatIds: [] }) });
    expect(transport.command.mock.calls.some(([command]) => command === 'probe.video')).toBe(false);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(selector.value).toBe(quality);
  });
  it('saves automatic quality from quick options and keeps audio-only settings', async () => {
    const snapshot = makeSnapshot();
    snapshot.settings.downloadOptions = { contentMode: 'audio', quality: 'best', formatId: 'old', videoFormatId: 'old-video', audioFormatIds: ['old-audio'], audioFormat: 'mp3' };
    transport.command.mockImplementation(async (command, args) => command === 'snapshot' ? snapshot : command === 'settings.save' ? args.settings : true);
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('button', { name: 'Quick options' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Quality selection' }), { target: { value: 'worst' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save batch plan' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(transport.command).toHaveBeenCalledWith('settings.save', { settings: expect.objectContaining({ downloadOptions: expect.objectContaining({ quality: 'worst', contentMode: 'audio', audioFormat: 'mp3', formatId: '', videoFormatId: '', audioFormatIds: [] }) }) });
    expect((screen.getByRole('combobox', { name: 'Quality' }) as HTMLSelectElement).value).toBe('worst');
  });
  it('gives recovery guidance for failed downloads while keeping the diagnostic', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'failed', error: 'yt-dlp.exe exited with code 1: Sign in to confirm you are not a bot' } }));
    expect(screen.getByRole('alert').textContent).toContain('Import or read signed-in cookies');
    expect(screen.getByRole('alert').textContent).toContain('yt-dlp.exe exited with code 1');
  });
  it('keeps URL drafts, caret, filter, selected hidden task and virtual viewport during progress updates', async () => {
    render(<AppProvider><App /></AppProvider>);
    await screen.findByRole('listbox', { name: 'Download task list' });
    const draft = screen.getByRole('textbox', { name: 'Download links' }) as HTMLTextAreaElement;
    fireEvent.change(draft, { target: { value: 'https://example.test/not-submitted' } }); draft.focus(); draft.setSelectionRange(10, 18);
    const search = screen.getByRole('searchbox', { name: 'Search tasks' }); fireEvent.change(search, { target: { value: 'Beta' } });
    fireEvent.change(search, { target: { value: '' } }); fireEvent.click(screen.getByRole('option', { name: /Alpha/ }));
    fireEvent.change(search, { target: { value: 'Beta' } }); draft.focus(); draft.setSelectionRange(10, 18);
    const viewport = screen.getByRole('listbox', { name: 'Download task list' });
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', progress: 77, stageProgress: 77 } }));
    expect(draft.value).toBe('https://example.test/not-submitted'); expect(document.activeElement).toBe(draft); expect(draft.selectionStart).toBe(10); expect(draft.selectionEnd).toBe(18);
    expect((search as HTMLInputElement).value).toBe('Beta'); expect(screen.getByText('Selected task is hidden by the current filter.')).toBeTruthy(); expect(screen.getByRole('listbox')).toBe(viewport);
  });
  it('keeps unsaved settings input identity and text when progress and settings events arrive', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    await waitFor(() => expect(screen.getByLabelText(/Filename template/)).toBeTruthy());
    const input = screen.getByLabelText(/Filename template/) as HTMLInputElement; fireEvent.change(input, { target: { value: '%(title)s-custom.%(ext)s' } }); input.focus();
    act(() => { transport.listener?.({ kind: 'task.upsert', data: { id: 'b', progress: 44, stageProgress: 44 } }); transport.listener?.({ kind: 'settings.updated', data: { uiLanguage: 'en-US', downloadOptions: { filenameTemplate: 'remote-template' } } }); });
    expect(screen.getByLabelText(/Filename template/)).toBe(input); expect(input.value).toBe('%(title)s-custom.%(ext)s'); expect(document.activeElement).toBe(input); expect(screen.getByText('Unsaved changes')).toBeTruthy();
  });
  it('refreshes a missed snapshot while preserving the selected task and URL draft', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.change(screen.getByRole('textbox', { name: 'Download links' }), { target: { value: 'still editing' } });
    const next = makeSnapshot(); next.tasks[0].progress = 92; next.tasks[0].stageProgress = 92; next.tasks.reverse();
    transport.command.mockImplementation(async command => command === 'snapshot' ? next : []);
    act(() => transport.listener?.({ kind: 'snapshot.required', data: null }));
    await waitFor(() => expect(screen.getAllByText('92.0%').length).toBeGreaterThan(0));
    expect((screen.getByRole('textbox', { name: 'Download links' }) as HTMLTextAreaElement).value).toBe('still editing');
    expect(within(screen.getByRole('listbox')).getByRole('option', { selected: true }).textContent).toContain('Alpha');
  });
  it('updates filtered and sorted rows when metadata changes after progress-only events', async () => {
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    const rows = () => within(screen.getByRole('listbox')).getAllByRole('option');
    fireEvent.change(screen.getByRole('combobox', { name: 'Sort' }), { target: { value: 'title' } });
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'b', progress: 45 } }));
    expect(rows()[0].textContent).toContain('Alpha');
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'b', title: 'Aardvark' } }));
    expect(rows()[0].textContent).toContain('Aardvark');
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search tasks' }), { target: { value: 'needle' } });
    expect(screen.getByText('No matching tasks')).toBeTruthy();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', url: 'https://example.test/needle' } }));
    expect(rows()[0].textContent).toContain('Alpha');
    fireEvent.change(screen.getByRole('combobox', { name: 'Status filter' }), { target: { value: 'paused' } });
    expect(screen.getByText('No matching tasks')).toBeTruthy();
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', status: 'paused' } }));
    expect(rows()[0].textContent).toContain('Alpha');
    expect(screen.getByText('2 tasks · 1 active')).toBeTruthy();
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search tasks' }), { target: { value: '' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'Status filter' }), { target: { value: 'all' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'Sort' }), { target: { value: 'newest' } });
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'b', createdAt: '2030-01-01T00:00:00Z' } }));
    expect(rows()[0].textContent).toContain('Aardvark');
  });
});
