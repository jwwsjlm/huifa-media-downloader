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
