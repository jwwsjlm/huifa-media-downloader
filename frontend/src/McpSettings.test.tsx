import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { McpSettings } from './McpSettings';
import { capabilityCoverage, mcpGroups } from './mcpTools';

const api = vi.hoisted(() => ({ desktop: true, command: vi.fn(), mcpStatus: vi.fn(), mcpConfigure: vi.fn() }));
vi.mock('./transport', () => ({ host: api }));
const writeTools = new Set(['start_download', 'start_batch_download', 'start_collection_download', 'download_assets', 'control_download', 'convert_media', 'retry_processing']);
const tools = mcpGroups.flatMap(group => group.tools.map(tool => ({ name: tool.name, description: tool.description[1], inputSchema: { type: 'object', properties: { url: { type: 'string' } }, required: ['url'] }, annotations: { readOnlyHint: !writeTools.has(tool.name) } })));
const connection = () => ({ url: 'http://127.0.0.1:19492/mcp', http: { mcpServers: { huifa: { headers: { Authorization: 'Bearer <YOUR_PASSWORD>' } } } }, tools });
beforeEach(() => {
  api.desktop = true;
  api.command.mockReset().mockImplementation(async name => name === 'mcp.status' ? { enabled: true, autoStart: false, port: 19492 } : name === 'access.status' ? { account: 'admin', defaultPassword: true } : connection());
  api.mcpStatus.mockReset().mockImplementation(async () => ({ enabled: true, autoStart: false, connection: connection() }));
  api.mcpConfigure.mockReset().mockImplementation(async config => ({ enabled: true, autoStart: false, ...config, connection: connection() }));
});
const show = (errors?: unknown[]) => render(<McpSettings run={operation => { const result = operation(); if (errors) void result.catch(reason => errors.push(reason)); }} t={(_zh, en) => en} />);

it('organizes all 28 tools by capability and expands parameters only on demand', async () => {
  show();
  await screen.findByText('Engine status');
  expect(tools).toHaveLength(28);
  expect(new Set(tools.map(tool => tool.name)).size).toBe(28);
  expect(screen.getByRole('heading', { name: 'AI media service (MCP)' })).toBeTruthy();
  expect(screen.getByText('Available tools（28）')).toBeTruthy();
  const group = screen.getByText('Engine & sites').closest('details')!;
  expect(group.open).toBe(true);
  const row = screen.getByText('Engine status').closest('article')!;
  expect(within(row).getByText('Read only')).toBeTruthy();
  const parameters = row.querySelector('details')!;
  expect(parameters.open).toBe(false);
  fireEvent.click(within(parameters).getByText('Parameters & example'));
  expect(parameters.open).toBe(true);
  expect(within(parameters).getByText('Input parameters')).toBeTruthy();
  expect(capabilityCoverage.some(item => item.status === 'site')).toBe(true);
});

it('searches names and capabilities across closed groups and shows site and write limits', async () => {
  show(); await screen.findByText('Engine status');
  const search = screen.getByRole('searchbox', { name: 'Search tool names or capabilities' });
  fireEvent.change(search, { target: { value: 'subtitles' } });
  expect(screen.queryByText('Engine status')).toBeNull();
  const comments = screen.getByText('Subtitle text').closest('article')!;
  expect(within(comments).getByText('Site dependent')).toBeTruthy();
  expect(comments.closest('details.mcp-tool-group')?.getAttribute('open')).not.toBeNull();
  fireEvent.change(search, { target: { value: 'download_assets' } });
  const assets = screen.getByText('Save attachments only').closest('article')!;
  expect(within(assets).getByText('Write')).toBeTruthy();
  fireEvent.change(search, { target: { value: 'missing-tool-123' } });
  expect(screen.getByText('No matching tools.').getAttribute('role')).toBe('status');
  fireEvent.change(search, { target: { value: '' } });
  await waitFor(() => expect(screen.getByText('Engine status')).toBeTruthy());
});

