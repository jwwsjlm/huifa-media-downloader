import assert from 'node:assert/strict';
import { readdirSync, readFileSync, copyFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { validateVersion } from './rust-release.mjs';

function one(directory, suffix) {
  const files = readdirSync(directory).filter(file => file.endsWith(suffix));
  if (files.length !== 1) throw new Error(`Expected one ${suffix} in ${directory}; found ${files.length}`);
  return join(directory, files[0]);
}

export function stage(root, platform, version) {
  validateVersion(version);
  const windows = platform === 'windows-x86_64';
  if (!windows && platform !== 'linux-x86_64') throw new Error('Unsupported release platform');
  const bundle = resolve(root, 'target/release/bundle', windows ? 'nsis' : 'appimage');
  const source = one(bundle, windows ? '.exe' : '.AppImage');
  const signature = readFileSync(source + '.sig', 'utf8').trim();
  if (!signature || !/^[A-Za-z0-9+/=\r\n]+$/.test(signature)) throw new Error('Missing or malformed desktop update signature');
  const folder = resolve(root, 'rust-release-assets');
  mkdirSync(folder, { recursive: true });
  const name = `HuifaDownloader-${version}-${windows ? 'windows-x64-setup.exe' : 'linux-x64.AppImage'}`;
  copyFileSync(source, join(folder, name));
  writeFileSync(join(folder, name + '.sig'), signature + '\n');
  const info = { platform, filename: name, signature, sha256: createHash('sha256').update(readFileSync(source)).digest('hex') };
  writeFileSync(join(folder, `${platform}.json`), JSON.stringify(info, null, 2) + '\n');
  return info;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.includes('--self-test')) {
    assert.throws(() => stage('.', 'macos', '2.1.3'), /Unsupported/);
    console.log('Desktop artifact platform self-check passed');
  } else {
    console.log(JSON.stringify(stage(process.cwd(), process.env.RELEASE_PLATFORM, process.env.RELEASE_VERSION)));
  }
}
