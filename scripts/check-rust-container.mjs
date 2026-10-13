import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname, basename } from 'node:path';
import { randomUUID } from 'node:crypto';

const image = process.env.HUIFA_IMAGE ?? 'huifa-rust:local';
const platform = process.env.HUIFA_PLATFORM ?? 'linux/amd64';
const name = 'huifa-check-' + randomUUID();
const dataVolume = name + '-data';
const downloadVolume = name + '-downloads';
const temporary = mkdtempSync(join(tmpdir(), 'huifa-container-check-'));
const account = 'container-acceptance';
const password = randomUUID() + randomUUID();
const cookieCanary = 'cookie-acceptance-' + randomUUID();
const keyCanary = 'ai-acceptance-' + randomUUID();
let source;
let origin;
let sessionCookie;
let csrf;

function docker(args, allowFailure = false) {
  const result = spawnSync('docker', args, { encoding: 'utf8', timeout: 300_000, maxBuffer: 4 * 1024 * 1024, shell: false });
  if (!allowFailure && result.status !== 0) throw new Error(`Container command failed: ${result.stderr || result.error?.message}`);
  return result.stdout.trim();
}
const delay = milliseconds => new Promise(resolveDelay => setTimeout(resolveDelay, milliseconds));
async function waitFor(check, timeout = 120_000) {
  const end = Date.now() + timeout;
  let error;
  while (Date.now() < end) {
    try { const value = await check(); if (value) return value; } catch (failure) { error = failure; }
    await delay(500);
  }
  throw error ?? new Error('Container acceptance timed out');
}
async function request(path, body, extra = {}) {
  const headers = { Origin: origin };
  if (sessionCookie) headers.Cookie = sessionCookie;
  if (csrf) headers['X-CSRF-Token'] = csrf;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  Object.assign(headers, extra);
  return fetch(origin + path, { method: body === undefined ? 'GET' : 'POST', headers, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(120_000) });
}
async function command(commandName, args = {}) {
  const response = await request('/api/command', { command: commandName, args });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error ?? `Command returned HTTP ${response.status}`);
  return payload.value;
}
async function start() {
  const options = ['run', '-d', '--name', name, '--platform', platform, '--read-only', '--tmpfs', '/tmp:rw,exec,nosuid,nodev,size=536870912', '--shm-size', '256m', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
    '-p', '127.0.0.1::19491', '-v', `${dataVolume}:/data`, '-v', `${downloadVolume}:/downloads`,
    '-e', `HUIFA_ACCOUNT=${account}`, '-e', `HUIFA_PASSWORD=${password}`];
  if (process.platform !== 'win32') options.push('--add-host', 'host.docker.internal:host-gateway');
  docker([...options, image]);
  const mapping = docker(['port', name, '19491/tcp']).split(/\r?\n/)[0];
  assert.match(mapping, /^127\.0\.0\.1:\d+$/);
  origin = 'http://' + mapping;
  await waitFor(async () => (await fetch(origin + '/api/session', { signal: AbortSignal.timeout(2000) })).status === 401);
  assert.equal(docker(['exec', name, 'id', '-u']), '10001');
}
async function login() {
  sessionCookie = undefined; csrf = undefined;
  const response = await request('/api/login', { account, password });
  assert.equal(response.status, 200);
  const cookie = response.headers.get('set-cookie');
  assert.match(cookie, /HttpOnly/); assert.match(cookie, /SameSite=Strict/);
  sessionCookie = cookie.split(';')[0];
  csrf = (await response.json()).csrfToken;
  assert.ok(csrf?.length >= 32);
}

