import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export function validateVersion(version) {
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version)) {
    throw new Error('Version must be an explicit semantic version such as 2.1.3');
  }
  return version;
}

export function validateNotes(notes) {
  const normalized = String(notes ?? '').replace(/\r\n/g, '\n').trim();
  if (!normalized || normalized.length > 100_000 || !/[\p{L}\p{N}]/u.test(normalized)) {
    throw new Error('Release notes must describe this version');
  }
  return normalized + '\n';
}

export function setCargoVersion(cargo, version) {
  const result = cargo.replace(/(\[workspace\.package\][\s\S]*?\nversion\s*=\s*")[^"]+("[^\n]*)/, `$1${version}$2`);
  if (result === cargo && !cargo.includes(`version = "${version}"`)) throw new Error('Workspace version was not found');
  return result;
}

export function synchronize(root, version, notes) {
  validateVersion(version);
  const releaseNotes = validateNotes(notes);
  const jsonPaths = ['frontend/package.json', 'frontend/package-lock.json', 'src-tauri/tauri.conf.json'];
  const files = jsonPaths.map(path => {
    const value = JSON.parse(readFileSync(resolve(root, path), 'utf8'));
    value.version = version;
    if (path.endsWith('package-lock.json')) value.packages[''].version = version;
    return [resolve(root, path), JSON.stringify(value, null, 2) + '\n'];
  });
  const cargoPath = resolve(root, 'Cargo.toml');
  files.push([cargoPath, setCargoVersion(readFileSync(cargoPath, 'utf8'), version)]);
  const notesPath = resolve(root, 'release-notes', `${version}.md`);
  if (existsSync(notesPath) && readFileSync(notesPath, 'utf8').replace(/\r\n/g, '\n') !== releaseNotes) {
    throw new Error('Existing release notes differ; a version cannot be rewritten');
  }
  mkdirSync(resolve(root, 'release-notes'), { recursive: true });
  for (const [path, content] of files) writeFileSync(path, content);
  writeFileSync(notesPath, releaseNotes);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.includes('--self-test')) {
    for (const version of ['2.1.3', '0.2.0', '100.0.9']) assert.equal(validateVersion(version), version);
    for (const version of ['', 'v2.1.3', '02.1.3', '2.1', '2.1.3\n', '../../x', '2.1.3-beta']) {
      assert.throws(() => validateVersion(version));
    }
    assert.throws(() => validateNotes('  \n '));
    assert.throws(() => validateNotes('### --'));
    assert.equal(validateNotes('修复下载\r\n- 保留进度 '), '修复下载\n- 保留进度\n');
    assert.equal(setCargoVersion('[workspace.package]\nversion = "0.2.0"\nedition = "2024"\n', '2.1.3'), '[workspace.package]\nversion = "2.1.3"\nedition = "2024"\n');
    console.log('Rust release validation self-check passed');
  } else {
    synchronize(process.cwd(), process.env.RELEASE_VERSION ?? '', process.env.RELEASE_NOTES ?? '');
  }
}
