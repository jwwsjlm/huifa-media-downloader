import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { McpSettings } from './McpSettings';

const api = vi.hoisted(() => ({ mcpStatus: vi.fn(), mcpConfigure: vi.fn() }));
vi.mock('./transport', () => ({ host: { desktop: false, ...api } }));

it('uses the Docker web origin with a separate MCP protocol path and one service switch', async () => {
  api.mcpStatus.mockResolvedValue({ enabled: false, connection: { url: '/mcp-server', tools: [] } });
  api.mcpConfigure.mockResolvedValue({ enabled: true, connection: { url: '/mcp-server', tools: [] } });
  render(<McpSettings run={operation => { void operation(); }} t={(_zh, en) => en} />);
  const toggle = screen.getByRole('switch', { name: 'Enable MCP service' });
  await waitFor(() => expect((toggle as HTMLInputElement).disabled).toBe(false));
  const url = new URL('/mcp-server', window.location.origin).href;
  expect(JSON.parse(screen.getByLabelText('MCP configuration example').textContent || '')).toEqual({ mcpServers: { huifa: { url } } });
  expect(screen.queryByRole('option', { name: 'JSON / stdio' })).toBeNull();
  fireEvent.click(toggle);
  await waitFor(() => expect(api.mcpConfigure).toHaveBeenCalledWith(true));
  expect(await screen.findByText('Running')).toBeTruthy();
});