try {
  docker(['volume', 'create', dataVolume]); docker(['volume', 'create', downloadVolume]);
  await start();
  assert.equal((await request('/api/login', { account, password }, { Origin: 'http://other.example' })).status, 403);
  assert.equal((await request('/api/events')).status, 401);
  await login();
  assert.equal((await request('/api/command', { command: 'snapshot', args: {} }, { 'X-CSRF-Token': 'incorrect' })).status, 403);
  assert.equal((await request('/api/command', { command: 'media.file', args: { id: randomUUID() } })).status, 403);
  assert.equal((await request('/api/command', { command: 'publish.submit', args: {} })).status, 403);
  assert.equal((await request('/api/command', { command: 'tasks.add', args: { urls: ['http://example.com/video.mp4'], options: { processingTempDir: '/etc' } } })).status, 400);
  const credentials = await command('cookies.import', { name: 'Acceptance', text: `# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsession\t${cookieCanary}\n` });
  assert.ok(credentials.id);
  await command('settings.save', { settings: { aiApiKey: keyCanary, aiBaseUrl: 'https://api.openai.com/v1', aiModel: 'gpt-image-2' } });
  const snapshot = await command('snapshot');
  assert.equal(snapshot.version, process.env.RELEASE_VERSION ?? snapshot.version);
  assert.equal(snapshot.settings.hasAiApiKey, true);
  assert.ok(!JSON.stringify(snapshot).includes(keyCanary));
  assert.ok(!JSON.stringify(await command('cookies.list')).includes(cookieCanary));
  assert.ok(snapshot.tools.find(tool => tool.name === 'yt-dlp' && tool.available));
  assert.ok(snapshot.tools.find(tool => tool.name === 'ffmpeg' && tool.available));

  docker(['exec', name, 'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=24', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', '-y', '/downloads/source-fixture.mp4']);
  docker(['cp', `${name}:/downloads/source-fixture.mp4`, join(temporary, 'source.mp4')]);
  const clip = readFileSync(join(temporary, 'source.mp4'));
  source = createServer((request, response) => {
    if (request.url !== '/clip.mp4') { response.writeHead(404).end(); return; }
    const range = request.headers.range?.match(/^bytes=(\d+)-(\d*)$/);
    let start = range ? Number(range[1]) : 0;
    let end = range?.[2] ? Math.min(Number(range[2]), clip.length - 1) : clip.length - 1;
    if (start > end || start >= clip.length) { response.writeHead(416, { 'Content-Range': `bytes */${clip.length}` }).end(); return; }
    const headers = { 'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes', 'Content-Length': end - start + 1 };
    if (range) headers['Content-Range'] = `bytes ${start}-${end}/${clip.length}`;
    response.writeHead(range ? 206 : 200, headers);
    response.end(request.method === 'HEAD' ? undefined : clip.subarray(start, end + 1));
  });
  await new Promise(resolveListen => source.listen(0, '0.0.0.0', resolveListen));
  const sourceUrl = `http://host.docker.internal:${source.address().port}/clip.mp4`;
  const ids = await command('tasks.add', { urls: [sourceUrl], options: { container: 'mp4', encoder: 'libx264', writeThumbnail: false, writeInfoJson: false } });
  assert.equal(ids.length, 1);
  const finished = await waitFor(async () => {
    const next = await command('snapshot');
    const task = next.tasks.find(task => task.id === ids[0]);
    if (['failed', 'processing_failed'].includes(task?.status)) throw new Error(`Actual media failed: ${task.error}`);
    return task?.status === 'completed' ? next : null;
  }, 180_000);
  const media = finished.media.find(item => item.taskId === ids[0]); assert.ok(media);
  const file = await request(`/api/files/${media.id}`); assert.equal(file.status, 200);
  assert.ok((await file.arrayBuffer()).byteLength > 1000);
  await new Promise(resolveClose => source.close(resolveClose)); source = undefined;

  // Chromium uses only the same authenticated port; its internal CDP never binds publicly.
  const browserResponse = await request('/api/browser/start', { url: 'https://example.com' });
  assert.equal(browserResponse.status, 200);
  await request('/api/browser/stop', {});
  const before = finished.media.length;
  docker(['stop', '--time', '30', name]); docker(['rm', name]);
  await start(); await login();
  const restored = await command('snapshot');
  assert.equal(restored.media.length, before);
  assert.ok(restored.tasks.find(task => task.id === ids[0] && task.status === 'completed'));
  assert.equal(restored.settings.hasAiApiKey, true);
  assert.ok((await command('cookies.list')).find(item => item.id === credentials.id));
  assert.equal((await request(`/api/files/${media.id}`)).status, 200);
  const logs = docker(['logs', name]);
  assert.ok(!logs.includes(password) && !logs.includes(cookieCanary) && !logs.includes(keyCanary));
  console.log(`Container acceptance passed: ${platform}, authentication, real download/transcode/result, browser startup, volume restart`);
} finally {
  if (source) await new Promise(resolveClose => source.close(resolveClose));
  docker(['rm', '-f', name], true);
  docker(['volume', 'rm', dataVolume, downloadVolume], true);
  if (dirname(resolve(temporary)) !== resolve(tmpdir()) || !basename(temporary).startsWith('huifa-container-check-')) throw new Error('Unsafe acceptance cleanup path');
  rmSync(temporary, { recursive: true, force: true });
}
