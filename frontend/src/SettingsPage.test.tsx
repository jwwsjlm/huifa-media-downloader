import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { SettingsPage } from './SettingsPage';
import { initialState } from './state';
import type { Tool } from './types';

const transport = vi.hoisted(() => ({ desktop: false, command: vi.fn(), paths: vi.fn() }));
vi.mock('./transport', () => ({ host: { get desktop() { return transport.desktop; }, command: transport.command, paths: transport.paths, capabilities: () => ({ updates: false }) } }));
beforeEach(() => { transport.desktop = false; transport.command.mockReset().mockResolvedValue([]); transport.paths.mockReset().mockResolvedValue({ downloadDir: '/downloads' }); });
const run = (operation: () => Promise<unknown>) => { void operation(); };
it('offers bounded fragment presets and subtitle language/source controls', () => {
  const dispatch = vi.fn();
  render(<SettingsPage state={{ ...initialState, view: { ...initialState.view, settingsTab: 'downloads' } }} dispatch={dispatch} t={(_zh,en) => en} run={run} message={vi.fn()} />);
  const fragments = screen.getByRole('combobox', { name: 'Concurrent fragments per task' }) as HTMLSelectElement;
  expect(fragments.value).toBe('4'); expect(within(fragments).getByRole('option', { name: '6 (recommended)' })).toBeTruthy(); expect(fragments.options.length).toBe(8);
  fireEvent.change(fragments, { target: { value: '6' } }); expect(dispatch).toHaveBeenCalledWith({ type:'option',key:'fragmentConcurrent',value:6 });
  const language = screen.getByRole('combobox', { name: 'Subtitle language' }) as HTMLSelectElement; expect(language.value).toBe('none');
  fireEvent.change(language, { target: { value: 'zh-Hans' } }); expect(dispatch).toHaveBeenCalledWith({ type:'option',key:'subtitleLanguage',value:'zh-Hans' });
  fireEvent.change(screen.getByRole('combobox', { name:'Subtitle source' }), { target:{ value:'false' } }); expect(dispatch).toHaveBeenCalledWith({type:'option',key:'automaticSubtitles',value:false});
});
it('fetches models using the current draft and key without saving or replacing other settings', async () => {
  const dispatch = vi.fn();
  transport.command.mockImplementation(async name => name === 'ai.models' ? { models:['fixture-image-a','fixture-image-b'] } : []);
  render(<SettingsPage state={{ ...initialState, settings:{ aiModel:'keep-model',hasAiApiKey:true }, view:{ ...initialState.view, settingsTab:'appearance', settingsDraft:{ aiBaseUrl:'https://fixture.invalid/custom/v1', aiModel:'draft-model' } } }} dispatch={dispatch} t={(_zh,en)=>en} run={run} message={vi.fn()} />);
  fireEvent.change(screen.getByLabelText(/OpenAI API key/), {target:{ value:'draft-fixture-key' }});
  fireEvent.click(screen.getByRole('button',{ name:'Fetch models' }));
  const select = await screen.findByRole('combobox',{ name:'Available models' });
  expect(transport.command).toHaveBeenCalledWith('ai.models',{baseUrl:'https://fixture.invalid/custom/v1',apiKey:'draft-fixture-key'});
  expect((screen.getByLabelText(/AI image model/) as HTMLInputElement).value).toBe('draft-model');
  expect(transport.command.mock.calls.some(([name])=>name==='settings.save')).toBe(false);
  fireEvent.change(select,{target:{value:'fixture-image-b'}}); expect(dispatch).toHaveBeenCalledWith({type:'setting',key:'aiModel',value:'fixture-image-b'});
});
it('keeps the selected model and allows retry when discovery fails', async () => {
  transport.command.mockImplementation(async name => { if(name==='ai.models') throw new Error('Model list authentication failed'); return []; });
  render(<SettingsPage state={{...initialState,settings:{aiModel:'keep-model',hasAiApiKey:true},view:{...initialState.view,settingsTab:'appearance'}}} dispatch={vi.fn()} t={(_zh,en)=>en} run={run} message={vi.fn()} />);
  fireEvent.click(screen.getByRole('button',{name:'Fetch models'})); expect((await screen.findByRole('alert')).textContent).toContain('authentication failed');
  expect((screen.getByLabelText(/AI image model/) as HTMLInputElement).value).toBe('keep-model'); expect((screen.getByRole('button',{name:'Fetch models'}) as HTMLButtonElement).disabled).toBe(false);
});
it('saves settings with readable labels while preserving download option values', async () => {
  const dispatch = vi.fn();
  const settings = { downloadOptions: { audioFormat: 'best', sourceVideoCodec: 'auto', quality: 'best' } };
  transport.command.mockImplementation(async (command, args) => command === 'settings.save' ? args.settings : []);
  render(<SettingsPage state={{ ...initialState, settings, view: { ...initialState.view, settingsTab: 'downloads', optionsDraft: { quality: '720p' } } }} dispatch={dispatch} t={(_zh, en) => en} run={run} message={vi.fn()} />);
  expect((screen.getByLabelText('Audio output format') as HTMLSelectElement).selectedOptions[0].textContent).toBe('Best match');
  expect((screen.getByLabelText('Preferred source codec') as HTMLSelectElement).selectedOptions[0].textContent).toBe('Automatic');
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('settings.save', { settings: expect.objectContaining({ downloadOptions: expect.objectContaining({ audioFormat: 'best', sourceVideoCodec: 'auto', quality: '720p' }) }) }));
  await waitFor(() => expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: 'saved' })));
});

