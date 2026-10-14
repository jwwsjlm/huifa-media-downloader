import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
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

test('publication creates or resumes a draft and leaves published assets unchanged', () => {
  const root = mkdtempSync(join(tmpdir(), 'huifa-publication-'));
  try {
    const folder = join(root, 'rust-release-assets');
    mkdirSync(folder); mkdirSync(join(root, 'release-notes'));
    writeFileSync(join(root, 'release-notes/2.1.3.md'), 'Verified build\n');
    const platforms = {};
    for (const [platform, filename] of [['windows-x86_64', 'windows.exe'], ['linux-x86_64', 'linux.AppImage']]) {
      writeFileSync(join(folder, filename), 'verified fixture');
      writeFileSync(join(folder, filename + '.sig'), 'dGVzdA==\n');
      platforms[platform] = { url: 'https://github.com/owner/repo/releases/download/v2.1.3/' + filename };
    }
    writeFileSync(join(folder, 'rust-updates.json'), JSON.stringify({ version: '2.1.3', platforms }));
    writeFileSync(join(folder, 'SHA256SUMS'), 'verified fixture checksums');
    for (const existing of [false, true]) {
      const result = spawnSync(process.execPath, ['--input-type=module', '-e', `
        import assert from 'node:assert/strict';
        import cp from 'node:child_process';
        import { syncBuiltinESMExports } from 'node:module';
        import { readFileSync } from 'node:fs';
        import { basename } from 'node:path';
        import { createHash } from 'node:crypto';
        const draft = () => ({ databaseId: 123, draft: true, body: 'Verified build', assets: [] });
        let release = process.env.HUIFA_TEST_DRAFT === 'true' ? draft() : null;
        let created = 0, uploaded = 0, published = 0;
        const ok = value => ({ status: 0, stdout: JSON.stringify(value), stderr: '' });
        cp.spawnSync = (executable, args) => {
          assert.equal(executable, 'gh');
          if (args[0] === 'api') {
            if (args[1].endsWith('/git/ref/tags/v2.1.3')) return ok({ object: { type: 'commit', sha: process.env.RELEASE_SHA } });
            if (args[1].endsWith('/releases/123')) { assert(release); return ok(release); }
            if (args[1].endsWith('/releases/tags/v2.1.3')) return { status: 1, stdout: '', stderr: 'gh: Not Found (HTTP 404)' };
          }
          if (args[0] === 'release') {
            if (args[1] === 'view') return release ? ok({ databaseId: 123 }) : { status: 1, stdout: '', stderr: 'release not found' };
            if (args[1] === 'create') { assert.equal(release, null); release = draft(); created++; return ok(null); }
            if (args[1] === 'upload') {
              assert(release.draft);
              const bytes = readFileSync(args[3]);
              release.assets.push({ name: basename(args[3]), size: bytes.length, digest: 'sha256:' + createHash('sha256').update(bytes).digest('hex') });
              uploaded++; return ok(null);
            }
            if (args[1] === 'edit') { assert(release.draft); release.draft = false; published++; return ok(null); }
          }
          assert.fail('Unexpected GitHub operation: ' + args.join(' '));
        };
        syncBuiltinESMExports();
        await import(process.env.HUIFA_TEST_SCRIPT + '?first');
        await import(process.env.HUIFA_TEST_SCRIPT + '?retry');
        assert.equal(created, process.env.HUIFA_TEST_DRAFT === 'true' ? 0 : 1);
        assert.equal(uploaded, 6); assert.equal(published, 1); assert.equal(release.draft, false);
      `], {
        cwd: root, encoding: 'utf8', env: { ...process.env, GITHUB_REPOSITORY: 'owner/repo', RELEASE_VERSION: '2.1.3', RELEASE_SHA: 'a'.repeat(40), HUIFA_TEST_DRAFT: String(existing), HUIFA_TEST_SCRIPT: new URL('../scripts/publish-rust-release.mjs', import.meta.url).href },
      });
      assert.equal(result.status, 0, result.stderr);
    }
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('update files keep the build commit date and identical bytes on retry', () => {
  const root = mkdtempSync(join(tmpdir(), 'huifa-update-retry-'));
  try {
    const folder = join(root, 'rust-release-assets');
    mkdirSync(folder); mkdirSync(join(root, 'release-notes'));
    writeFileSync(join(root, 'release-notes/2.1.3.md'), 'Verified build\n');
    for (const platform of ['windows-x86_64', 'linux-x86_64']) {
      const filename = `HuifaDownloader-2.1.3-${platform}.${platform.startsWith('windows') ? 'exe' : 'AppImage'}`;
      writeFileSync(join(folder, filename), 'fixture installer');
      writeFileSync(join(folder, filename + '.sig'), 'dGVzdA==\n');
      writeFileSync(join(folder, platform + '.json'), JSON.stringify({ platform, filename, signature: 'dGVzdA==', sha256: createHash('sha256').update('fixture installer').digest('hex') }));
    }
    const env = { ...process.env, GIT_AUTHOR_DATE: '2026-10-14T04:54:13+00:00', GIT_COMMITTER_DATE: '2026-10-14T04:54:13+00:00', GITHUB_REPOSITORY: 'owner/repo', RELEASE_VERSION: '2.1.3' };
    const run = (executable, args) => {
      const result = spawnSync(executable, args, { cwd: root, encoding: 'utf8', env });
      assert.equal(result.status, 0, result.stderr);
      return result.stdout.trim();
    };
    run('git', ['init']); run('git', ['add', '.']);
    run('git', ['-c', 'user.name=Release test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'fixture']);
    env.RELEASE_SHA = run('git', ['rev-parse', 'HEAD']);
    const script = fileURLToPath(new URL('../scripts/rust-update-manifest.mjs', import.meta.url));
    run(process.execPath, [script]);
    const manifest = readFileSync(join(folder, 'rust-updates.json'), 'utf8');
    const sums = readFileSync(join(folder, 'SHA256SUMS'), 'utf8');
    assert.equal(JSON.parse(manifest).pub_date, '2026-10-14T04:54:13.000Z');
    run(process.execPath, [script]);
    assert.equal(readFileSync(join(folder, 'rust-updates.json'), 'utf8'), manifest);
    assert.equal(readFileSync(join(folder, 'SHA256SUMS'), 'utf8'), sums);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
