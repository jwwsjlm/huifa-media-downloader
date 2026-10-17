import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { AppProvider } from './state';
import { makeSnapshot, makeTask } from './test-fixtures';
import type { CoreEvent, Snapshot } from './types';

const transport = vi.hoisted(() => ({ listener: null as null | ((event: CoreEvent) => void), command: vi.fn() }));
vi.mock('./transport', () => ({ host: {
  desktop: false, capabilities: () => ({ browserCookies: false, remoteBrowser: false, updates: false }),
  session: async () => ({ authenticated: true }), subscribe: async (listener: (event: CoreEvent) => void, connection: (value: boolean) => void) => { transport.listener = listener; connection(true); return () => { transport.listener = null; }; },
  command: transport.command, assetUrl: (id: string) => `/api/files/${id}`, notify: async () => {},
  releaseStatus: async () => ({ currentVersion: '0.4.1', latestVersion: '0.4.1', updateAvailable: false, url: 'https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.1' }),
} }));

beforeEach(() => transport.command.mockReset());
function install(snapshot: Snapshot) { transport.command.mockImplementation(async command => command === 'snapshot' ? snapshot : command === 'tools.check' ? [] : true); }

describe('task list multiple selection', () => {
  it('starts and reopens with no batch selection until the user selects tasks', async () => {
    install(makeSnapshot()); render(<AppProvider><App /></AppProvider>);
    let list = await screen.findByRole('listbox');
    expect(within(list).queryAllByRole('option', { selected: true })).toHaveLength(0);
    expect(screen.getByText('0 selected')).toBeTruthy();
    expect(screen.getByText('Select a task to see its plan, progress, and actions.')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Delete selected' }) as HTMLButtonElement).disabled).toBe(true);
    act(() => transport.listener?.({ kind: 'task.upsert', data: { ...makeTask('c', 'Gamma') } }));
    expect(within(list).queryAllByRole('option', { selected: true })).toHaveLength(0);
    fireEvent.click(within(list).getByRole('option', { name: /Alpha/ }));
    expect(screen.getByText('1 selected')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Navigation' })).getByRole('button', { name: 'Downloads' }));
    list = screen.getByRole('listbox');
    expect(within(list).queryAllByRole('option', { selected: true })).toHaveLength(0);
    expect(screen.getByText('0 selected')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Delete selected' }) as HTMLButtonElement).disabled).toBe(true);
  });
  it('selects a Shift range and scopes select all and deletion to the filtered list', async () => {
    const snapshot = makeSnapshot(); snapshot.tasks.push(makeTask('c', 'Gamma'), makeTask('d', 'Delta'));
    install(snapshot); render(<AppProvider><App /></AppProvider>); const list = await screen.findByRole('listbox');
    const rows = within(list); const chosen = () => rows.queryAllByRole('option', { selected: true });
    fireEvent.click(rows.getByRole('option', { name: /Alpha/ }));
    fireEvent.click(rows.getByRole('option', { name: /Gamma/ }), { shiftKey: true });
    expect(chosen()).toHaveLength(3); expect(screen.getByText('3 selected')).toBeTruthy();
    expect((screen.getByRole('checkbox', { name: 'Select all tasks in this list' }) as HTMLInputElement).indeterminate).toBe(true);
    act(() => transport.listener?.({ kind: 'task.upsert', data: { id: 'a', progress: 43 } }));
    expect(chosen()).toHaveLength(3);
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search tasks' }), { target: { value: 'Beta' } });
    expect(chosen()).toHaveLength(1); expect(screen.getByText('1 selected')).toBeTruthy();
    const all = screen.getByRole('checkbox', { name: 'Select all tasks in this list' });
    fireEvent.click(all); expect(chosen()).toHaveLength(0);
    expect((screen.getByRole('button', { name: 'Delete selected' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(all); expect(chosen()).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }));
    const dialog = await screen.findByRole('dialog', { name: 'Delete selected tasks' });
    expect(within(dialog).getByText('Delete 1 selected tasks? Active work stops first.')).toBeTruthy();
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.deleteBatch')).toBe(false);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    act(() => transport.listener?.({ kind: 'task.remove', data: { id: 'b' } }));
    await waitFor(() => expect(screen.getByText('0 selected')).toBeTruthy());
  });

  it('confirms bulk deletion, preserves failures, and retries only the failed tasks', async () => {
    const snapshot = makeSnapshot(); snapshot.tasks.push(makeTask('c', 'Gamma'));
    let attempt = 0;
    transport.command.mockImplementation(async (command, args) => {
      if (command === 'snapshot') return { ...snapshot, tasks: attempt === 0 ? snapshot.tasks : attempt === 1 ? [snapshot.tasks[1]] : [] };
      if (command === 'tools.check') return [];
      if (command === 'tasks.deleteBatch') {
        attempt += 1;
        return { results: args.ids.map((id: string) => ({ id, ok: attempt > 1 || id !== 'b', ...(attempt === 1 && id === 'b' ? { error: 'fixture media busy' } : {}) })) };
      }
      return true;
    });
    render(<AppProvider><App /></AppProvider>); await screen.findByRole('listbox');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all tasks in this list' }));
    expect(screen.getByText('3 selected')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }));
    const dialog = await screen.findByRole('dialog', { name: 'Delete selected tasks' });
    expect(transport.command.mock.calls.some(([name]) => name === 'tasks.deleteBatch')).toBe(false);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(within(dialog).getByRole('alert').textContent).toContain('Beta: fixture media busy'));
    expect(transport.command).toHaveBeenCalledWith('tasks.deleteBatch', { ids: ['a', 'b', 'c'], deleteFiles: false });
    expect(within(dialog).getByText('Delete 1 selected tasks? Active work stops first.')).toBeTruthy();
    await waitFor(() => expect((within(dialog).getByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(transport.command).toHaveBeenLastCalledWith('snapshot', { includeTools: false });
    expect(transport.command.mock.calls.filter(([name]) => name === 'tasks.deleteBatch')[1]).toEqual(['tasks.deleteBatch', { ids: ['b'], deleteFiles: false }]);
    await waitFor(() => expect(screen.getByText('0 selected')).toBeTruthy());
  });
});
