import { app, BrowserWindow, clipboard, ClipboardItem, dialog, ipcMain, Menu, nativeImage, nativeTheme, net, Notification, protocol, shell, Tray } from 'electron';
import { ChildProcessWithoutNullStreams, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { externalUrl, lifecycleArguments, MAX_PENDING, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, validateCommand } from './protocol';

let window: BrowserWindow | null = null;
let tray: Tray | null = null;
let backend: ChildProcessWithoutNullStreams | null = null;
let closing = false;
let backendExited = false;
let shutdownReady = false;
let nextId = 1;
let ready = false;
let rendererLoaded = false;
let smokeRunning = false;
let receiveBuffer = Buffer.alloc(0);
let latestState: Record<string, any> = {};
const pending = new Map<number, { resolve: (value: any) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }>();
const root = app.isPackaged ? path.dirname(process.execPath) : path.resolve(__dirname, '../..');
const backendExe = path.join(root, 'HuifaPythonBackend.exe');
const rendererUrl = pathToFileURL(path.join(__dirname, '../index.html')).href;
const assets = new Map<string, string>();
protocol.registerSchemesAsPrivileged([{ scheme: 'huifa-media', privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } }]);
const managedRoot = path.basename(root).toLowerCase() === 'current' && fs.existsSync(path.join(root, 'sq.version')) && fs.existsSync(path.join(root, '../Update.exe')) ? path.dirname(root) : null;
const dataRoot = process.env.HUIFA_DATA_ROOT || (managedRoot ? fs.existsSync(path.join(managedRoot, '.portable')) ? path.join(managedRoot, 'data') : process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, 'Huifa Video Downloader') : path.join(managedRoot, 'data') : path.join(root, 'data'));
const shellData = path.join(dataRoot, 'desktop');
fs.mkdirSync(shellData, { recursive: true });
app.setName('Huifa Media Downloader');
app.setPath('userData', shellData);
app.setPath('sessionData', path.join(shellData, 'session'));

function showWindow() {
  window?.show();
  if (window?.isMinimized()) window.restore();
  window?.focus();
}

function sendEvent(event: string, data: any) {
  window?.webContents.send('huifa:event', { event, data });
}

function failPending(message: string) {
  for (const item of pending.values()) {
    clearTimeout(item.timer);
    item.reject(new Error(message));
  }
  pending.clear();
}

function request(command: { target: string; action: string; args: unknown[] }) {
  if (!backend || backendExited || !backend.stdin.writable) return Promise.reject(new Error('Python backend is unavailable'));
  if (pending.size >= MAX_PENDING) return Promise.reject(new Error('Too many pending requests'));
  const id = nextId++;
  const encoded = JSON.stringify({ id, ...command }) + '\n';
  if (Buffer.byteLength(encoded) > MAX_REQUEST_BYTES) return Promise.reject(new Error('Command is too large'));
  return new Promise<any>((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('Backend request timed out')); }, command.action === 'requestShutdown' ? 30000 : 120000);
    pending.set(id, { resolve, reject, timer });
    backend!.stdin.write(encoded, 'utf8', error => {
      if (error) { clearTimeout(timer); pending.delete(id); reject(new Error('Unable to send backend request')); }
    });
  });
}

function updateNativeSettings() {
  const values = latestState.settings?.values || {};
  const theme = values.appearance_theme;
  nativeTheme.themeSource = ['dark', 'light', 'system'].includes(theme) ? theme : 'system';
  if (tray) {
    if (values.desktop_notifications === 'false') tray.destroy();
    if (tray.isDestroyed()) tray = null;
  } else if (values.desktop_notifications !== 'false') createTray();
}

function createTray() {
  const iconPath = path.join(root, 'assets/huifa.ico');
  if (!fs.existsSync(iconPath)) return;
  tray = new Tray(nativeImage.createFromPath(iconPath));
  tray.setToolTip('Huifa Media Downloader');
  tray.setContextMenu(Menu.buildFromTemplate([{ label: '显示主窗口', click: showWindow }, { type: 'separator' }, { label: '退出', click: () => void gracefulQuit() }]));
  tray.on('double-click', showWindow);
}

async function gracefulQuit() {
  if (closing) return;
  closing = true;
  sendEvent('closing', {});
  if (backendExited || !backend) { app.quit(); return; }
  try { await request({ target: 'app', action: 'requestShutdown', args: [] }); }
  catch (error) {
    closing = false;
    dialog.showErrorBox('无法安全退出', String(error));
  }
}

