import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { delimiter, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const rootDirectory = fileURLToPath(new URL('../', import.meta.url));
const installScript = join(rootDirectory, 'docker/install-tools.sh');
const checkedIn = JSON.parse(readFileSync(join(rootDirectory, 'runtime-tool-lock.json'), 'utf8'));
const shell = process.platform === 'win32' ? 'C:/Program Files/Git/usr/bin/sh.exe' : 'sh';
const available = spawnSync(shell, ['-c', 'command -v tar && command -v xz && command -v sha256sum && command -v awk'], { encoding: 'utf8' }).status === 0;
const portablePath = value => value.replaceAll('\\', '/');
const sha256 = value => createHash('sha256').update(value).digest('hex');

function executable(path, source) {
  writeFileSync(path, source);
  chmodSync(path, 0o755);
}

function fixture(t, { architecture = 'amd64', missingProbe = false, wrongVersion = false, missingHevc = false, corruptChecksum = false, wrongOrigin = false } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'huifa-docker-tools-'));
  t.after(() => {
    assert.equal(dirname(root), resolve(tmpdir()));
    assert.ok(root.startsWith(join(resolve(tmpdir()), 'huifa-docker-tools-')));
    rmSync(root, { recursive: true, force: true });
  });
  const commands = join(root, 'commands');
  const destination = join(root, 'installed');
  const scratch = join(root, 'scratch');
  for (const directory of [commands, destination, scratch]) mkdirSync(directory);
  const platform = architecture === 'arm64' ? 'linux-aarch64' : 'linux-x86_64';
  const lock = structuredClone(checkedIn);
  const archiveRoot = lock.tools.ffmpeg.assets[platform].name.replace(/\.tar\.xz$/, '');
  const payload = join(root, 'payload');
  const bin = join(payload, archiveRoot, 'bin');
  mkdirSync(bin, { recursive: true });
  const encoders = ['libx264', ...missingHevc ? [] : ['libx265'], 'libvpx-vp9', 'libopus'];
  executable(join(bin, 'ffmpeg'), `#!/bin/sh
if [ "$1" = -version ]; then echo 'ffmpeg version fixture-7.1.3'; exit 0; fi
if [ "$2" = -encoders ]; then
${encoders.map(encoder => `  echo ' V..... ${encoder} fixture encoder'`).join('\n')}
  exit 0
fi
exit 1
`);
  if (!missingProbe) executable(join(bin, 'ffprobe'), `#!/bin/sh\necho 'ffprobe version fixture-${wrongVersion ? '8.0' : '7.1.3'}'\n`);
  const archive = join(root, 'ffmpeg.tar.xz');
  const packed = spawnSync('tar', ['-cJf', archive, '-C', payload, archiveRoot], { encoding: 'utf8' });
  assert.equal(packed.status, 0, packed.stderr || packed.error?.message);
  const downloads = {};
  for (const tool of ['yt-dlp', 'qjs', 'ffmpeg']) {
    const asset = lock.tools[tool].assets[platform];
    const source = tool === 'ffmpeg' ? archive : join(root, tool + '.fixture');
    if (tool !== 'ffmpeg') executable(source, `#!/bin/sh\necho '${tool} fixture'\n`);
    asset.sha256 = tool === 'ffmpeg' && corruptChecksum ? '0'.repeat(64) : sha256(readFileSync(source));
    downloads[asset.url] = portablePath(source);
  }
  if (wrongOrigin) lock.tools.ffmpeg.assets[platform].url = 'https://example.invalid/ffmpeg.tar.xz';
  const lockPath = join(root, 'lock.json');
  const downloadMap = join(root, 'downloads.json');
  const requests = join(root, 'requests.txt');
  writeFileSync(lockPath, JSON.stringify(lock));
  writeFileSync(downloadMap, JSON.stringify(downloads));
  const curlHelper = join(root, 'curl.mjs');
  writeFileSync(curlHelper, `import fs from 'node:fs';
const args = process.argv.slice(2), url = args.find(value => value.startsWith('https://'));
const sources = JSON.parse(fs.readFileSync(process.env.HUIFA_TEST_DOWNLOADS, 'utf8'));
if (!sources[url]) throw new Error('Unexpected download: ' + url);
fs.appendFileSync(process.env.HUIFA_TEST_REQUESTS, url + '\\n');
fs.copyFileSync(sources[url], args[args.indexOf('-o') + 1]);
`);
  executable(join(commands, 'curl'), '#!/bin/sh\nexec "$HUIFA_TEST_NODE" "$HUIFA_TEST_CURL" "$@"\n');
  executable(join(commands, 'chromium'), '#!/bin/sh\necho "Chromium fixture"\n');
  if (spawnSync('jq', ['--version'], { encoding: 'utf8' }).status !== 0) {
    const jqHelper = join(root, 'jq.mjs');
    writeFileSync(jqHelper, `import fs from 'node:fs';
const args = process.argv.slice(2), vars = {};
for (let index = 0; index < args.length; index++) if (args[index] === '--arg') { vars[args[index + 1]] = args[index + 2]; index += 2; }
const lock = JSON.parse(fs.readFileSync(args.at(-1), 'utf8')), expression = args.at(-2);
let value;
if (expression === '.schemaVersion') value = lock.schemaVersion;
else if (expression.endsWith('.repository')) value = lock.tools[vars.tool].repository;
else if (expression.includes('.version //')) value = lock.tools[vars.tool].assets[vars.target].version ?? lock.tools[vars.tool].version;
else value = lock.tools[vars.tool].assets[vars.target][expression.split('.').at(-1)];
console.log(value ?? 'null');
`);
    executable(join(commands, 'jq'), '#!/bin/sh\nexec "$HUIFA_TEST_NODE" "$HUIFA_TEST_JQ" "$@"\n');
  }
  const environment = {
    ...process.env,
    PATH: commands + delimiter + process.env.PATH,
    TMPDIR: portablePath(scratch),
    HUIFA_TEST_NODE: portablePath(process.execPath),
    HUIFA_TEST_CURL: portablePath(curlHelper),
    HUIFA_TEST_JQ: portablePath(join(root, 'jq.mjs')),
    HUIFA_TEST_DOWNLOADS: portablePath(downloadMap),
    HUIFA_TEST_REQUESTS: portablePath(requests),
  };
  return {
    destination, requests, lock, platform,
    run: () => spawnSync(shell, [portablePath(installScript), architecture, portablePath(lockPath), portablePath(destination)], { cwd: rootDirectory, env: environment, encoding: 'utf8', timeout: 30_000 }),
  };
}

