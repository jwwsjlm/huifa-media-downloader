import { test } from 'node:test';
import assert from 'node:assert/strict';
import { renderReleaseNotes } from '../scripts/release-notes.mjs';

const version = '0.5.3';
const name = suffix => `HuifaDownloader-${version}-${suffix}`;
const url = suffix => `https://github.com/jwwsjlm/huifa-media-downloader/releases/download/v${version}/${name(suffix)}`;

test('download guide links every available platform and preserves the original notes', () => {
  const suffixes = ['windows-x64-setup.exe', 'windows-x64-portable.zip', 'windows-arm64-setup.exe', 'windows-arm64-portable.zip', 'linux-x64.AppImage', 'linux-arm64.AppImage', 'macos-arm64.dmg', 'macos-x64.dmg'];
  const notes = '## 更新\r\n\r\n- Original notes, unchanged.\r\n';
  const body = renderReleaseNotes(version, notes, [...suffixes.map(name).reverse(), 'SHA256SUMS', 'rust-updates.json', name('windows-x64-setup.exe.sig')]);
  for (const suffix of suffixes) assert.ok(body.includes(`](${url(suffix)})`), suffix);
  assert.equal((body.match(/\]\(https:\/\//g) ?? []).length, suffixes.length);
  assert.match(body, /推荐：普通 Intel \/ AMD/);
  assert.match(body, /Snapdragon.*ARM/);
  assert.match(body, /Apple M 系列/);
  assert.match(body, /Intel 芯片的 Mac/);
  assert.ok(body.indexOf(url('windows-x64-setup.exe')) < body.indexOf(url('windows-x64-portable.zip')));
  assert.match(body, /System Settings → Privacy & Security/);
  assert.ok(body.endsWith('\n\n---\n\n' + notes));
  assert.ok(body.startsWith('## 选择下载包 / Choose your download\n'));
});

test('guide lists only exact assets for this version and does not link support files', () => {
  const body = renderReleaseNotes(version, 'Release notes', [name('windows-x64-portable.zip'), name('macos-arm64.dmg.sig'), 'HuifaDownloader-0.5.2-linux-x64.AppImage', 'SHA256SUMS', 'rust-updates.json']);
  assert.ok(body.includes(`[便携版 / Portable](${url('windows-x64-portable.zip')})`));
  assert.equal((body.match(/\]\(https:\/\//g) ?? []).length, 1);
  assert.doesNotMatch(body, /\| Windows ARM64|\| Linux|\| macOS|Privacy & Security/);
  assert.match(body, /`\.sig`.*`\.json`.*`SHA256SUMS`/);
  assert.match(body, /These extra files are optional/);
});

test('empty desktop assets do not produce fabricated links', () => {
  const body = renderReleaseNotes(version, 'Notes', []);
  assert.match(body, /No desktop download packages are available/);
  assert.doesNotMatch(body, /\]\(https:\/\//);
});

test('rejects unsafe versions and asset filenames before rendering links', () => {
  for (const invalid of ['', 'v0.5.3', '00.5.3', '0.5', '0.5.3-beta', '../0.5.3', '0.5.3\n', 503, null]) {
    assert.throws(() => renderReleaseNotes(invalid, 'Notes', []), /version/i);
  }
  for (const invalid of ['', '../app.exe', 'folder/app.exe', 'folder\\app.exe', 'app.exe?download=1', 'app.exe#x', 'app%2F.exe', 'app](https://evil.test).exe', 'app.exe\n', 'app exe', '.hidden', 'x'.repeat(256), 123, null]) {
    assert.throws(() => renderReleaseNotes(version, 'Notes', [invalid]), /filename/);
  }
  assert.throws(() => renderReleaseNotes(version, 'Notes', 'app.exe'), /filename/);
  assert.throws(() => renderReleaseNotes(version, null, []), /must be text/);
});
