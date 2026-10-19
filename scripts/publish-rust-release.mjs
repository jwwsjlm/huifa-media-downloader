import { spawnSync } from 'node:child_process';
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { validateVersion } from './rust-release.mjs';
import { renderReleaseNotes } from './release-notes.mjs';
import { releasePlatforms, isReleaseAsset } from './release-platforms.mjs';

function gh(args, optional404 = false) {
  const result = spawnSync('gh', args, { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, shell: false });
  if (result.status !== 0) {
    if (optional404 && /HTTP 404|release not found/.test(result.stderr)) return null;
    throw new Error(`GitHub command failed: ${result.stderr || result.error?.message}`);
  }
  return result.stdout;
}
function api(path, optional404 = false) {
  const text = gh(['api', path], optional404);
  return text === null ? null : JSON.parse(text);
}
function releaseForTag(repository, tag) {
  const result = gh(['release', 'view', tag, '--repo', repository, '--json', 'databaseId'], true);
  return result === null ? null : api(`repos/${repository}/releases/${JSON.parse(result).databaseId}`);
}

const version = validateVersion(process.env.RELEASE_VERSION ?? '');
const repository = process.env.GITHUB_REPOSITORY;
const sha = process.env.RELEASE_SHA;
if (!/^[\w.-]+\/[\w.-]+$/.test(repository ?? '') || !/^[a-f0-9]{40}$/.test(sha ?? '')) {
  throw new Error('A repository and exact build commit are required');
}
const tag = `v${version}`;
const portableOnly = process.argv.includes('--portable-only');
const folder = resolve(process.argv[2] ?? 'rust-release-assets');
const notesPath = resolve('release-notes', `${version}.md`);
const rawNotes = readFileSync(notesPath, 'utf8');
const portable = `HuifaDownloader-${version}-windows-x64-portable.zip`;
const required = [portable, portable + '.sig'];
if (portableOnly) {
  if (JSON.parse(readFileSync('src-tauri/tauri.conf.json', 'utf8')).version !== version) throw new Error('Portable version differs from the build');
  if (!readFileSync(join(folder, portable)).length || !/^[A-Za-z0-9+/=\r\n]+$/.test(readFileSync(join(folder, portable + '.sig'), 'utf8').trim())) throw new Error('Missing signed portable archive');
  writeFileSync(join(folder, portable + '.sha256'), required.map(file => `${createHash('sha256').update(readFileSync(join(folder, file))).digest('hex')}  ${file}`).join('\n') + '\n');
  required.push(portable + '.sha256');
} else {
  const manifest = JSON.parse(readFileSync(join(folder, 'rust-updates.json'), 'utf8'));
  if (manifest.version !== version || Object.keys(releasePlatforms).some(platform => !manifest.platforms?.[platform])) throw new Error('Incomplete update manifest');
  required.push('SHA256SUMS', 'rust-updates.json');
  required.push(`HuifaDownloader-${version}-windows-arm64-portable.zip`, `HuifaDownloader-${version}-windows-arm64-portable.zip.sig`, `HuifaDownloader-${version}-macos-x64.dmg`, `HuifaDownloader-${version}-macos-arm64.dmg`);
  for (const entry of Object.values(manifest.platforms)) {
    const filename = decodeURIComponent(new URL(entry.url).pathname.split('/').at(-1));
    if (!filename || filename.includes('/') || filename.includes('\\')) throw new Error('Unsafe artifact filename');
    required.push(filename, filename + '.sig');
  }
}
for (const file of required) if (!statSync(join(folder, file)).isFile()) throw new Error(`Missing artifact: ${file}`);

const reference = api(`repos/${repository}/git/ref/tags/${tag}`, true);
if (portableOnly) {
  if (!reference) throw new Error('Portable additions require an existing release tag');
} else if (reference) {
  let object = reference.object;
  for (let depth = 0; object.type === 'tag' && depth < 4; depth++) object = api(`repos/${repository}/git/tags/${object.sha}`).object;
  if (object.type !== 'commit' || object.sha !== sha) throw new Error('Release tag points to another commit');
} else {
  gh(['api', `repos/${repository}/git/refs`, '--method', 'POST', '-f', `ref=refs/tags/${tag}`, '-f', `sha=${sha}`]);
}

let release = releaseForTag(repository, tag);
if (portableOnly && (!release || release.draft)) throw new Error('Portable additions require an existing published release');
const files = portableOnly ? required : readdirSync(folder).filter(isReleaseAsset);
const notes = renderReleaseNotes(version, rawNotes, portableOnly ? [...release.assets.map(asset => asset.name), ...files] : files);
if (!release) {
  const generatedNotes = join(folder, 'release-notes.md');
  writeFileSync(generatedNotes, notes);
  gh(['release', 'create', tag, '--repo', repository, '--target', sha, '--draft', '--title', `Huifa Downloader ${version}`, '--notes-file', generatedNotes]);
  release = releaseForTag(repository, tag);
}
if (![(notes).trim(), rawNotes.trim()].includes((release.body ?? '').trim())) throw new Error('Existing release notes do not match this build');
for (const file of files) {
  const path = join(folder, file);
  const existing = release.assets.find(asset => asset.name === file);
  if (existing) {
    const digest = 'sha256:' + createHash('sha256').update(readFileSync(path)).digest('hex');
    if (existing.size !== statSync(path).size || existing.digest !== digest) {
      throw new Error(`Published asset differs: ${file}; existing assets are never overwritten`);
    }
  } else {
    if (!release.draft && !portableOnly) throw new Error(`A published release is incomplete: ${file}; refusing to alter it`);
    gh(['release', 'upload', tag, path, '--repo', repository]);
  }
}
if (release.draft) gh(['release', 'edit', tag, '--repo', repository, '--draft=false', '--latest']);
console.log(`${portableOnly ? 'Added portable files to' : 'Published'} ${repository} ${tag} from ${sha}`);
