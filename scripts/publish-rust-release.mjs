import { spawnSync } from 'node:child_process';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { validateVersion } from './rust-release.mjs';

function gh(args, optional404 = false) {
  const result = spawnSync('gh', args, { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, shell: false });
  if (result.status !== 0) {
    if (optional404 && /HTTP 404/.test(result.stderr)) return null;
    throw new Error(`GitHub command failed: ${result.stderr || result.error?.message}`);
  }
  return result.stdout;
}
function api(path, optional404 = false) {
  const text = gh(['api', path], optional404);
  return text === null ? null : JSON.parse(text);
}

const version = validateVersion(process.env.RELEASE_VERSION ?? '');
const repository = process.env.GITHUB_REPOSITORY;
const sha = process.env.RELEASE_SHA;
if (!/^[\w.-]+\/[\w.-]+$/.test(repository ?? '') || !/^[a-f0-9]{40}$/.test(sha ?? '')) {
  throw new Error('A repository and exact build commit are required');
}
const tag = `v${version}`;
const folder = resolve(process.argv[2] ?? 'rust-release-assets');
const notesPath = resolve('release-notes', `${version}.md`);
const manifest = JSON.parse(readFileSync(join(folder, 'rust-updates.json'), 'utf8'));
if (manifest.version !== version || Object.keys(manifest.platforms ?? {}).length !== 2) throw new Error('Incomplete update manifest');
const required = ['SHA256SUMS', 'rust-updates.json'];
for (const entry of Object.values(manifest.platforms)) {
  const filename = decodeURIComponent(new URL(entry.url).pathname.split('/').at(-1));
  if (!filename || filename.includes('/') || filename.includes('\\')) throw new Error('Unsafe artifact filename');
  required.push(filename, filename + '.sig');
}
for (const file of required) if (!statSync(join(folder, file)).isFile()) throw new Error(`Missing artifact: ${file}`);

let reference = api(`repos/${repository}/git/ref/tags/${tag}`, true);
if (reference) {
  let object = reference.object;
  for (let depth = 0; object.type === 'tag' && depth < 4; depth++) object = api(`repos/${repository}/git/tags/${object.sha}`).object;
  if (object.type !== 'commit' || object.sha !== sha) throw new Error('Release tag points to another commit');
} else {
  gh(['api', `repos/${repository}/git/refs`, '--method', 'POST', '-f', `ref=refs/tags/${tag}`, '-f', `sha=${sha}`]);
}

let release = api(`repos/${repository}/releases/tags/${tag}`, true);
if (!release) {
  gh(['release', 'create', tag, '--repo', repository, '--target', sha, '--draft', '--title', `Huifa Downloader ${version}`, '--notes-file', notesPath]);
  release = api(`repos/${repository}/releases/tags/${tag}`);
}
const notes = readFileSync(notesPath, 'utf8').trim();
if ((release.body ?? '').trim() !== notes) throw new Error('Existing release notes do not match this build');
const files = readdirSync(folder).filter(file => /\.(exe|AppImage|sig|json)$/.test(file) || file === 'SHA256SUMS');
for (const file of files) {
  const path = join(folder, file);
  const existing = release.assets.find(asset => asset.name === file);
  if (existing) {
    const digest = 'sha256:' + createHash('sha256').update(readFileSync(path)).digest('hex');
    if (existing.size !== statSync(path).size || existing.digest !== digest) {
      throw new Error(`Published asset differs: ${file}; existing assets are never overwritten`);
    }
  } else {
    if (!release.draft) throw new Error(`A published release is incomplete: ${file}; refusing to alter it`);
    gh(['release', 'upload', tag, path, '--repo', repository]);
  }
}
if (release.draft) gh(['release', 'edit', tag, '--repo', repository, '--draft=false', '--latest']);
console.log(`Published ${repository} ${tag} from ${sha}`);
