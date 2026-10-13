import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream, readFileSync, writeFileSync, mkdirSync, mkdtempSync, readdirSync, chmodSync, copyFileSync, openSync, closeSync, rmSync, realpathSync } from 'node:fs';
import { dirname, basename, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const targets = ['windows-x86_64', 'linux-x86_64', 'linux-aarch64'];
const repositories = { 'yt-dlp': 'yt-dlp/yt-dlp', ffmpeg: 'yt-dlp/FFmpeg-Builds', deno: 'denoland/deno' };
const names = {
  'yt-dlp': ['yt-dlp.exe', 'yt-dlp_linux', 'yt-dlp_linux_aarch64'],
  deno: ['deno-x86_64-pc-windows-msvc.zip', 'deno-x86_64-unknown-linux-gnu.zip', 'deno-aarch64-unknown-linux-gnu.zip'],
};
const ffmpegSuffixes = ['-win64-gpl.zip', '-linux64-gpl.tar.xz', '-linuxarm64-gpl.tar.xz'];

export function ffmpegRelease(releases) {
  const release = releases.find(item => /^autobuild-\d{4}-\d{2}-\d{2}-/.test(item.tag_name) && !item.draft);
  if (!release) throw new Error('A dated FFmpeg release is required');
  const assets = ffmpegSuffixes.map(suffix => {
    const matching = release.assets.filter(asset => asset.name.startsWith('ffmpeg-N-') && asset.name.endsWith(suffix));
    if (matching.length !== 1) throw new Error(`Expected one dated FFmpeg asset: ${suffix}`);
    return matching[0].name;
  });
  return { release, assets };
}

function assetDescriptor(repository, release, name) {
  const asset = release.assets.find(item => item.name === name);
  if (!asset || !/^sha256:[a-f0-9]{64}$/.test(asset.digest ?? '')) throw new Error(`A verified ${repository} asset is required: ${name}`);
  const url = new URL(asset.browser_download_url);
  if (url.protocol !== 'https:' || url.host !== 'github.com' || !url.pathname.startsWith(`/${repository}/releases/download/`)) throw new Error('Unexpected tool release origin');
  return { name, url: url.toString(), sha256: asset.digest.slice(7) };
}

export async function resolveLock() {
  const tools = {};
  for (const [tool, repository] of Object.entries(repositories)) {
    const headers = { 'User-Agent': 'huifa-release', Accept: 'application/vnd.github+json' };
    if (process.env.GH_TOKEN) headers.Authorization = `Bearer ${process.env.GH_TOKEN}`;
    const response = await fetch(`https://api.github.com/repos/${repository}/releases${tool === 'ffmpeg' ? '?per_page=10' : '/latest'}`, { headers, signal: AbortSignal.timeout(30_000) });
    if (!response.ok) throw new Error(`Cannot resolve ${tool}: HTTP ${response.status}`);
    const payload = await response.json();
    const selected = tool === 'ffmpeg' ? ffmpegRelease(payload) : { release: payload, assets: names[tool] };
    tools[tool] = { repository, version: selected.release.tag_name, assets: Object.fromEntries(targets.map((target, index) => [target, assetDescriptor(repository, selected.release, selected.assets[index])])) };
  }
  return { schemaVersion: 1, tools };
}

export function archiveEntry(entries, filename) {
  const matches = entries.filter(entry => {
    if (entry.startsWith('/') || entry.includes('\\') || entry.split('/').includes('..')) throw new Error('Unsafe tool archive entry');
    return basename(entry) === filename;
  });
  if (matches.length !== 1) throw new Error(`Expected one ${filename} in the verified archive`);
  return matches[0];
}

export async function download(asset, destination) {
  if (!/^[a-f0-9]{64}$/.test(asset.sha256)) throw new Error('Tool checksum is missing');
  let received;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const response = await fetch(asset.url, { signal: AbortSignal.timeout(300_000) });
      if (!response.ok || !response.body) throw new Error(`Tool download returned HTTP ${response.status}`);
      const digest = createHash('sha256');
      let size = 0;
      const verify = new Transform({ transform(chunk, _encoding, callback) {
        size += chunk.length;
        if (size > 512 * 1024 * 1024) return callback(new Error('Tool archive is too large'));
        digest.update(chunk); callback(null, chunk);
      } });
      await pipeline(Readable.fromWeb(response.body), verify, createWriteStream(destination, { flags: 'wx' }));
      received = digest.digest('hex');
      break;
    } catch (error) {
      rmSync(destination, { force: true });
      if (attempt === 2) throw error;
      await new Promise(resolveDelay => setTimeout(resolveDelay, 1000 * (attempt + 1)));
    }
  }
  if (received !== asset.sha256) {
    rmSync(destination, { force: true });
    throw new Error('Tool archive checksum does not match the pinned release');
  }
}