function onMessage(message: any) {
  if (message.type === 'result') {
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    if (message.ok) item.resolve(message.value); else item.reject(new Error(String(message.error || 'Backend command failed')));
    return;
  }
  if (message.type === 'fatal' || message.type === 'protocolError') {
    sendEvent('backendError', { message: String(message.error || 'Backend failed') });
    if (message.type === 'fatal') {
      const destination = process.env.HUIFA_PACKAGED_SMOKE_OUTPUT;
      if (destination) { fs.mkdirSync(path.dirname(destination), { recursive: true }); fs.writeFileSync(destination, JSON.stringify({ ok: false, error: String(message.error) })); closing = true; }
      else dialog.showErrorBox('后台服务启动失败', String(message.error));
    }
    return;
  }
  if (message.type !== 'event' || typeof message.event !== 'string') return;
  const data = message.data;
  if (message.event === 'ready') { ready = true; void runSmoke(); }
  if (message.event === 'state') {
    latestState = { ...latestState, ...data };
    updateNativeSettings();
  }
  if (message.event === 'asset' && /^[a-f0-9]{32}$/.test(data.token) && typeof data.path === 'string' && path.isAbsolute(data.path) && /\.(jpg|jpeg|png|webp|gif|bmp)$/i.test(data.path)) {
    assets.set(data.token, data.path);
    return;
  }
  if (message.event === 'native') {
    if (data.action === 'copy' && typeof data.text === 'string') clipboard.writeText(data.text);
    else if (data.action === 'copyImage' && typeof data.png === 'string' && data.png.length < MAX_RESPONSE_BYTES) {
      const image = nativeImage.createFromBuffer(Buffer.from(data.png, 'base64'));
      if (!image.isEmpty()) void clipboard.write([new ClipboardItem({ 'image/png': new Blob([new Uint8Array(image.toPNG())], { type: 'image/png' }) })]).catch(error => sendEvent('backendError', { message: String(error) }));
    }
    else if (data.action === 'reveal' && typeof data.path === 'string' && path.isAbsolute(data.path)) shell.showItemInFolder(data.path);
    else if (data.action === 'openDirectory' && typeof data.path === 'string' && path.isAbsolute(data.path)) void shell.openPath(data.path).then(error => { if (error) sendEvent('backendError', { message: error }); });
    return;
  }
  if (message.event === 'notification' && Notification.isSupported()) {
    const notification = new Notification({ title: String(data.title || ''), body: String(data.body || '') });
    notification.on('click', () => { showWindow(); sendEvent(data.kind === 'download' ? 'taskFocus' : 'publishTaskFocus', [data.id]); });
    notification.show();
  }
  if (message.event === 'activate') showWindow();
  if (message.event === 'shutdown') { shutdownReady = true; closing = true; }
  sendEvent(message.event, data);
}

function startBackend() {
  const isFrozen = fs.existsSync(backendExe);
  const python = process.env.HUIFA_PYTHON || path.join(root, '.venv/Scripts/python.exe');
  const command = isFrozen ? backendExe : python;
  const lifecycleArgs = lifecycleArguments(process.argv.slice(1));
  const args = isFrozen ? lifecycleArgs : ['-m', 'app.electron_backend'];
  backend = spawn(command, args, { cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, HUIFA_APPLICATION_ROOT: root, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1', QT_QPA_PLATFORM: 'offscreen' } });
  backend.stdout.on('data', (chunk: Buffer) => {
    receiveBuffer = Buffer.concat([receiveBuffer, chunk]);
    let newline: number;
    while ((newline = receiveBuffer.indexOf(10)) >= 0) {
      if (newline > MAX_RESPONSE_BYTES) { failPending('Backend message exceeds limit'); void gracefulQuit(); return; }
      const line = receiveBuffer.subarray(0, newline).toString('utf8');
      receiveBuffer = receiveBuffer.subarray(newline + 1);
      try { onMessage(JSON.parse(line)); }
      catch { sendEvent('backendError', { message: 'Invalid backend response' }); }
    }
    if (receiveBuffer.length > MAX_RESPONSE_BYTES) { failPending('Backend message exceeds limit'); void gracefulQuit(); }
  });
  // Python diagnostics stay private; do not forward cookies, auth or raw logs to Chromium.
  backend.stderr.on('data', () => {});
  backend.on('error', error => {
    backendExited = true;
    failPending('Unable to start Python backend');
    sendEvent('backendDisconnected', { message: error.message });
    sendEvent('backendError', { message: error.message });
    dialog.showErrorBox('后台服务启动失败', error.message);
  });
  backend.on('exit', (code) => {
    backendExited = true;
    failPending('Python backend exited');
    if (shutdownReady || closing && code === 0) app.quit();
    else { closing = false; sendEvent('backendDisconnected', { message: `Python后台已退出 (${code})。请重新启动应用，已保存任务会恢复。` }); }
  });
}

function verifySender(event: Electron.IpcMainInvokeEvent) {
  if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame || event.senderFrame.url !== rendererUrl) throw new Error('Untrusted IPC sender');
}

function configureIpc() {
  ipcMain.handle('huifa:command', async (event, value) => {
    verifySender(event);
    const command = validateCommand(value);
    if (command.target === 'host' && command.action === 'snapshot') {
      if (ready) return request(command);
      if (Object.keys(latestState).length) return latestState;
    }
    return request(command);
  });
  ipcMain.handle('huifa:path', async (event, value: PathOptions) => {
    verifySender(event);
    if (!value || !['directory', 'file', 'save'].includes(value.kind)) throw new Error('Invalid dialog');
    if (value.title !== undefined && typeof value.title !== 'string' || value.defaultPath !== undefined && typeof value.defaultPath !== 'string') throw new Error('Invalid dialog arguments');
    const options = { title: value.title?.slice(0, 200), defaultPath: value.defaultPath, filters: Array.isArray(value.filters) ? value.filters.filter(filter => typeof filter.name === 'string' && Array.isArray(filter.extensions) && filter.extensions.every(ext => /^[a-z0-9*]+$/i.test(ext))).slice(0, 10) : undefined };
    if (value.kind === 'save') return (await dialog.showSaveDialog(window!, options)).filePath || '';
    const result = await dialog.showOpenDialog(window!, { ...options, properties: [value.kind === 'directory' ? 'openDirectory' : 'openFile'] });
    return result.canceled ? '' : result.filePaths[0] || '';
  });
  ipcMain.handle('huifa:external', async (event, url) => { verifySender(event); await shell.openExternal(externalUrl(url)); });
}

async function createWindow() {
  window = new BrowserWindow({ width: 1280, height: 830, minWidth: 850, minHeight: 600, title: 'Huifa Media Downloader', icon: path.join(root, 'assets/huifa.ico'), backgroundColor: '#111820', webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true } });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, url) => { if (url !== rendererUrl) event.preventDefault(); });
  window.webContents.on('did-finish-load', () => { rendererLoaded = true; if (Object.keys(latestState).length) sendEvent('state', latestState); void runSmoke(); });
  window.on('close', event => { if (!backendExited) { event.preventDefault(); void gracefulQuit(); } });
  Menu.setApplicationMenu(Menu.buildFromTemplate([{ label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] }, { label: '视图', submenu: [{ role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'resetZoom' }, { role: 'togglefullscreen' }] }]));
  await window.loadURL(rendererUrl);
}

