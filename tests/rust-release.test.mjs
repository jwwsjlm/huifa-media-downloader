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
import { releasePlatforms } from '../scripts/release-platforms.mjs';

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
    const portable = join(root, 'target/release/bundle/portable');
    mkdirSync(portable, { recursive: true });
    const name = 'HuifaDownloader-2.1.3-windows-x64-portable.zip';
    writeFileSync(join(portable, name), 'fixture portable');
    writeFileSync(join(portable, name + '.sig'), 'dGVzdA==\n');
    const record = stage(root, 'windows-x86_64', '2.1.3');
    assert.equal(record.platform, 'windows-x86_64');
    assert.match(record.sha256, /^[a-f0-9]{64}$/);
    assert.throws(() => makeManifest('2.1.3', 'notes', 'owner/repo', [record]), /Missing signed artifact/);
    assert.throws(() => makeManifest('2.1.3', 'notes', 'owner/repo', [{ ...record, signature: '' }]), /Missing/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('Windows releases require a signed portable archive alongside the installer', () => {
  const root = mkdtempSync(join(tmpdir(), 'huifa-portable-'));
  try {
    const bundle = join(root, 'target/release/bundle/nsis');
    const portable = join(root, 'target/release/bundle/portable');
    mkdirSync(bundle, { recursive: true }); mkdirSync(portable, { recursive: true });
    writeFileSync(join(bundle, 'installer.exe'), 'fixture installer');
    writeFileSync(join(bundle, 'installer.exe.sig'), 'dGVzdA==\n');
    assert.throws(() => stage(root, 'windows-x86_64', '2.1.3'), /zip/);
    const archive = join(portable, 'HuifaDownloader-2.1.3-windows-x64-portable.zip');
    writeFileSync(archive, 'fixture portable');
    assert.throws(() => stage(root, 'windows-x86_64', '2.1.3'));
    writeFileSync(archive + '.sig', 'not a signature!');
    assert.throws(() => stage(root, 'windows-x86_64', '2.1.3'), /signature/);
    writeFileSync(archive + '.sig', 'dGVzdA==\n');
    stage(root, 'windows-x86_64', '2.1.3');
    const staged = join(root, 'rust-release-assets/HuifaDownloader-2.1.3-windows-x64-portable.zip');
    assert.equal(readFileSync(staged, 'utf8'), 'fixture portable');
    assert.equal(readFileSync(staged + '.sig', 'utf8'), 'dGVzdA==\n');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('native desktop packages stage all supported CPU architectures and signed Mac updates', () => {
  for (const [platform, target] of Object.entries(releasePlatforms)) {
    const root = mkdtempSync(join(tmpdir(), 'huifa-platform-'));
    try {
      const bundle = join(root, 'target/release/bundle', target.bundle);
      mkdirSync(bundle, { recursive: true });
      writeFileSync(join(bundle, 'installer' + target.extension), 'fixture package');
      writeFileSync(join(bundle, 'installer' + target.extension + '.sig'), 'dGVzdA==\n');
      if (platform.startsWith('windows')) {
        const portable = join(root, 'target/release/bundle/portable');
        mkdirSync(portable, { recursive: true });
        writeFileSync(join(portable, `HuifaDownloader-2.1.3-${target.suffix}-portable.zip`), 'portable');
        writeFileSync(join(portable, `HuifaDownloader-2.1.3-${target.suffix}-portable.zip.sig`), 'dGVzdA==\n');
      }
      if (platform.startsWith('darwin')) {
        const dmg = join(root, 'target/release/bundle/dmg');
        mkdirSync(dmg, { recursive: true });
        writeFileSync(join(dmg, 'installer.dmg'), 'disk image');
      }
      const record = stage(root, platform, '2.1.3');
      assert.equal(record.platform, platform);
      assert.ok(record.filename.includes(target.suffix));
      assert.equal(readFileSync(join(root, 'rust-release-assets', record.filename), 'utf8'), 'fixture package');
      if (platform.startsWith('darwin')) assert.equal(readFileSync(join(root, 'rust-release-assets', `HuifaDownloader-2.1.3-${target.suffix}.dmg`), 'utf8'), 'disk image');
    } finally { rmSync(root, { recursive: true, force: true }); }
  }
});

test('publication creates or resumes a draft and leaves published assets unchanged', () => {
  const root = mkdtempSync(join(tmpdir(), 'huifa-publication-'));
  try {
    const folder = join(root, 'rust-release-assets');
    mkdirSync(folder); mkdirSync(join(root, 'release-notes'));
    writeFileSync(join(root, 'release-notes/2.1.3.md'), 'Verified build\n');
    const platforms = {};
    for (const [platform, target] of Object.entries(releasePlatforms)) {
      const filename = `HuifaDownloader-2.1.3-${target.suffix}${target.extension}`;
      writeFileSync(join(folder, filename), 'verified fixture');
      writeFileSync(join(folder, filename + '.sig'), 'dGVzdA==\n');
      platforms[platform] = { url: 'https://github.com/owner/repo/releases/download/v2.1.3/' + filename };
    }
    writeFileSync(join(folder, 'rust-updates.json'), JSON.stringify({ version: '2.1.3', platforms }));
    writeFileSync(join(folder, 'SHA256SUMS'), 'verified fixture checksums');
    writeFileSync(join(folder, 'HuifaDownloader-2.1.3-windows-x64-portable.zip'), 'fixture portable');
    writeFileSync(join(folder, 'HuifaDownloader-2.1.3-windows-x64-portable.zip.sig'), 'dGVzdA==\n');
    writeFileSync(join(folder, 'HuifaDownloader-2.1.3-windows-arm64-portable.zip'), 'fixture portable');
    writeFileSync(join(folder, 'HuifaDownloader-2.1.3-windows-arm64-portable.zip.sig'), 'dGVzdA==\n');
    for (const suffix of ['macos-x64', 'macos-arm64']) writeFileSync(join(folder, `HuifaDownloader-2.1.3-${suffix}.dmg`), 'disk image');
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
            if (args[1] === 'create') {
              assert.equal(release, null); release = draft();
              release.body = readFileSync(args[args.indexOf('--notes-file') + 1], 'utf8');
              assert.match(release.body, /Choose your download/);
              assert.match(release.body, /macos-arm64\.dmg/);
              created++; return ok(null);
            }
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
        assert.equal(uploaded, 20); assert.equal(published, 1); assert.equal(release.draft, false);
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
    writeFileSync(join(folder, 'HuifaDownloader-2.1.3-windows-x64-portable.zip'), 'fixture portable');
    writeFileSync(join(folder, 'HuifaDownloader-2.1.3-windows-x64-portable.zip.sig'), 'dGVzdA==\n');
    writeFileSync(join(folder, 'HuifaDownloader-2.1.3-windows-arm64-portable.zip'), 'fixture portable');
    writeFileSync(join(folder, 'HuifaDownloader-2.1.3-windows-arm64-portable.zip.sig'), 'dGVzdA==\n');
    for (const suffix of ['macos-x64', 'macos-arm64']) writeFileSync(join(folder, `HuifaDownloader-2.1.3-${suffix}.dmg`), 'disk image');
    for (const [platform, target] of Object.entries(releasePlatforms)) {
      const filename = `HuifaDownloader-2.1.3-${target.suffix}${target.extension}`;
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
    assert(sums.includes('HuifaDownloader-2.1.3-windows-x64-portable.zip\n'));
    assert.equal(JSON.parse(manifest).pub_date, '2026-10-14T04:54:13.000Z');
    run(process.execPath, [script]);
    assert.equal(readFileSync(join(folder, 'rust-updates.json'), 'utf8'), manifest);
    assert.equal(readFileSync(join(folder, 'SHA256SUMS'), 'utf8'), sums);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('portable additions preserve the release tag and existing files, including on retry', () => {
  const root = mkdtempSync(join(tmpdir(), 'huifa-portable-publication-'));
  try {
    const folder = join(root, 'rust-release-assets');
    mkdirSync(folder); mkdirSync(join(root, 'release-notes')); mkdirSync(join(root, 'src-tauri'));
    writeFileSync(join(root, 'release-notes/2.1.3.md'), 'Verified build\n');
    writeFileSync(join(root, 'src-tauri/tauri.conf.json'), '{"version":"2.1.3"}');
    writeFileSync(join(folder, 'HuifaDownloader-2.1.3-windows-x64-portable.zip'), 'fixture portable');
    writeFileSync(join(folder, 'HuifaDownloader-2.1.3-windows-x64-portable.zip.sig'), 'dGVzdA==\n');
    const result = spawnSync(process.execPath, ['--input-type=module', '-e', `
      import assert from 'node:assert/strict';
      import cp from 'node:child_process';
      import { syncBuiltinESMExports } from 'node:module';
      import { readFileSync, writeFileSync } from 'node:fs';
      import { basename } from 'node:path';
      import { createHash } from 'node:crypto';
      const original = { name: 'SHA256SUMS', size: 123, digest: 'sha256:' + 'c'.repeat(64) };
      const release = { draft: false, body: 'Verified build', assets: [original] };
      const uploaded = [];
      const ok = value => ({ status: 0, stdout: JSON.stringify(value), stderr: '' });
      cp.spawnSync = (executable, args) => {
        assert.equal(executable, 'gh');
        if (args[0] === 'api' && args[1].endsWith('/git/ref/tags/v2.1.3')) return ok({ object: { type: 'commit', sha: 'b'.repeat(40) } });
        if (args[0] === 'api' && args[1].endsWith('/releases/123')) return ok(release);
        if (args[0] === 'release' && args[1] === 'view') return ok({ databaseId: 123 });
        if (args[0] === 'release' && args[1] === 'upload') {
          assert.equal(args.length, 6);
          const bytes = readFileSync(args[3]);
          const name = basename(args[3]);
          assert.match(name, /^HuifaDownloader-2\\.1\\.3-windows-x64-portable\\.zip(?:\\.sig|\\.sha256)?$/);
          release.assets.push({ name, size: bytes.length, digest: 'sha256:' + createHash('sha256').update(bytes).digest('hex') });
          uploaded.push(name); return ok(null);
        }
        assert.fail('Unexpected GitHub mutation: ' + args.join(' '));
      };
      syncBuiltinESMExports();
      process.argv = [process.execPath, process.env.HUIFA_TEST_SCRIPT, 'rust-release-assets', '--portable-only'];
      await import(process.env.HUIFA_TEST_SCRIPT + '?first');
      await import(process.env.HUIFA_TEST_SCRIPT + '?retry');
      assert.equal(uploaded.length, 3);
      assert.deepEqual(release.assets[0], original);
      assert.equal(release.draft, false);
      const checksums = readFileSync('rust-release-assets/HuifaDownloader-2.1.3-windows-x64-portable.zip.sha256', 'utf8');
      assert.equal(checksums.trim().split('\\n').length, 2);
      writeFileSync('rust-release-assets/HuifaDownloader-2.1.3-windows-x64-portable.zip', 'different portable');
      await assert.rejects(import(process.env.HUIFA_TEST_SCRIPT + '?changed'), /Published asset differs/);
      assert.equal(uploaded.length, 3);
    `], {
      cwd: root, encoding: 'utf8', env: { ...process.env, GITHUB_REPOSITORY: 'owner/repo', RELEASE_VERSION: '2.1.3', RELEASE_SHA: 'a'.repeat(40), HUIFA_TEST_SCRIPT: new URL('../scripts/publish-rust-release.mjs', import.meta.url).href },
    });
    assert.equal(result.status, 0, result.stderr);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
