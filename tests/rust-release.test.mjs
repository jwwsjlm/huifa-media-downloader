import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { synchronize } from '../scripts/rust-release.mjs';
import { stage } from '../scripts/stage-rust-artifacts.mjs';
import { makeManifest } from '../scripts/rust-update-manifest.mjs';

test('one explicit version reaches every manifest and notes cannot be replaced', () => {
  const root = mkdtempSync(join(tmpdir(), 'huifa-release-'));
  try {
    mkdirSync(join(root, 'frontend')); mkdirSync(join(root, 'src-tauri'));
    writeFileSync(join(root, 'Cargo.toml'), '[workspace.package]\nversion = "0.2.0"\n');
    writeFileSync(join(root, 'frontend/package.json'), '{"version":"0.2.0"}');
    writeFileSync(join(root, 'frontend/package-lock.json'), '{"version":"0.2.0","packages":{"":{"version":"0.2.0"}}}');
    writeFileSync(join(root, 'src-tauri/tauri.conf.json'), '{"version":"0.2.0"}');
    synchronize(root, '2.1.3', '修复处理恢复\n- 保留原始输入');
    for (const path of ['frontend/package.json', 'frontend/package-lock.json', 'src-tauri/tauri.conf.json']) {
      assert.equal(JSON.parse(readFileSync(join(root, path))).version, '2.1.3');
    }
    assert.match(readFileSync(join(root, 'Cargo.toml'), 'utf8'), /version = "2\.1\.3"/);
    assert.equal(readFileSync(join(root, 'release-notes/2.1.3.md'), 'utf8'), '修复处理恢复\n- 保留原始输入\n');
    assert.throws(() => synchronize(root, '2.1.3', 'Different notes'), /cannot be rewritten/);
    assert.throws(() => synchronize(root, '2.1.4', ''), /must describe/);
    assert.equal(JSON.parse(readFileSync(join(root, 'frontend/package.json'))).version, '2.1.3');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('an unsigned desktop build cannot be staged or reach the updater', () => {
  const root = mkdtempSync(join(tmpdir(), 'huifa-artifacts-'));
  try {
    const bundle = join(root, 'target/release/bundle/nsis');
    mkdirSync(bundle, { recursive: true });
    writeFileSync(join(bundle, 'installer.exe'), 'fixture installer');
    assert.throws(() => stage(root, 'windows-x86_64', '2.1.3'));
    writeFileSync(join(bundle, 'installer.exe.sig'), 'dGVzdA==\n');
    const record = stage(root, 'windows-x86_64', '2.1.3');
    assert.equal(record.platform, 'windows-x86_64');
    assert.match(record.sha256, /^[a-f0-9]{64}$/);
    assert.throws(() => makeManifest('2.1.3', 'notes', 'owner/repo', [record]), /linux/);
    assert.throws(() => makeManifest('2.1.3', 'notes', 'owner/repo', [{ ...record, signature: '' }]), /Missing/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