function extract(archive, filename, destination) {
  const zip = archive.endsWith('.zip') && process.platform !== 'win32';
  const list = spawnSync(zip ? 'unzip' : 'tar', zip ? ['-Z1', archive] : ['-tf', archive], { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, shell: false });
  if (list.status !== 0) throw new Error(`Cannot inspect tool archive: ${list.stderr}`);
  const entry = archiveEntry(list.stdout.trim().split(/\r?\n/), filename);
  const fd = openSync(destination, 'wx');
  try {
    const result = spawnSync(zip ? 'unzip' : 'tar', zip ? ['-p', archive, entry] : ['-xOf', archive, entry], { stdio: ['ignore', fd, 'pipe'], shell: false });
    if (result.status !== 0) throw new Error(`Cannot extract tool: ${result.stderr}`);
  } finally { closeSync(fd); }
}

export async function acquire(lock, target, destination) {
  if (lock.schemaVersion !== 1 || !targets.includes(target)) throw new Error('Unsupported tool target or lock format');
  const parent = realpathSync(tmpdir());
  const temporary = mkdtempSync(join(parent, 'huifa-rust-tools-'));
  mkdirSync(destination, { recursive: true });
  const suffix = target.startsWith('windows') ? '.exe' : '';
  const native = `${process.platform === 'win32' ? 'windows' : 'linux'}-${process.arch === 'arm64' ? 'aarch64' : 'x86_64'}`;
  const receipt = {};
  try {
    for (const [tool, repository] of Object.entries(repositories)) {
      const record = lock.tools[tool];
      if (record?.repository !== repository) throw new Error('Unexpected tool repository');
      const asset = record.assets[target];
      if (basename(asset.name) !== asset.name || asset.name.includes('\\')) throw new Error('Unsafe tool archive name');
      const url = new URL(asset.url);
      if (url.protocol !== 'https:' || url.host !== 'github.com' || !url.pathname.startsWith(`/${repository}/releases/download/`)) throw new Error('Unexpected tool download origin');
      const archive = join(temporary, 'archive-' + asset.name);
      console.log(`Acquiring ${tool} ${record.version} for ${target}`);
      await download(asset, archive);
      for (const name of tool === 'ffmpeg' ? ['ffmpeg', 'ffprobe'] : [tool]) {
        const filename = name + suffix;
        const staged = join(temporary, filename);
        if (tool === 'yt-dlp') copyFileSync(archive, staged); else extract(archive, filename, staged);
        chmodSync(staged, 0o755);
        const executable = readFileSync(staged);
        if (suffix ? executable.subarray(0, 2).toString() !== 'MZ' : !executable.subarray(0, 4).equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46]))) throw new Error('Tool payload is not a native executable');
        if (native === target) {
          const result = spawnSync(staged, [name === 'ffmpeg' || name === 'ffprobe' ? '-version' : '--version'], { encoding: 'utf8', timeout: 30_000, shell: false });
          if (result.status !== 0) throw new Error(`${name} cannot run on this platform: ${result.stderr}`);
        }
        copyFileSync(staged, join(destination, filename)); chmodSync(join(destination, filename), 0o755);
        receipt[name] = { version: record.version, sha256: createHash('sha256').update(executable).digest('hex'), archiveSha256: asset.sha256 };
      }
    }
    writeFileSync(join(destination, 'manifest.json'), JSON.stringify({ target, tools: receipt }, null, 2) + '\n');
  } finally {
    if (dirname(resolve(temporary)) !== resolve(parent) || !basename(temporary).startsWith('huifa-rust-tools-')) throw new Error('Unsafe temporary cleanup path');
    rmSync(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.includes('--self-test')) {
    assert.equal(archiveEntry(['build/bin/ffmpeg.exe', 'build/bin/ffprobe.exe'], 'ffmpeg.exe'), 'build/bin/ffmpeg.exe');
    assert.throws(() => archiveEntry(['../../ffmpeg.exe'], 'ffmpeg.exe'), /Unsafe/);
    assert.throws(() => archiveEntry(['one/ffmpeg.exe', 'two/ffmpeg.exe'], 'ffmpeg.exe'), /Expected one/);
    assert.throws(() => assetDescriptor('owner/repo', { assets: [{ name: 'tool', digest: 'sha256:' + 'f'.repeat(64), browser_download_url: 'https://evil.example/tool' }] }, 'tool'), /origin/);
    assert.throws(() => assetDescriptor('owner/repo', { assets: [{ name: 'tool' }] }, 'tool'), /verified/);
    console.log('Verified tool acquisition self-check passed');
  } else if (process.argv.includes('--lock')) {
    writeFileSync('runtime-tool-lock.json', JSON.stringify(await resolveLock(), null, 2) + '\n');
  } else {
    const target = process.argv[2] ?? `${process.platform === 'win32' ? 'windows' : 'linux'}-${process.arch === 'arm64' ? 'aarch64' : 'x86_64'}`;
    await acquire(JSON.parse(readFileSync('runtime-tool-lock.json', 'utf8')), target, resolve(process.argv[3] ?? 'runtime-tools'));
  }
}
