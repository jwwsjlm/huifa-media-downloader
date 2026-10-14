import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { validateVersion, validateNotes } from './rust-release.mjs';

export function makeManifest(version, notes, repository, records, now = new Date()) {
  validateVersion(version);
  const platforms = {};
  for (const target of ['windows-x86_64', 'linux-x86_64']) {
    const record = records.find(item => item.platform === target);
    if (!record?.signature || !/^[a-f0-9]{64}$/.test(record.sha256) || record.filename.includes('/') || !record.filename.includes(`-${version}-`)) {
      throw new Error(`Missing signed artifact for ${target}`);
    }
    platforms[target] = {
      signature: record.signature,
      url: `https://github.com/${repository}/releases/download/v${version}/${encodeURIComponent(record.filename)}`,
    };
  }
  return { version, notes: validateNotes(notes).trim(), pub_date: now.toISOString(), platforms };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.includes('--self-test')) {
    const records = ['windows-x86_64', 'linux-x86_64'].map(platform => ({ platform, signature: 'validSignature', filename: `HuifaDownloader-2.1.3-${platform}`, sha256: 'f'.repeat(64) }));
    assert.equal(makeManifest('2.1.3', '修复下载', 'owner/repo', records).version, '2.1.3');
    assert.throws(() => makeManifest('2.1.3', 'notes', 'owner/repo', records.slice(0, 1)), /Missing/);
    assert.throws(() => makeManifest('2.1.3', 'notes', 'owner/repo', records.map(item => ({ ...item, filename: '../bad' }))), /Missing/);
    console.log('Signed update manifest self-check passed');
  } else {
    const folder = resolve(process.argv[2] ?? 'rust-release-assets');
    const version = process.env.RELEASE_VERSION;
    const notes = readFileSync(resolve('release-notes', `${validateVersion(version)}.md`), 'utf8');
    const records = ['windows-x86_64', 'linux-x86_64'].map(platform => JSON.parse(readFileSync(join(folder, `${platform}.json`), 'utf8')));
    for (const record of records) {
      const digest = createHash('sha256').update(readFileSync(join(folder, record.filename))).digest('hex');
      if (digest !== record.sha256 || readFileSync(join(folder, record.filename + '.sig'), 'utf8').trim() !== record.signature) {
        throw new Error(`Artifact or signature changed: ${record.filename}`);
      }
    }
    const portable = join(folder, `HuifaDownloader-${version}-windows-x64-portable.zip`);
    if (!readFileSync(portable).length || !/^[A-Za-z0-9+/=\r\n]+$/.test(readFileSync(portable + '.sig', 'utf8').trim())) throw new Error('Missing signed Windows portable archive');
    const revision = process.env.RELEASE_SHA ?? 'HEAD';
    if (revision !== 'HEAD' && !/^[a-f0-9]{40}$/.test(revision)) throw new Error('Invalid build commit');
    const date = spawnSync('git', ['show', '-s', '--format=%cI', revision], { encoding: 'utf8', shell: false });
    if (date.status !== 0) throw new Error('Cannot read build commit date');
    writeFileSync(join(folder, 'rust-updates.json'), JSON.stringify(makeManifest(version, notes, process.env.GITHUB_REPOSITORY, records, new Date(date.stdout.trim())), null, 2) + '\n');
    const files = readdirSync(folder).filter(file => /\.(exe|AppImage|zip|sig|json)$/.test(file)).sort();
    writeFileSync(join(folder, 'SHA256SUMS'), files.map(file => `${createHash('sha256').update(readFileSync(join(folder, file))).digest('hex')}  ${file}`).join('\n') + '\n');
  }
}
