import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream, readFileSync, writeFileSync, mkdirSync, mkdtempSync, readdirSync, chmodSync, copyFileSync, openSync, closeSync, rmSync, realpathSync } from 'node:fs';
import { dirname, basename, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const targets = ['windows-x86_64', 'linux-x86_64', 'linux-aarch64', 'windows-aarch64', 'darwin-x86_64', 'darwin-aarch64'];
const ffmpegSuffixes = ['-win64-gpl-shared.zip', '-linux64-gpl.tar.xz', '-linuxarm64-gpl.tar.xz', '-winarm64-gpl-shared.zip'];
export const isFfmpegDll = name => /^(?:avcodec|avdevice|avfilter|avformat|avutil|swresample|swscale)-\d+\.dll$/.test(name);

export function nativeTarget(platform = process.platform, arch = process.arch) {
  const os = { win32: 'windows', linux: 'linux', darwin: 'darwin' }[platform];
  const cpu = { x64: 'x86_64', arm64: 'aarch64' }[arch];
  if (!os || !cpu) throw new Error('Unsupported tool platform or architecture');
  return `${os}-${cpu}`;
}

export function toolSource(tool, target) {
  if (!targets.includes(target)) throw new Error('Unsupported tool target');
  const mac = target.startsWith('darwin-');
  const arm = target.endsWith('-aarch64');
  if (tool === 'yt-dlp') return { repository: 'yt-dlp/yt-dlp', name: mac ? 'yt-dlp_macos' : target.startsWith('windows-') ? arm ? 'yt-dlp_arm64.exe' : 'yt-dlp.exe' : arm ? 'yt-dlp_linux_aarch64' : 'yt-dlp_linux' };
  if (tool === 'ffmpeg' || tool === 'ffprobe') {
    if (mac) return { repository: 'shaka-project/static-ffmpeg-binaries', name: `${tool}-osx-${arm ? 'arm64' : 'x64'}` };
    if (tool === 'ffprobe') return null; // Included in the BtbN FFmpeg archive.
    return { repository: 'BtbN/FFmpeg-Builds', suffix: ffmpegSuffixes[targets.indexOf(target)] };
  }
  if (tool === 'qjs' && !mac && target !== 'windows-aarch64') return { repository: 'quickjs-ng/quickjs', name: `qjs-${target}${target.startsWith('windows-') ? '.exe' : ''}` };
  // QuickJS NG has no Windows ARM64 binary; its current macOS binaries require macOS 26.
  if (tool === 'deno' && (target === 'windows-aarch64' || mac)) return { repository: 'denoland/deno', name: mac ? `deno-${arm ? 'aarch64' : 'x86_64'}-apple-darwin.zip` : 'deno-aarch64-pc-windows-msvc.zip' };
  return null;
}

export function nativeExecutable(bytes, target) {
  if (!targets.includes(target) || bytes.length < 32) return false;
  const arm = target.endsWith('-aarch64');
  if (target.startsWith('windows-')) {
    if (bytes.length < 64 || bytes.subarray(0, 2).toString() !== 'MZ') return false;
    const offset = bytes.readUInt32LE(60);
    return offset <= bytes.length - 6 && bytes.readUInt32LE(offset) === 0x4550 && bytes.readUInt16LE(offset + 4) === (arm ? 0xaa64 : 0x8664);
  }
  if (target.startsWith('linux-')) return bytes.subarray(0, 6).equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46, 2, 1])) && bytes.readUInt16LE(18) === (arm ? 183 : 62);
  const magic = bytes.readUInt32BE(0);
  const cpu = arm ? 0x0100000c : 0x01000007;
  if (magic === 0xcffaedfe) return bytes.readUInt32LE(4) === cpu;
  if (magic === 0xfeedfacf) return bytes.readUInt32BE(4) === cpu;
  const fat64 = magic === 0xcafebabf || magic === 0xbfbafeca;
  const little = magic === 0xbebafeca || magic === 0xbfbafeca;
  if (!fat64 && magic !== 0xcafebabe && magic !== 0xbebafeca) return false;
  const u32 = offset => little ? bytes.readUInt32LE(offset) : bytes.readUInt32BE(offset);
  const count = u32(4), stride = fat64 ? 32 : 20;
  if (count < 1 || count > 64 || 8 + count * stride > bytes.length) return false;
  for (let index = 0; index < count; index++) {
    const entry = 8 + index * stride;
    if (u32(entry) !== cpu) continue;
    const offset = fat64 ? Number(little ? bytes.readBigUInt64LE(entry + 8) : bytes.readBigUInt64BE(entry + 8)) : u32(entry + 8);
    const size = fat64 ? Number(little ? bytes.readBigUInt64LE(entry + 16) : bytes.readBigUInt64BE(entry + 16)) : u32(entry + 12);
    return Number.isSafeInteger(offset) && Number.isSafeInteger(size) && offset >= 8 + count * stride && size >= 32 && offset <= bytes.length - size && nativeExecutable(bytes.subarray(offset, offset + size), target);
  }
  return false;
}

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
  const releases = new Map();
  for (const tool of ['yt-dlp', 'ffmpeg', 'qjs', 'ffprobe', 'deno']) for (const target of targets) {
    const source = toolSource(tool, target);
    if (!source) continue;
    const { repository } = source;
    const headers = { 'User-Agent': 'huifa-release', Accept: 'application/vnd.github+json' };
    if (process.env.GH_TOKEN) headers.Authorization = `Bearer ${process.env.GH_TOKEN}`;
    if (!releases.has(repository)) {
      const response = await fetch(`https://api.github.com/repos/${repository}/releases${repository === 'BtbN/FFmpeg-Builds' ? '?per_page=10' : '/latest'}`, { headers, signal: AbortSignal.timeout(30_000) });
      if (!response.ok) throw new Error(`Cannot resolve ${tool}: HTTP ${response.status}`);
      const payload = await response.json();
      releases.set(repository, repository === 'BtbN/FFmpeg-Builds' ? ffmpegRelease(payload).release : payload);
    }
    const release = releases.get(repository);
    const name = source.name ?? release.assets.find(asset => asset.name.startsWith('ffmpeg-N-') && asset.name.endsWith(source.suffix))?.name;
    const record = tools[tool] ??= { repository, version: release.tag_name, assets: {} };
    record.assets[target] = { ...assetDescriptor(repository, release, name), ...(repository === record.repository ? {} : { repository, version: release.tag_name }) };
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

export function runtimeEntries(entries, windows) {
  const filenames = [windows ? 'ffmpeg.exe' : 'ffmpeg', windows ? 'ffprobe.exe' : 'ffprobe'];
  for (const filename of filenames) archiveEntry(entries, filename);
  if (windows) {
    const libraries = entries.filter(entry => isFfmpegDll(basename(entry))).map(entry => basename(entry));
    for (const prefix of ['avcodec', 'avdevice', 'avfilter', 'avformat', 'avutil', 'swresample', 'swscale']) {
      if (libraries.filter(name => name.startsWith(prefix + '-')).length !== 1) throw new Error(`Expected one ${prefix} runtime DLL`);
    }
    filenames.push(...libraries);
  }
  return filenames;
}
function entries(archive) {
  const zip = archive.endsWith('.zip') && process.platform !== 'win32';
  const list = spawnSync(zip ? 'unzip' : 'tar', zip ? ['-Z1', archive] : ['-tf', archive], { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, shell: false });
  if (list.status !== 0) throw new Error(`Cannot inspect tool archive: ${list.stderr}`);
  return list.stdout.trim().split(/\r?\n/);
}
function extract(archive, filename, destination, members) {
  const zip = archive.endsWith('.zip') && process.platform !== 'win32';
  const entry = archiveEntry(members, filename);
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
  const native = nativeTarget();
  const receipt = {};
  const files = {};
  try {
    for (const tool of ['yt-dlp', 'ffmpeg', 'qjs', 'ffprobe', 'deno']) {
      const source = toolSource(tool, target);
      if (!source) continue;
      const { repository } = source;
      const record = lock.tools[tool];
      const asset = record?.assets?.[target];
      if (!asset || (asset.repository ?? record.repository) !== repository) throw new Error('Unexpected tool repository or missing platform asset');
      if (basename(asset.name) !== asset.name || asset.name.includes('\\')) throw new Error('Unsafe tool archive name');
      const url = new URL(asset.url);
      if (url.protocol !== 'https:' || url.host !== 'github.com' || !url.pathname.startsWith(`/${repository}/releases/download/`)) throw new Error('Unexpected tool download origin');
      const archive = join(temporary, 'archive-' + asset.name);
      const version = asset.version ?? record.version;
      console.log(`Acquiring ${tool} ${version} for ${target}`);
      await download(asset, archive);
      const archived = tool === 'ffmpeg' && !target.startsWith('darwin-') || asset.name.endsWith('.zip');
      const members = archived ? entries(archive) : [];
      const filenames = tool === 'ffmpeg' && !target.startsWith('darwin-') ? runtimeEntries(members, suffix === '.exe') : [tool + suffix];
      for (const filename of filenames) {
        const name = filename.replace(/\.exe$/, '');
        const staged = join(temporary, filename);
        if (!archived) copyFileSync(archive, staged); else extract(archive, filename, staged, members);
        chmodSync(staged, 0o755);
        const executable = readFileSync(staged);
        if (!nativeExecutable(executable, target)) throw new Error(`Tool payload is not a native executable for ${target}: ${filename}`);
        const sha256 = createHash('sha256').update(executable).digest('hex');
        files[filename] = sha256;
        if (!isFfmpegDll(filename)) receipt[name] = { version, sha256, archiveSha256: asset.sha256 };
      }
    }
    if (native === target) for (const name of Object.keys(receipt)) {
      const result = spawnSync(join(temporary, name + suffix), [name === 'ffmpeg' || name === 'ffprobe' ? '-version' : '--version'], { encoding: 'utf8', timeout: 30_000, shell: false });
      if (result.status !== 0) throw new Error(`${name} cannot run on this platform: ${result.stderr}`);
    }
    const previous = join(destination, 'manifest.json');
    if (readdirSync(destination).includes('manifest.json')) {
      const manifest = JSON.parse(readFileSync(previous, 'utf8'));
      for (const filename of Object.keys(manifest.files || Object.fromEntries(Object.keys(manifest.tools || {}).map(name => [name + suffix, ''])))) {
        if (/^(?:yt-dlp|ffmpeg|ffprobe|deno|qjs)(?:\.exe)?$/.test(filename) || isFfmpegDll(filename)) rmSync(join(destination, filename), { force: true });
      }
    }
    for (const filename of Object.keys(files)) { copyFileSync(join(temporary, filename), join(destination, filename)); chmodSync(join(destination, filename), 0o755); }
    writeFileSync(previous, JSON.stringify({ target, tools: receipt, files }, null, 2) + '\n');
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
    const target = process.argv[2] ?? nativeTarget();
    await acquire(JSON.parse(readFileSync('runtime-tool-lock.json', 'utf8')), target, resolve(process.argv[3] ?? 'runtime-tools'));
  }
}