it('saves auto start independently from the current service switch', async () => {
  let state = { enabled: true, autoStart: false, port: 19492 };
  api.command.mockImplementation(async (name, args) => name === 'mcp.status' ? state : name === 'mcp.configure' ? (state = { ...state, ...args }) : name === 'access.status' ? { account: 'admin', defaultPassword: true } : connection());
  show(); await screen.findByText('Engine status');
  const service = screen.getByRole('switch', { name: 'Enable MCP service' }) as HTMLInputElement;
  const automatic = screen.getByRole('switch', { name: 'Start MCP automatically with the app' }) as HTMLInputElement;
  fireEvent.click(automatic);
  await waitFor(() => expect(automatic.checked).toBe(true));
  expect(api.command).toHaveBeenCalledWith('mcp.configure', { autoStart: true });
  expect(service.checked).toBe(true);
  fireEvent.click(service);
  await waitFor(() => expect(service.checked).toBe(false));
  expect(api.command).toHaveBeenCalledWith('mcp.configure', { enabled: false });
  expect(automatic.checked).toBe(true);
  expect(screen.queryByText('More information')).toBeNull();
});

function enterPasswords(confirmation = 'new-password') {
  fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'admin' } });
  fireEvent.change(screen.getByLabelText('New password'), { target: { value: 'new-password' } });
  fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: confirmation } });
  fireEvent.click(screen.getByRole('button', { name: 'Update access password' }));
}

it('validates password confirmation before changing shared credentials', async () => {
  const errors: unknown[] = [];
  show(errors); await screen.findByText('Engine status');
  enterPasswords('different-password');
  await waitFor(() => expect(errors).toHaveLength(1));
  expect(String(errors[0])).toContain('New passwords do not match.');
  expect(api.command.mock.calls.some(([name]) => name === 'access.change_password')).toBe(false);
  expect((screen.getByLabelText('New password') as HTMLInputElement).value).toBe('new-password');
});

it('clears passwords after saving on desktop without requesting sign in', async () => {
  const expired = vi.fn(); window.addEventListener('huifa-session-expired', expired);
  try {
    show(); await screen.findByText('Engine status');
    enterPasswords();
    await screen.findByText('Access password updated.');
    expect(api.command).toHaveBeenCalledWith('access.change_password', { currentPassword: 'admin', newPassword: 'new-password' });
    for (const label of ['Current password', 'New password', 'Confirm new password']) expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe('');
    expect(screen.getByText(/Custom password set/)).toBeTruthy();
    expect(expired).not.toHaveBeenCalled();
  } finally { window.removeEventListener('huifa-session-expired', expired); }
});

it('keeps password fields and the web session after a rejected password change', async () => {
  api.desktop = false;
  api.command.mockImplementation(async name => {
    if (name === 'access.change_password') throw new Error('Current password is incorrect');
    return { account: 'admin', defaultPassword: true };
  });
  const expired = vi.fn(); const errors: unknown[] = [];
  window.addEventListener('huifa-session-expired', expired);
  try {
    show(errors); await screen.findByText('Engine status');
    enterPasswords();
    await waitFor(() => expect(errors).toHaveLength(1));
    expect((screen.getByLabelText('Current password') as HTMLInputElement).value).toBe('admin');
    expect((screen.getByLabelText('New password') as HTMLInputElement).value).toBe('new-password');
    expect(expired).not.toHaveBeenCalled();
    expect(screen.queryByText('Access password updated.')).toBeNull();
    expect((screen.getByRole('button', { name: 'Update access password' }) as HTMLButtonElement).disabled).toBe(false);
  } finally { window.removeEventListener('huifa-session-expired', expired); }
});

it('uses the web configuration endpoint and returns to sign in after changing the shared password', async () => {
  api.desktop = false;
  const expired = vi.fn(); window.addEventListener('huifa-session-expired', expired);
  try {
    show(); await screen.findByText('Engine status');
    const config = screen.getByLabelText('MCP configuration example').textContent!;
    expect(config).toContain('Bearer <YOUR_PASSWORD>');
    expect(config).toContain('/mcp-server');
    expect(config).not.toContain('Bearer admin');
    fireEvent.click(screen.getByRole('switch', { name: 'Start MCP automatically with the app' }));
    await waitFor(() => expect(api.mcpConfigure).toHaveBeenCalledWith({ autoStart: true }));
    expect((screen.getByRole('switch', { name: 'Enable MCP service' }) as HTMLInputElement).checked).toBe(true);
    enterPasswords();
    await waitFor(() => expect(expired).toHaveBeenCalledOnce());
    expect((screen.getByLabelText('Current password') as HTMLInputElement).value).toBe('');
  } finally { window.removeEventListener('huifa-session-expired', expired); }
});