test('Docker builder includes the compatibility lock and runtime installs the pinned media tools', () => {
  const docker = readFileSync(join(rootDirectory, 'Dockerfile'), 'utf8');
  const builder = docker.split(/FROM rust:[^\n]+ AS rust/)[1].split(/FROM debian:/)[0];
  assert.match(builder, /^COPY[^\n]*runtime-tool-lock\.json[^\n]*$/m);
  const packages = docker.match(/apt-get install[^\n]+/)[0];
  assert.doesNotMatch(packages, /\bffmpeg\b/);
  assert.match(packages, /\bxz-utils\b/);
  assert.match(docker, /sh \/tmp\/install-tools\.sh "\$TARGETARCH" \/tmp\/runtime-tool-lock\.json/);
});

test('container tool installer is valid POSIX shell', { skip: !available }, () => {
  const result = spawnSync(shell, ['-n', portablePath(installScript)], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
});

for (const architecture of ['amd64', 'arm64']) {
  test(`container installer verifies and installs the ${architecture} FFmpeg/FFprobe pair with HEVC`, { skip: !available }, t => {
    const setup = fixture(t, { architecture });
    const result = setup.run();
    assert.equal(result.status, 0, result.stdout + result.stderr + (result.error?.message || ''));
    for (const tool of ['yt-dlp', 'qjs', 'ffmpeg', 'ffprobe']) assert.ok(existsSync(join(setup.destination, tool)), tool);
    assert.match(readFileSync(join(setup.destination, 'ffmpeg'), 'utf8'), /libx265/);
    assert.deepEqual(readFileSync(setup.requests, 'utf8').trim().split('\n'), ['yt-dlp', 'qjs', 'ffmpeg'].map(tool => setup.lock.tools[tool].assets[setup.platform].url));
  });
}

for (const [reason, options] of [
  ['checksum mismatch', { corruptChecksum: true }],
  ['untrusted source', { wrongOrigin: true }],
  ['missing FFprobe', { missingProbe: true }],
  ['mismatched versions', { wrongVersion: true }],
  ['missing HEVC', { missingHevc: true }],
]) {
  test(`container installer preserves the existing media pair on ${reason}`, { skip: !available }, t => {
    const setup = fixture(t, options);
    for (const tool of ['ffmpeg', 'ffprobe']) writeFileSync(join(setup.destination, tool), `original ${tool}`);
    const result = setup.run();
    assert.notEqual(result.status, 0, result.stdout + result.stderr);
    for (const tool of ['ffmpeg', 'ffprobe']) assert.equal(readFileSync(join(setup.destination, tool), 'utf8'), `original ${tool}`);
  });
}
