import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { download, ffmpegRelease, runtimeEntries, nativeExecutable, nativeTarget, toolSource, targets } from '../scripts/acquire-rust-tools.mjs';

test('FFmpeg uses dated assets rather than a moving latest URL', () => {
  const assets = ['win64-gpl-shared.zip', 'linux64-gpl.tar.xz', 'linuxarm64-gpl.tar.xz', 'winarm64-gpl-shared.zip'].map(suffix => ({ name: `ffmpeg-N-123-gabcdef-${suffix}` }));
  const stable = { tag_name: 'autobuild-2026-10-13-20-20', assets };
  assert.equal(ffmpegRelease([{ tag_name: 'latest', assets }, stable]).release, stable);
  assert.throws(() => ffmpegRelease([{ tag_name: 'latest', assets }]), /dated/);
  assert.throws(() => ffmpegRelease([{ ...stable, assets: assets.slice(1) }]), /Expected one/);
});

test('every desktop target has pinned media tools and one native JavaScript runtime', () => {
  const lock = JSON.parse(readFileSync(new URL('../runtime-tool-lock.json', import.meta.url), 'utf8'));
  assert.equal(nativeTarget('darwin', 'arm64'), 'darwin-aarch64');
  assert.equal(nativeTarget('win32', 'arm64'), 'windows-aarch64');
  assert.throws(() => nativeTarget('android', 'arm64'), /Unsupported/);
  for (const target of targets) {
    const javascript = ['qjs', 'deno'].filter(tool => toolSource(tool, target));
    assert.equal(javascript.length, 1, target);
    for (const tool of ['yt-dlp', 'ffmpeg', ...javascript, ...(target.startsWith('darwin-') ? ['ffprobe'] : [])]) {
      const source = toolSource(tool, target), record = lock.tools[tool], asset = record.assets[target];
      assert.equal(asset.repository ?? record.repository, source.repository);
      assert.match(asset.sha256, /^[a-f0-9]{64}$/);
      assert.ok(asset.url.startsWith(`https://github.com/${source.repository}/releases/download/`));
      if (source.name) assert.equal(asset.name, source.name); else assert.ok(asset.name.endsWith(source.suffix));
    }
  }
  assert.equal(toolSource('qjs', 'windows-aarch64'), null);
  assert.equal(toolSource('qjs', 'darwin-aarch64'), null);
});

test('native executable validation rejects the wrong CPU and accepts Mach-O universal slices', () => {
  const pe = Buffer.alloc(128); pe.write('MZ'); pe.writeUInt32LE(64, 60); pe.write('PE\0\0', 64); pe.writeUInt16LE(0xaa64, 68);
  assert.ok(nativeExecutable(pe, 'windows-aarch64'));
  assert.equal(nativeExecutable(pe, 'windows-x86_64'), false);
  pe.writeUInt32LE(0xfffffff0, 60);
  assert.equal(nativeExecutable(pe, 'windows-aarch64'), false);
  const elf = Buffer.alloc(32); Buffer.from([0x7f, 0x45, 0x4c, 0x46, 2, 1]).copy(elf); elf.writeUInt16LE(183, 18);
  assert.ok(nativeExecutable(elf, 'linux-aarch64'));
  assert.equal(nativeExecutable(elf, 'linux-x86_64'), false);
  const macho = cpu => { const bytes = Buffer.alloc(32); bytes.writeUInt32BE(0xcffaedfe); bytes.writeUInt32LE(cpu, 4); return bytes; };
  const arm = macho(0x0100000c), intel = macho(0x01000007);
  assert.ok(nativeExecutable(arm, 'darwin-aarch64'));
  assert.equal(nativeExecutable(arm, 'darwin-x86_64'), false);
  const fat = Buffer.alloc(112); fat.writeUInt32BE(0xcafebabe); fat.writeUInt32BE(2, 4);
  for (const [index, [cpu, bytes]] of [[0x01000007, intel], [0x0100000c, arm]].entries()) {
    const entry = 8 + index * 20, offset = 48 + index * 32;
    fat.writeUInt32BE(cpu, entry); fat.writeUInt32BE(offset, entry + 8); fat.writeUInt32BE(32, entry + 12); bytes.copy(fat, offset);
  }
  assert.ok(nativeExecutable(fat, 'darwin-x86_64'));
  assert.ok(nativeExecutable(fat, 'darwin-aarch64'));
  fat.writeUInt32BE(0xfffffff0, 36);
  assert.equal(nativeExecutable(fat, 'darwin-aarch64'), false);
  assert.equal(nativeExecutable(Buffer.from('not executable'), 'darwin-aarch64'), false);
});

test('shared FFmpeg includes every runtime DLL, excludes development files and rejects missing dependencies', () => {
  const libraries = ['avcodec', 'avdevice', 'avfilter', 'avformat', 'avutil', 'swresample', 'swscale'].map(name => `${name}-1.dll`);
  const members = ['build/bin/ffmpeg.exe', 'build/bin/ffprobe.exe', ...libraries.map(name => `build/bin/${name}`), 'build/lib/avcodec.lib', 'build/bin/ffplay.exe'];
  assert.deepEqual(runtimeEntries(members, true).sort(), ['ffmpeg.exe', 'ffprobe.exe', ...libraries].sort());
  assert.throws(() => runtimeEntries(members.filter(name => !name.endsWith('avcodec-1.dll')), true), /avcodec runtime DLL/);
  assert.throws(() => runtimeEntries([...members, 'other/avcodec-1.dll'], true), /avcodec runtime DLL/);
  assert.deepEqual(runtimeEntries(['build/bin/ffmpeg', 'build/bin/ffprobe'], false), ['ffmpeg', 'ffprobe']);
  assert.deepEqual(runtimeEntries(['build/bin/ffmpeg.exe', 'build/bin/ffprobe.exe'], true, false), ['ffmpeg.exe', 'ffprobe.exe']);
});

test('download retries broken connections and still rejects a checksum mismatch', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'huifa-tool-test-'));
  let requests = 0;
  const body = Buffer.from('verified runtime fixture');
  const server = createServer((request, response) => {
    if (++requests === 1) { request.socket.destroy(); return; }
    response.writeHead(200, { 'Content-Length': body.length, Connection: 'close' });
    response.end(body);
  });
  await new Promise(resolveListen => server.listen(0, '127.0.0.1', resolveListen));
  t.after(async () => {
    await new Promise(resolveClose => server.close(resolveClose));
    assert.equal(dirname(resolve(directory)), resolve(tmpdir()));
    rmSync(directory, { recursive: true, force: true });
  });
  const asset = { url: `http://127.0.0.1:${server.address().port}/tool`, sha256: createHash('sha256').update(body).digest('hex') };
  const destination = join(directory, 'tool');
  await download(asset, destination);
  assert.equal(requests, 2);
  assert.deepEqual(readFileSync(destination), body);
  const invalid = join(directory, 'invalid');
  await assert.rejects(download({ ...asset, sha256: '0'.repeat(64) }, invalid), /checksum/);
  assert.equal(requests, 3);
  assert.equal(existsSync(invalid), false);
});
