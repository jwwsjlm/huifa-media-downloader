import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { download, ffmpegRelease, runtimeEntries } from '../scripts/acquire-rust-tools.mjs';

test('FFmpeg uses dated assets rather than a moving latest URL', () => {
  const assets = ['win64-gpl-shared.zip', 'linux64-gpl.tar.xz', 'linuxarm64-gpl.tar.xz'].map(suffix => ({ name: `ffmpeg-N-123-gabcdef-${suffix}` }));
  const stable = { tag_name: 'autobuild-2026-10-13-20-20', assets };
  assert.equal(ffmpegRelease([{ tag_name: 'latest', assets }, stable]).release, stable);
  assert.throws(() => ffmpegRelease([{ tag_name: 'latest', assets }]), /dated/);
  assert.throws(() => ffmpegRelease([{ ...stable, assets: assets.slice(1) }]), /Expected one/);
});

test('shared FFmpeg includes every runtime DLL, excludes development files and rejects missing dependencies', () => {
  const libraries = ['avcodec', 'avdevice', 'avfilter', 'avformat', 'avutil', 'swresample', 'swscale'].map(name => `${name}-1.dll`);
  const members = ['build/bin/ffmpeg.exe', 'build/bin/ffprobe.exe', ...libraries.map(name => `build/bin/${name}`), 'build/lib/avcodec.lib', 'build/bin/ffplay.exe'];
  assert.deepEqual(runtimeEntries(members, true).sort(), ['ffmpeg.exe', 'ffprobe.exe', ...libraries].sort());
  assert.throws(() => runtimeEntries(members.filter(name => !name.endsWith('avcodec-1.dll')), true), /avcodec runtime DLL/);
  assert.throws(() => runtimeEntries([...members, 'other/avcodec-1.dll'], true), /avcodec runtime DLL/);
  assert.deepEqual(runtimeEntries(['build/bin/ffmpeg', 'build/bin/ffprobe'], false), ['ffmpeg', 'ffprobe']);
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