async function runSmoke() {
  const destination = process.env.HUIFA_PACKAGED_SMOKE_OUTPUT;
  if (!destination || !ready || !rendererLoaded || smokeRunning) return;
  smokeRunning = true;
  try {
    const health = await request({ target: 'host', action: 'health', args: [] });
    const state = await request({ target: 'host', action: 'snapshot', args: [] });
    const renderer = await window!.webContents.executeJavaScript(`({ loaded: document.querySelector('nav') !== null, nodeExposed: typeof require !== 'undefined' || typeof process !== 'undefined', bodyText: document.body.innerText.slice(0, 300) })`);
    const unavailable = new Set(['', '未安装', '未检测', '不可用']);
    const toolsReady = ['yt_dlp', 'ffmpeg', 'ffprobe'].every(name => health[name] && !unavailable.has(String(health[name].version || '')));
    const packageReady = !app.isPackaged || health.frozen && path.basename(health.executable).toLowerCase() === 'huifapythonbackend.exe' && path.basename(process.execPath).toLowerCase() === 'huifavideodownloader.exe' && health.application_update_mode === 'velopack' && health.application_version === health.expected_application_version && health.organization_name === health.expected_organization_name && health.secure_store_backend === 'keyring.backends.Windows.WinVaultKeyring';
    const report = { ...health, frontend_executable: process.execPath, electron_version: process.versions.electron, renderer_loaded: renderer.loaded, node_exposed: renderer.nodeExposed, backend_connected: true, task_count: state.tasks.count, application_status: state.updates.applicationStatus, ok: Boolean(renderer.loaded && !renderer.nodeExposed && health.yt_dlp.core_ready && toolsReady && packageReady) };
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.writeFileSync(destination, JSON.stringify(report, null, 2));
  } catch (error) {
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.writeFileSync(destination, JSON.stringify({ ok: false, error: String(error) }));
  }
  await gracefulQuit();
}

const veloArgs = process.argv.filter(argument => /^--veloapp-(install|updated|obsolete|uninstall)(?:=|$)/.test(argument));
if (veloArgs.length && fs.existsSync(backendExe)) {
  const hook = spawn(backendExe, process.argv.slice(1), { cwd: root, windowsHide: true, env: { ...process.env, HUIFA_APPLICATION_ROOT: root }, stdio: 'ignore' });
  hook.on('exit', code => app.exit(code || 0));
} else if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', showWindow);
  app.on('before-quit', event => { if (!backendExited) { event.preventDefault(); void gracefulQuit(); } });
  app.on('window-all-closed', () => { if (backendExited) app.quit(); });
  app.whenReady().then(async () => {
    protocol.handle('huifa-media', request => {
      const url = new URL(request.url);
      const token = url.pathname.slice(1);
      const imagePath = url.hostname === 'image' && /^[a-f0-9]{32}$/.test(token) ? assets.get(token) : undefined;
      return imagePath ? net.fetch(pathToFileURL(imagePath).href) : new Response('Unknown image', { status: 404 });
    });
    configureIpc(); startBackend(); await createWindow();
  });
}