it('undoes drafts without sending a settings command', async () => {
  const dispatch = vi.fn(); const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  render(<SettingsPage state={{ ...initialState, view: { ...initialState.view, settingsTab: 'downloads', optionsDraft: { quality: '720p' } } }} dispatch={dispatch} t={(_zh, en) => en} run={run} message={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Undo changes' }));
  expect(dispatch).toHaveBeenCalledWith({ type: 'view', patch: { settingsDraft: null, optionsDraft: null } });
  expect(transport.command.mock.calls.some(([name]) => name === 'settings.save')).toBe(false);
  confirm.mockRestore();
});

function show(tools: Tool[] = [{ name: 'yt-dlp', available: true, version: '2026.08.19', platform: 'windows-x86_64', asset: 'yt-dlp.exe' }]) { render(<SettingsPage state={{ ...initialState, tools, view: { ...initialState.view, settingsTab: 'tools' } }} dispatch={vi.fn()} t={(_zh, en) => en} run={run} message={vi.fn()} />); }
it('saves browser login as the default identity through the desktop network settings', async () => {
  transport.desktop = true;
  const dispatch = vi.fn(); const message = vi.fn();
  const result = { cookie: { id: 'login-id', name: 'Login' }, settings: { downloadOptions: { cookieId: 'auto' } } };
  transport.command.mockImplementation(async command => command === 'browser.login.status' ? { active: true, mode: 'embedded', targetUrl: 'https://example.com/', systemBrowser: 'edge' } : command === 'browser.login.save' ? result : []);
  render(<SettingsPage state={{ ...initialState, view: { ...initialState.view, settingsTab: 'network' } }} dispatch={dispatch} t={(_zh, en) => en} run={run} message={message} />);
  expect(screen.queryByRole('dialog')).toBeNull();
  const controls = within(screen.getByRole('group', { name: 'Browser sign-in' }));
  const save = await controls.findByRole('button', { name: 'Sync now' });
  await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(save);
  await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'cookie.saved', id: 'auto' }));
  expect(message).toHaveBeenCalledWith('Cookies saved locally and enabled for new downloads');
  expect(screen.queryByRole('dialog')).toBeNull();
});
it('offers optional faster Deno parsing only on desktop and explains the size tradeoff', async () => {
  transport.desktop = true; show([{ name: 'qjs', available: true, version: '0.17.0' }]);
  expect(screen.getByText(/executable is about 93 MiB/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Install Deno for faster parsing' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'deno' }));
});
it('shows automatic sign-in status without exposing cookie records or changing settings drafts', async () => {
  transport.desktop = true;
  const dispatch = vi.fn();
  transport.command.mockImplementation(async command => command === 'cookies.list' ? [{ id: 'enabled', name: 'Saved login', domains: ['youtube.com'] }, { id: 'draft', name: 'Draft login', domains: ['douyin.com'] }] : { active: false, mode: null, systemBrowser: 'edge', preferredUrl: 'https://www.douyin.com/' });
  render(<SettingsPage state={{ ...initialState, settings: { downloadOptions: { cookieId: 'enabled' } }, view: { ...initialState.view, settingsTab: 'network', optionsDraft: { cookieId: 'draft', quality: '720p' } } }} dispatch={dispatch} t={(_zh, en) => en} run={run} message={vi.fn()} />);
  expect(await screen.findByText('Website sign-in data is saved and matched to downloads automatically.')).toBeTruthy();
  expect(screen.queryByText('Saved login')).toBeNull();
  expect(screen.queryByText('Draft login')).toBeNull();
  expect(screen.queryByRole('button', { name: 'View cookies' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
  expect(screen.queryByLabelText('Cookie name')).toBeNull();
  const controls = within(screen.getByRole('group', { name: 'Browser sign-in' }));
  await waitFor(() => expect((controls.getByRole('button', { name: 'Open built-in browser' }) as HTMLButtonElement).disabled).toBe(false));
  expect((controls.getByLabelText('Sign-in website URL') as HTMLInputElement).value).toBe('https://www.douyin.com/');
  fireEvent.click(controls.getByRole('button', { name: 'Open built-in browser' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('browser.login.open', { url: 'https://www.douyin.com/', mode: 'embedded' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(controls.queryByRole('combobox')).toBeNull();
  expect(controls.queryByLabelText('Cookie name')).toBeNull();
  expect(screen.queryByRole('combobox', { name: 'Cookie source' })).toBeNull();
  expect(transport.command.mock.calls.some(([name]) => name === 'cookies.read' || name === 'cookies.delete' || name === 'settings.save')).toBe(false);
  expect(dispatch).not.toHaveBeenCalled();
});
it('explains Docker image updates instead of offering a forbidden tool command', async () => {
  show();
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('cookies.list'));
  expect(screen.queryByRole('button', { name: 'Update / install' })).toBeNull();
  expect(screen.getByText(/Update the Docker image/)).toBeTruthy();
  expect(screen.queryByLabelText('yt-dlp update channel')).toBeNull();
  expect(screen.queryByLabelText('yt-dlp download source')).toBeNull();
  expect(screen.getByRole('button', { name: 'Check yt-dlp updates' })).toBeTruthy();
  expect(screen.queryByLabelText('Enable local MCP')).toBeNull();
});
it('shows a password placeholder, tool reference and independent MCP switches', async () => {
  transport.desktop = true;
  const http = { mcpServers: { huifa: { url: 'http://127.0.0.1:19492/mcp', headers: { Authorization: 'Bearer <YOUR_PASSWORD>' } } } };
  transport.command.mockImplementation(async command => command === 'mcp.status' ? { enabled: false, autoStart: false, port: 19492 } : command === 'mcp.connection' ? { url: http.mcpServers.huifa.url, http, tools: [{ name: 'get_video_info', description: 'Get video information', inputSchema: { properties: { url: {} }, required: ['url'] }, annotations: { readOnlyHint: true } }] } : command === 'mcp.configure' ? { enabled: true, autoStart: false, port: 19492 } : command === 'access.status' ? { account: 'admin', defaultPassword: true } : []);
  render(<SettingsPage state={{ ...initialState, view: { ...initialState.view, settingsTab: 'mcp' } }} dispatch={vi.fn()} t={(_zh, en) => en} run={run} message={vi.fn()} />);
  const toggle = screen.getByRole('switch', { name: 'Enable MCP service' });
  await waitFor(() => expect((toggle as HTMLInputElement).disabled).toBe(false));
  expect(JSON.parse(screen.getByLabelText('MCP configuration example').textContent || '')).toEqual(http);
  expect(screen.getByText('get_video_info(url)')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Reset token' })).toBeNull();
  expect(screen.getAllByRole('switch')).toHaveLength(2);
  expect(screen.getByRole('switch', { name: 'Start MCP automatically with the app' })).toBeTruthy();
  fireEvent.click(toggle);
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('mcp.configure', { enabled: true }));
  expect(await screen.findByText('Running')).toBeTruthy();
});
it('keeps runtime tool installation available on desktop', async () => {
  transport.desktop = true; show();
  await waitFor(() => expect(transport.paths).toHaveBeenCalled());
  expect(screen.getByRole('button', { name: 'Update / install' })).toBeTruthy();
  expect(screen.getByText(/Current 2026.08.19/)).toBeTruthy();
  expect(screen.getByText(/Selected for this system: windows-x86_64/)).toBeTruthy();
});
it('sends the chosen official channel and exact tag and clears a tag when switching channels', async () => {
  transport.desktop = true; show();
  await waitFor(() => expect(transport.paths).toHaveBeenCalled());
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'nightly' } });
  fireEvent.change(screen.getByLabelText(/Specific version/), { target: { value: ' 2026.10.10.233141 ' } });
  fireEvent.click(screen.getByRole('button', { name: 'Update / install' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'yt-dlp', channel: 'nightly', version: '2026.10.10.233141', downloadSource: 'auto' }));
  await waitFor(() => expect((screen.getByLabelText('yt-dlp update channel') as HTMLSelectElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'stable' } });
  expect((screen.getByLabelText(/Specific version/) as HTMLInputElement).value).toBe('');
  fireEvent.click(screen.getByRole('button', { name: 'Update / install' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'yt-dlp', channel: 'stable', version: '', downloadSource: 'auto' }));
});
it('checks GitHub for the selected channel and shows the matching version and package', async () => {
  transport.desktop = true; show();
  transport.command.mockImplementation(async command => command === 'tools.latest' ? { name: 'yt-dlp', channel: 'nightly', current: '2026.08.19', latest: '2026.10.10.233141', updateAvailable: true, platform: 'windows-x86_64', asset: 'yt-dlp.exe' } : []);
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'nightly' } });
  fireEvent.click(screen.getByRole('button', { name: 'Check yt-dlp updates' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.latest', { name: 'yt-dlp', channel: 'nightly' }));
  expect((await screen.findByRole('status')).textContent).toContain('Latest on GitHub 2026.10.10.233141');
  expect(screen.getByRole('status').textContent).toContain('An update is available');
  expect(screen.getByRole('status').textContent).toContain('windows-x86_64 · yt-dlp.exe');
  expect(transport.command.mock.calls.some(([command]) => command === 'tools.update')).toBe(false);
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'stable' } });
  expect(screen.queryByRole('status')).toBeNull();
});
it('lets the user explicitly select CDN downloads', async () => {
  transport.desktop = true; show();
  fireEvent.change(screen.getByLabelText('yt-dlp download source'), { target: { value: 'cdn' } });
  fireEvent.click(screen.getByRole('button', { name: 'Update / install' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'yt-dlp', channel: 'stable', version: '', downloadSource: 'cdn' }));
});
it.each([
  ['2026.08.19', 'Up to date for this channel.'],
  ['2026.10.10.233141', 'The installed version is newer than this channel'],
])('shows the comparison for installed version %s without suggesting a newer release', async (current, hint) => {
  transport.desktop = true; show();
  transport.command.mockImplementation(async command => command === 'tools.latest' ? { name: 'yt-dlp', channel: 'stable', current, latest: '2026.08.19', updateAvailable: false, platform: 'windows-x86_64', asset: 'yt-dlp.exe' } : []);
  fireEvent.click(screen.getByRole('button', { name: 'Check yt-dlp updates' }));
  expect((await screen.findByRole('status')).textContent).toContain(hint);
  expect(screen.getByRole('status').textContent).not.toContain('An update is available');
});
