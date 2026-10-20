import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useReducer } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import { SettingsPage } from './SettingsPage';
import { initialState, reducer } from './state';
import type { Settings, Tool } from './types';

const transport = vi.hoisted(() => ({ desktop: false, command: vi.fn(), paths: vi.fn(), directory: vi.fn(), openDownloadDirectory: vi.fn() }));
vi.mock('./transport', () => ({ host: { get desktop() { return transport.desktop; }, command: transport.command, paths: transport.paths, directory: transport.directory, openDownloadDirectory: transport.openDownloadDirectory, capabilities: () => ({ updates: false }) } }));
beforeEach(() => { transport.desktop = false; transport.command.mockReset().mockResolvedValue([]); transport.paths.mockReset().mockResolvedValue({ downloadDir: '/downloads' }); transport.directory.mockReset().mockResolvedValue(null); transport.openDownloadDirectory.mockReset().mockResolvedValue(undefined); });
const run = (operation: () => Promise<unknown>) => { void operation(); };
function AnalysisSettings({ settings = {}, visible = true }: { settings?: Settings; visible?: boolean }) {
  const [state, dispatch] = useReducer(reducer, { ...initialState, settings, view: { ...initialState.view, settingsTab: 'downloads' } });
  return visible ? <SettingsPage state={state} dispatch={dispatch} t={(_zh, en) => en} run={run} message={vi.fn()} /> : null;
}
it.each([false, true])('uses host detection for automatic analysis threads on desktop=%s', desktop => {
  transport.desktop = desktop;
  render(<AnalysisSettings settings={{ detectedAnalysisThreads: 20 }} />);
  expect((screen.getByRole('combobox', { name: 'Video analysis threads' }) as HTMLSelectElement).value).toBe('auto');
  expect(screen.getByRole('option', { name: 'Automatic (up to 8 threads; host: 20 logical processors)' })).toBeTruthy();
  expect(screen.queryByRole('spinbutton', { name: 'Manual analysis threads' })).toBeNull();
  expect(screen.getByText(/running analyses are unchanged/)).toBeTruthy();
});
it('shows automatic mode without inventing a processor count when host metadata is missing', () => {
  render(<AnalysisSettings />);
  expect(within(screen.getByRole('combobox', { name: 'Video analysis threads' })).getByRole('option', { name: 'Automatic (up to 8 threads)' })).toBeTruthy();
  expect(screen.queryByText(/host:.*logical processors/)).toBeNull();
});
it('keeps a manual analysis draft across tabs, saves it without host metadata and can save automatic mode again', async () => {
  transport.command.mockImplementation(async (name, args) => name === 'settings.save' ? { ...args.settings, detectedAnalysisThreads: 20 } : []);
  render(<AnalysisSettings settings={{ analysisThreads: 0, detectedAnalysisThreads: 20 }} />);
  fireEvent.change(screen.getByRole('combobox', { name: 'Video analysis threads' }), { target: { value: 'manual' } });
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Manual analysis threads' }), { target: { value: '4' } });
  fireEvent.click(screen.getByRole('button', { name: 'Tools & updates' }));
  expect(screen.queryByRole('spinbutton', { name: 'Manual analysis threads' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Downloads' }));
  expect((screen.getByRole('spinbutton', { name: 'Manual analysis threads' }) as HTMLInputElement).value).toBe('4');
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('settings.save', { settings: expect.objectContaining({ analysisThreads: 4 }) }));
  const saved = transport.command.mock.calls.find(([name]) => name === 'settings.save')?.[1].settings;
  expect(saved).not.toHaveProperty('detectedAnalysisThreads');
  await waitFor(() => expect((screen.getByRole('button', { name: 'Save settings' }) as HTMLButtonElement).disabled).toBe(true));
  fireEvent.change(screen.getByRole('combobox', { name: 'Video analysis threads' }), { target: { value: 'auto' } });
  expect(screen.queryByRole('spinbutton', { name: 'Manual analysis threads' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('settings.save', { settings: expect.objectContaining({ analysisThreads: 0 }) }));
});
it.each([false, true])('keeps newer settings, download options and API key across a delayed save (reopen=%s)', async reopen => {
  let finish!: (settings: Settings) => void;
  let submitted!: Settings;
  transport.command.mockImplementation(async (name, args) => {
    if (name !== 'settings.save') return [];
    submitted = args.settings;
    return new Promise<Settings>(resolve => { finish = resolve; });
  });
  const settings = { hasAiApiKey: true };
  const view = render(<AnalysisSettings settings={settings} />);
  fireEvent.click(screen.getByRole('button', { name: 'Appearance & covers' }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Completion time format' }), { target: { value: 'YYMMDD HH:mm' } });
  fireEvent.change(screen.getByLabelText(/OpenAI API key/), { target: { value: 'submitted-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
  await waitFor(() => expect(submitted.aiApiKey).toBe('submitted-key'));
  if (reopen) {
    view.rerender(<AnalysisSettings settings={settings} visible={false} />);
    view.rerender(<AnalysisSettings settings={settings} />);
  }
  fireEvent.change(screen.getByRole('textbox', { name: 'Completion time format' }), { target: { value: 'YYYY-MM-DD' } });
  fireEvent.change(screen.getByLabelText(/OpenAI API key/), { target: { value: 'newer-key' } });
  fireEvent.click(screen.getByRole('button', { name: 'Downloads' }));
  fireEvent.change(screen.getByRole('combobox', { name: 'Concurrent fragments per task' }), { target: { value: '6' } });
  const { aiApiKey: _key, ...saved } = submitted;
  await act(async () => finish(saved));
  expect(screen.getByText('Unsaved changes')).toBeTruthy();
  expect((screen.getByRole('combobox', { name: 'Concurrent fragments per task' }) as HTMLSelectElement).value).toBe('6');
  fireEvent.click(screen.getByRole('button', { name: 'Appearance & covers' }));
  expect((screen.getByRole('textbox', { name: 'Completion time format' }) as HTMLInputElement).value).toBe('YYYY-MM-DD');
  expect((screen.getByLabelText(/OpenAI API key/) as HTMLInputElement).value).toBe('newer-key');
  transport.command.mockImplementation(async (name, args) => name === 'settings.save' ? args.settings : []);
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('settings.save', { settings: expect.objectContaining({ completedTimeFormat: 'YYYY-MM-DD', aiApiKey: 'newer-key', downloadOptions: expect.objectContaining({ fragmentConcurrent: 6 }) }) }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Save settings' }) as HTMLButtonElement).disabled).toBe(true));
  expect((screen.getByLabelText(/OpenAI API key/) as HTMLInputElement).value).toBe('');
});
it('clears the saved API key without dropping a replacement typed while the request is pending', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  let finish!: () => void;
  transport.command.mockImplementation(async (name, args) => name === 'settings.save'
    ? new Promise<Settings>(resolve => { finish = () => resolve({ ...args.settings, hasAiApiKey: false }); }) : []);
  render(<AnalysisSettings settings={{ hasAiApiKey: true }} />);
  fireEvent.click(screen.getByRole('button', { name: 'Appearance & covers' }));
  fireEvent.click(screen.getByRole('button', { name: 'Clear saved key' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('settings.save', { settings: expect.objectContaining({ aiApiKey: '' }) }));
  expect((screen.getByRole('button', { name: 'Clear saved key' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/OpenAI API key/), { target: { value: 'replacement-key' } });
  await act(async () => finish());
  expect((screen.getByLabelText(/OpenAI API key/) as HTMLInputElement).value).toBe('replacement-key');
  expect(screen.getByText('Unsaved changes')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Save settings' }) as HTMLButtonElement).disabled).toBe(false);
  confirm.mockRestore();
});
it.each(['', '0', '-1', '1.5', '257'])('rejects invalid manual analysis threads %s without switching to automatic mode', value => {
  render(<AnalysisSettings settings={{ analysisThreads: 4, detectedAnalysisThreads: 20 }} />);
  const input = screen.getByRole('spinbutton', { name: 'Manual analysis threads' }) as HTMLInputElement;
  fireEvent.change(input, { target: { value } });
  expect((screen.getByRole('combobox', { name: 'Video analysis threads' }) as HTMLSelectElement).value).toBe('manual');
  expect(screen.getByRole('alert').textContent).toContain('integer from 1 to 256');
  expect(input.checkValidity()).toBe(false);
  const save = screen.getByRole('button', { name: 'Save settings' }) as HTMLButtonElement;
  expect(save.disabled).toBe(true); fireEvent.click(save);
  expect(transport.command.mock.calls.some(([name]) => name === 'settings.save')).toBe(false);
});
it('undoes an invalid manual analysis draft using the saved automatic mode', () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  render(<AnalysisSettings settings={{ analysisThreads: 0 }} />);
  fireEvent.change(screen.getByRole('combobox', { name: 'Video analysis threads' }), { target: { value: 'manual' } });
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Manual analysis threads' }), { target: { value: '' } });
  fireEvent.click(screen.getByRole('button', { name: 'Undo changes' }));
  expect((screen.getByRole('combobox', { name: 'Video analysis threads' }) as HTMLSelectElement).value).toBe('auto');
  expect(screen.queryByRole('alert')).toBeNull();
  expect((screen.getByRole('button', { name: 'Save settings' }) as HTMLButtonElement).disabled).toBe(true);
  confirm.mockRestore();
});
it('keeps desktop clipboard monitoring off until the changed setting is saved', async () => {
  transport.desktop = true;
  const dispatch = vi.fn();
  const props = { state: { ...initialState, view: { ...initialState.view, settingsTab: 'downloads' } }, dispatch, t: (_zh: string, en: string) => en, run, message: vi.fn() };
  const page = render(<SettingsPage {...props} />);
  const monitor = screen.getByRole('checkbox', { name: 'Automatically download links copied to the clipboard' }) as HTMLInputElement;
  expect(monitor.checked).toBe(false);
  expect(screen.getByText(/After saving, current and newly copied HTTP/)).toBeTruthy();
  fireEvent.click(monitor);
  expect(dispatch).toHaveBeenCalledWith({ type: 'setting', key: 'clipboardMonitoring', value: true });
  expect(transport.command.mock.calls.some(([command]) => command === 'settings.save')).toBe(false);
  page.rerender(<SettingsPage {...props} state={{ ...props.state, view: { ...props.state.view, settingsDraft: { clipboardMonitoring: true } } }} />);
  transport.command.mockImplementation(async (command, args) => command === 'settings.save' ? args.settings : []);
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('settings.save', { settings: expect.objectContaining({ clipboardMonitoring: true }) }));
});
it('hides clipboard monitoring from the web settings', () => {
  render(<SettingsPage state={initialState} dispatch={vi.fn()} t={(_zh, en) => en} run={run} message={vi.fn()} />);
  expect(screen.queryByRole('checkbox', { name: 'Automatically download links copied to the clipboard' })).toBeNull();
});
it('edits and saves the completion time format with token guidance', async () => {
  const dispatch = vi.fn();
  const props = { state: { ...initialState, view: { ...initialState.view, settingsTab: 'appearance' } }, dispatch, t: (_zh: string, en: string) => en, run, message: vi.fn() };
  const page = render(<SettingsPage {...props} />);
  const input = screen.getByRole('textbox', { name: 'Completion time format' }) as HTMLInputElement;
  expect(input.value).toBe('YYYY-MM-DD HH:mm');
  expect(input.placeholder).toBe('YYYY-MM-DD HH:mm');
  expect(screen.getByText(/YY short year, MM month/)).toBeTruthy();
  fireEvent.change(input, { target: { value: 'YYMMDD HH:mm' } });
  expect(dispatch).toHaveBeenCalledWith({ type: 'setting', key: 'completedTimeFormat', value: 'YYMMDD HH:mm' });
  page.rerender(<SettingsPage {...props} state={{ ...props.state, view: { ...props.state.view, settingsDraft: { completedTimeFormat: 'YYMMDD HH:mm' } } }} />);
  transport.command.mockImplementation(async (command, args) => command === 'settings.save' ? args.settings : []);
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('settings.save', { settings: expect.objectContaining({ completedTimeFormat: 'YYMMDD HH:mm' }) }));
});
it('saves the selected desktop download folder immediately and reloads its saved value', async () => {
  transport.desktop = true;
  const dispatch = vi.fn(); const message = vi.fn();
  transport.paths.mockResolvedValue({ downloadDir: 'D:/old' });
  transport.directory.mockResolvedValue('D:/new');
  transport.command.mockImplementation(async (name, args) => name === 'host.download_directory' ? { downloadDir: args.path } : []);
  const props = { state: { ...initialState, view: { ...initialState.view, settingsTab: 'downloads' as const } }, dispatch, t: (_zh: string, en: string) => en, run, message };
  const page = render(<SettingsPage {...props} />);
  await waitFor(() => expect((screen.getByLabelText('Download folder') as HTMLInputElement).value).toBe('D:/old'));
  fireEvent.click(screen.getByRole('button', { name: 'Choose folder' }));
  await waitFor(() => expect((screen.getByLabelText('Download folder') as HTMLInputElement).value).toBe('D:/new'));
  expect(transport.directory).toHaveBeenCalledWith('D:/old');
  expect(transport.command).toHaveBeenCalledWith('host.download_directory', { path: 'D:/new' });
  expect(message).toHaveBeenCalledWith('Download folder saved for new tasks');
  expect(dispatch).not.toHaveBeenCalled();
  expect(transport.command.mock.calls.some(([name]) => name === 'settings.save')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: 'Open' }));
  expect(transport.openDownloadDirectory).toHaveBeenCalledTimes(1);
  page.unmount();
  transport.paths.mockResolvedValue({ downloadDir: 'D:/new' });
  render(<SettingsPage {...props} />);
  await waitFor(() => expect((screen.getByLabelText('Download folder') as HTMLInputElement).value).toBe('D:/new'));
});
it('keeps the existing download folder when choosing is cancelled or saving fails', async () => {
  transport.desktop = true;
  transport.paths.mockResolvedValue({ downloadDir: 'D:/old' });
  render(<SettingsPage state={{ ...initialState, view: { ...initialState.view, settingsTab: 'downloads' } }} dispatch={vi.fn()} t={(_zh, en) => en} run={run} message={vi.fn()} />);
  const choose = screen.getByRole('button', { name: 'Choose folder' }) as HTMLButtonElement;
  await waitFor(() => expect(choose.disabled).toBe(false));
  fireEvent.click(choose);
  await waitFor(() => expect(choose.disabled).toBe(false));
  expect(transport.command.mock.calls.some(([name]) => name === 'host.download_directory')).toBe(false);
  transport.directory.mockResolvedValue('D:/new');
  transport.command.mockRejectedValue(new Error('Download folder is not writable'));
  fireEvent.click(choose);
  expect((await screen.findByRole('alert')).textContent).toContain('not writable');
  expect((screen.getByLabelText('Download folder') as HTMLInputElement).value).toBe('D:/old');
  expect(choose.disabled).toBe(false);
});
it('keeps Docker download folders deployment-managed', () => {
  render(<SettingsPage state={{ ...initialState, view: { ...initialState.view, settingsTab: 'downloads' } }} dispatch={vi.fn()} t={(_zh, en) => en} run={run} message={vi.fn()} />);
  expect(screen.queryByRole('button', { name: 'Choose folder' })).toBeNull();
  expect(screen.getByText('Configure the ./downloads volume in Docker Compose.')).toBeTruthy();
  expect((screen.getByLabelText('Download folder') as HTMLInputElement).readOnly).toBe(true);
});
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
it('opens sign-in with the current proxy draft without implicitly saving settings', async () => {
  transport.desktop = true;
  transport.command.mockImplementation(async command => command === 'cookies.list' ? [] : { active: false, systemBrowser: 'edge' });
  render(<SettingsPage state={{ ...initialState, settings: { hasProxy: true }, view: { ...initialState.view, settingsTab: 'network', optionsDraft: { proxy: 'http://127.0.0.1:7890' } } }} dispatch={vi.fn()} t={(_zh, en) => en} run={run} message={vi.fn()} />);
  const open = screen.getByRole('button', { name: 'Open built-in browser' });
  await waitFor(() => expect((open as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(open);
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('browser.login.open', { url: 'https://www.youtube.com/', mode: 'embedded', proxy: 'http://127.0.0.1:7890' }));
  expect(screen.getByText(/Sign-in browsers share the download proxy/)).toBeTruthy();
  expect(transport.command.mock.calls.some(([name]) => name === 'settings.save')).toBe(false);
});
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
  expect(screen.queryByRole('button', { name: /^(?:Update|Install)$/ })).toBeNull();
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
it('hides install and update actions for installed tools until an update is confirmed', async () => {
  transport.desktop = true; show([{ name: 'yt-dlp', available: true, version: '2026.08.19', platform: 'windows-x86_64' }, { name: 'qjs', available: true, version: '0.17.0' }, { name: 'deno', available: true, version: '2.5.0' }]);
  await waitFor(() => expect(transport.paths).toHaveBeenCalled());
  expect(screen.queryByRole('button', { name: /^(?:Update|Install)$/ })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Install Deno for faster parsing' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Apply FFmpeg version' })).toBeTruthy();
  expect(screen.getByText(/Current 2026.08.19/)).toBeTruthy();
  expect(screen.getByText(/Selected for this system: windows-x86_64/)).toBeTruthy();
});
it('installs missing tools and disables other tool actions while installing', async () => {
  transport.desktop = true; let finish!: () => void;
  transport.command.mockImplementation(async name => name === 'tools.update' ? new Promise<void>(resolve => { finish = resolve; }) : []);
  show([{ name: 'yt-dlp', available: false }, { name: 'qjs', available: false }]);
  expect(screen.getAllByRole('button', { name: 'Install' })).toHaveLength(2);
  const qjs = within(screen.getByText('qjs').closest('.component-row') as HTMLElement);
  fireEvent.click(qjs.getByRole('button', { name: 'Install' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'qjs' }));
  expect((qjs.getByRole('button', { name: 'Installing…' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: 'Install' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: 'Check yt-dlp updates' }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => finish());
  expect(screen.getAllByRole('button', { name: 'Install' }).every(button => !(button as HTMLButtonElement).disabled)).toBe(true);
});
it('sends the chosen official channel and exact tag and clears a tag when switching channels', async () => {
  transport.desktop = true; show();
  await waitFor(() => expect(transport.paths).toHaveBeenCalled());
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'nightly' } });
  fireEvent.change(screen.getByLabelText(/Specific version/), { target: { value: ' 2026.10.10.233141 ' } });
  fireEvent.click(screen.getByRole('button', { name: 'Install specific version' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'yt-dlp', channel: 'nightly', version: '2026.10.10.233141', downloadSource: 'auto' }));
  await waitFor(() => expect((screen.getByLabelText('yt-dlp update channel') as HTMLSelectElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'stable' } });
  expect((screen.getByLabelText(/Specific version/) as HTMLInputElement).value).toBe('');
  expect(screen.queryByRole('button', { name: 'Install specific version' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Update' })).toBeNull();
  expect(transport.command.mock.calls.filter(([name]) => name === 'tools.update')).toHaveLength(1);
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
  expect(screen.getByRole('button', { name: 'Update' })).toBeTruthy();
  expect(transport.command.mock.calls.some(([command]) => command === 'tools.update')).toBe(false);
  fireEvent.change(screen.getByLabelText('yt-dlp update channel'), { target: { value: 'stable' } });
  expect(screen.queryByRole('status')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Update' })).toBeNull();
});
it('lets the user explicitly select CDN downloads', async () => {
  transport.desktop = true; show([{ name: 'yt-dlp', available: false }]);
  fireEvent.change(screen.getByLabelText('yt-dlp download source'), { target: { value: 'cdn' } });
  fireEvent.click(screen.getByRole('button', { name: 'Install' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'yt-dlp', channel: 'stable', version: '', downloadSource: 'cdn' }));
});
it.each([
  ['2026.08.19', 'Up to date for this channel.'],
  ['2026.10.10.233141', 'The installed version is newer than this channel'],
])('shows the comparison for installed version %s without suggesting a newer release', async (current, hint) => {
  transport.desktop = true; show([{ name: 'yt-dlp', available: true, version: current }]);
  transport.command.mockImplementation(async command => command === 'tools.latest' ? { name: 'yt-dlp', channel: 'stable', current, latest: '2026.08.19', updateAvailable: false, platform: 'windows-x86_64', asset: 'yt-dlp.exe' } : []);
  fireEvent.click(screen.getByRole('button', { name: 'Check yt-dlp updates' }));
  expect((await screen.findByRole('status')).textContent).toContain(hint);
  expect(screen.getByRole('status').textContent).not.toContain('An update is available');
  expect(screen.queryByRole('button', { name: /^(?:Update|Install)$/ })).toBeNull();
});

it('updates an installed tool only after a newer release is confirmed and clears the action after installation', async () => {
  transport.desktop = true; let finish!: () => void;
  transport.command.mockImplementation(async name => name === 'tools.latest' ? { name: 'yt-dlp', channel: 'stable', current: '2026.08.19', latest: '2026.10.10', updateAvailable: true, platform: 'windows-x86_64', asset: 'yt-dlp.exe' } : name === 'tools.update' ? new Promise<void>(resolve => { finish = resolve; }) : []);
  show();
  fireEvent.click(screen.getByRole('button', { name: 'Check yt-dlp updates' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Update' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'yt-dlp', channel: 'stable', version: '', downloadSource: 'auto' }));
  expect((screen.getByRole('button', { name: 'Installing…' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: 'Check components' }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => finish());
  expect(screen.queryByRole('button', { name: /^(?:Update|Installing…)$/ })).toBeNull();
});

it('hides a previously confirmed update when the installed version changes', async () => {
  transport.desktop = true;
  transport.command.mockImplementation(async name => name === 'tools.latest' ? { name: 'yt-dlp', channel: 'stable', current: '2026.08.19', latest: '2026.10.10', updateAvailable: true, platform: 'windows-x86_64', asset: 'yt-dlp.exe' } : []);
  const props = { state: { ...initialState, tools: [{ name: 'yt-dlp', available: true, version: '2026.08.19' }], view: { ...initialState.view, settingsTab: 'tools' } }, dispatch: vi.fn(), t: (_zh: string, en: string) => en, run, message: vi.fn() };
  const page = render(<SettingsPage {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Check yt-dlp updates' }));
  await screen.findByRole('button', { name: 'Update' });
  page.rerender(<SettingsPage {...props} state={{ ...props.state, tools: [{ name: 'yt-dlp', available: true, version: '2026.10.10' }] }} />);
  expect(screen.queryByRole('button', { name: 'Update' })).toBeNull();
});

it('keeps the default FFmpeg policy as a saved draft on the web without offering runtime installation', async () => {
  const dispatch = vi.fn();
  const props = { state: { ...initialState, view: { ...initialState.view, settingsTab: 'tools' } }, dispatch, t: (_zh: string, en: string) => en, run, message: vi.fn() };
  const page = render(<SettingsPage {...props} />);
  expect((screen.getByRole('combobox', { name: 'FFmpeg version policy' }) as HTMLSelectElement).value).toBe('compatibility');
  fireEvent.change(screen.getByRole('combobox', { name: 'FFmpeg version policy' }), { target: { value: 'efficiency' } });
  expect(dispatch).toHaveBeenCalledWith({ type: 'setting', key: 'ffmpegMode', value: 'efficiency' });
  expect(transport.command.mock.calls.some(([name]) => name === 'settings.save' || name === 'tools.update')).toBe(false);
  page.rerender(<SettingsPage {...props} state={{ ...props.state, view: { ...props.state.view, settingsDraft: { ffmpegMode: 'efficiency' } } }} />);
  transport.command.mockImplementation(async (name, args) => name === 'settings.save' ? args.settings : []);
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('settings.save', { settings: expect.objectContaining({ ffmpegMode: 'efficiency' }) }));
  expect(screen.queryByRole('button', { name: 'Apply FFmpeg version' })).toBeNull();
  expect(screen.getByText(/Update the Docker image/)).toBeTruthy();
});

it('queries an exact FFmpeg release for the draft policy and clears the tag when changing policies', async () => {
  transport.desktop = true; const dispatch = vi.fn();
  const tools: Tool[] = [{ name: 'ffmpeg', available: true, version: '7.1.1', installedMode: 'compatibility', releaseTag: 'n7.1.1', repository: 'official/compatibility', platform: 'windows-x86_64', asset: 'ffmpeg.zip' }];
  const props = { state: { ...initialState, tools, settings: { ffmpegMode: 'compatibility' as const }, view: { ...initialState.view, settingsTab: 'tools' } }, dispatch, t: (_zh: string, en: string) => en, run, message: vi.fn() };
  const page = render(<SettingsPage {...props} />);
  fireEvent.change(screen.getByRole('combobox', { name: 'FFmpeg version policy' }), { target: { value: 'efficiency' } });
  page.rerender(<SettingsPage {...props} state={{ ...props.state, view: { ...props.state.view, settingsDraft: { ffmpegMode: 'efficiency' } } }} />);
  expect((screen.getByRole('button', { name: 'Apply FFmpeg version' }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText('Save the policy before applying an FFmpeg version.')).toBeTruthy();
  fireEvent.change(screen.getByRole('textbox', { name: /FFmpeg exact release tag/ }), { target: { value: ' n8.0 ' } });
  transport.command.mockImplementation(async name => name === 'tools.latest' ? { name: 'ffmpeg', channel: 'efficiency', mode: 'efficiency', current: '7.1.1', latest: 'n8.0', updateAvailable: true, platform: 'windows-x86_64', asset: 'ffmpeg-8.zip', repository: 'official/efficiency' } : []);
  fireEvent.click(screen.getByRole('button', { name: 'Check FFmpeg release' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.latest', { name: 'ffmpeg', mode: 'efficiency', version: 'n8.0' }));
  const release = await screen.findByLabelText('Selected FFmpeg release');
  expect(release.textContent).toContain('n8.0'); expect(release.textContent).toContain('official/efficiency'); expect(release.textContent).toContain('windows-x86_64 · ffmpeg-8.zip');
  expect(screen.getByText('Current 7.1.1')).toBeTruthy();
  expect(transport.command.mock.calls.some(([name]) => name === 'tools.update')).toBe(false);
  fireEvent.change(screen.getByRole('combobox', { name: 'FFmpeg version policy' }), { target: { value: 'compatibility' } });
  page.rerender(<SettingsPage {...props} />);
  expect((screen.getByRole('textbox', { name: /FFmpeg exact release tag/ }) as HTMLInputElement).value).toBe('');
  expect(screen.queryByLabelText('Selected FFmpeg release')).toBeNull();
});

it('applies the saved FFmpeg policy and exact tag without saving unrelated drafts or claiming installation early', async () => {
  transport.desktop = true; const dispatch = vi.fn(); const message = vi.fn();
  let finish!: () => void;
  const tools: Tool[] = [{ name: 'ffmpeg', available: true, version: '7.1.1', installedMode: 'compatibility', releaseTag: 'n7.1.1' }];
  const updated: Tool[] = [{ name: 'ffmpeg', available: true, version: '8.0', installedMode: 'efficiency', releaseTag: 'n8.0', repository: 'official/efficiency', platform: 'windows-x86_64' }];
  const props = { state: { ...initialState, tools, settings: { ffmpegMode: 'efficiency' as const }, view: { ...initialState.view, settingsTab: 'tools', optionsDraft: { quality: '720p' } } }, dispatch, t: (_zh: string, en: string) => en, run, message };
  transport.command.mockImplementation(async name => name === 'tools.update' ? new Promise<void>(resolve => { finish = resolve; }) : name === 'tools.check' ? updated : []);
  const page = render(<SettingsPage {...props} />);
  fireEvent.change(screen.getByRole('textbox', { name: /FFmpeg exact release tag/ }), { target: { value: ' n8.0 ' } });
  fireEvent.click(screen.getByRole('button', { name: 'Apply FFmpeg version' }));
  await waitFor(() => expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'ffmpeg', mode: 'efficiency', version: 'n8.0' }));
  expect(screen.getByText('Current 7.1.1')).toBeTruthy();
  expect(screen.queryByText('Current 8.0')).toBeNull(); expect(message).not.toHaveBeenCalled();
  expect((screen.getByRole('combobox', { name: 'FFmpeg version policy' }) as HTMLSelectElement).disabled).toBe(true);
  await act(async () => finish());
  await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'event', event: { kind: 'tools.updated', data: updated } }));
  expect(message).toHaveBeenCalledWith('FFmpeg updated');
  expect(transport.command.mock.calls.some(([name]) => name === 'settings.save')).toBe(false);
  page.rerender(<SettingsPage {...props} state={{ ...props.state, tools: updated }} />);
  expect(screen.getByText('Current 8.0')).toBeTruthy();
  expect(screen.getByText(/Installed policy: Efficiency/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: /^(?:Update|Install)$/ })).toBeNull();
});

it('keeps the active FFmpeg build after a failed install and does not infer a policy for bundled tools', async () => {
  transport.desktop = true; const dispatch = vi.fn(); const message = vi.fn();
  transport.command.mockImplementation(async name => { if (name === 'tools.update') throw new Error('FFmpeg download failed'); return []; });
  render(<SettingsPage state={{ ...initialState, tools: [{ name: 'ffmpeg', available: true, version: 'bundled-7.0' }, { name: 'ffprobe', available: true, version: 'bundled-7.0' }], view: { ...initialState.view, settingsTab: 'tools' } }} dispatch={dispatch} t={(_zh, en) => en} run={run} message={message} />);
  fireEvent.click(screen.getByRole('button', { name: 'Apply FFmpeg version' }));
  expect((await screen.findByRole('alert')).textContent).toBeTruthy();
  expect(screen.getAllByText('Current bundled-7.0')).toHaveLength(2);
  expect(screen.queryByText(/Installed policy:/)).toBeNull();
  expect(transport.command).toHaveBeenCalledWith('tools.update', { name: 'ffmpeg', mode: 'compatibility' });
  expect(transport.command.mock.calls.some(([name]) => name === 'tools.check')).toBe(false);
  expect(dispatch).not.toHaveBeenCalled(); expect(message).not.toHaveBeenCalled();
  expect((screen.getByRole('button', { name: 'Apply FFmpeg version' }) as HTMLButtonElement).disabled).toBe(false);
});

it('discards an old FFmpeg release query when undoing a draft policy while the query is pending', async () => {
  transport.desktop = true; let finish!: (value: unknown) => void;
  const props = { state: { ...initialState, settings: { ffmpegMode: 'compatibility' as const }, view: { ...initialState.view, settingsTab: 'tools', settingsDraft: { ffmpegMode: 'efficiency' as const } } }, dispatch: vi.fn(), t: (_zh: string, en: string) => en, run, message: vi.fn() };
  transport.command.mockImplementation(async name => name === 'tools.latest' ? new Promise(resolve => { finish = resolve; }) : []);
  const page = render(<SettingsPage {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Check FFmpeg release' }));
  page.rerender(<SettingsPage {...props} state={{ ...props.state, view: { ...props.state.view, settingsDraft: null } }} />);
  await act(async () => finish({ name: 'ffmpeg', mode: 'efficiency', current: '7.1.1', latest: 'n8.0', platform: 'windows-x86_64', asset: 'ffmpeg.zip' }));
  expect(screen.queryByLabelText('Selected FFmpeg release')).toBeNull();
  expect((screen.getByRole('combobox', { name: 'FFmpeg version policy' }) as HTMLSelectElement).value).toBe('compatibility');
});
