import assert from 'node:assert/strict';
import { readdirSync, readFileSync, copyFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { validateVersion } from './rust-release.mjs';
import { releasePlatforms } from './release-platforms.mjs';

function one(directory, suffix) {
  const files = readdirSync(directory).filter(file => file.endsWith(suffix));
  if (files.length !== 1) throw new Error(`Expected one ${suffix} in ${directory}; found ${files.length}`);
  return join(directory, files[0]);
}

export function stage(root, platform, version) {
  validateVersion(version);
  const target = releasePlatforms[platform];
  if (!target) throw new Error('Unsupported release platform');
  const windows = platform.startsWith('windows');
  const bundle = resolve(root, 'target/release/bundle', target.bundle);
  const source = one(bundle, target.extension);
  const name = `HuifaDownloader-${version}-${target.suffix}${windows ? '-setup' : ''}${target.extension}`;
  const artifacts = [[source, name]];
  if (windows) {
    const filename = `HuifaDownloader-${version}-${target.suffix}-portable.zip`;
    const file = resolve(root, 'target/release/bundle/portable', filename);
    if (!readFileSync(file).length) throw new Error(`Empty portable archive: ${filename}`);
    artifacts.push([file, filename]);
  }
  const signatures = artifacts.map(([file]) => {
    const signature = readFileSync(file + '.sig', 'utf8').trim();
    if (!signature || !/^[A-Za-z0-9+/=\r\n]+$/.test(signature)) throw new Error('Missing or malformed desktop update signature');
    return signature;
  });
  const folder = resolve(root, 'rust-release-assets');
  mkdirSync(folder, { recursive: true });
  for (const [index, [file, filename]] of artifacts.entries()) {
    copyFileSync(file, join(folder, filename));
    writeFileSync(join(folder, filename + '.sig'), signatures[index] + '\n');
  }
  if (platform.startsWith('darwin')) {
    const dmg = one(resolve(root, 'target/release/bundle/dmg'), '.dmg');
    copyFileSync(dmg, join(folder, `HuifaDownloader-${version}-${target.suffix}.dmg`));
  }
  const info = { platform, filename: name, signature: signatures[0], sha256: createHash('sha256').update(readFileSync(source)).digest('hex') };
  writeFileSync(join(folder, `${platform}.json`), JSON.stringify(info, null, 2) + '\n');
  if (windows) {
    const [file, filename] = artifacts[1];
    const portable = { platform: `${platform}-portable`, filename, signature: signatures[1], sha256: createHash('sha256').update(readFileSync(file)).digest('hex') };
    writeFileSync(join(folder, `${portable.platform}.json`), JSON.stringify(portable, null, 2) + '\n');
  }
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
