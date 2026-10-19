import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { execFileSync, spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

assert.equal(process.platform, 'darwin', 'This bundle check requires macOS');
const folder = path.resolve('target/release/bundle/macos');
const apps = readdirSync(folder).filter(name => name.endsWith('.app'));
assert.equal(apps.length, 1, 'Expected one application bundle');
const app = path.join(folder, apps[0]);
execFileSync('codesign', ['--verify', '--deep', '--strict', app], { stdio: 'inherit' });
const resources = path.join(app, 'Contents/Resources/tools');
const receipt = JSON.parse(readFileSync(path.join(resources, 'manifest.json'), 'utf8'));
assert.equal(receipt.target, `darwin-${process.arch === 'arm64' ? 'aarch64' : 'x86_64'}`);
for (const [name, expected] of Object.entries(receipt.files)) {
  assert.match(name, /^(yt-dlp|ffmpeg|ffprobe|qjs|deno)$/);
  assert.equal(createHash('sha256').update(readFileSync(path.join(resources, name))).digest('hex'), expected, `${name} changed during application signing`);
}
const executable = execFileSync('/usr/libexec/PlistBuddy', ['-c', 'Print :CFBundleExecutable', path.join(app, 'Contents/Info.plist')], { encoding: 'utf8' }).trim();
const directory = mkdtempSync(path.join(tmpdir(), 'huifa-macos-startup-'));
const child = spawn(path.join(app, 'Contents/MacOS', executable), [], {
  detached: true, stdio: ['ignore', 'ignore', 'pipe'],
  env: { ...process.env, HUIFA_DATA_DIR: path.join(directory, 'data'), HUIFA_DOWNLOAD_DIR: path.join(directory, 'downloads') },
});
let errors = '';
child.stderr.on('data', chunk => { errors = (errors + chunk).slice(-16000); });
let launchError;
child.on('error', error => { launchError = error; });
try {
  const deadline = Date.now() + 30000;
  while (!existsSync(path.join(directory, 'data/rust-core.sqlite3'))) {
    assert.ok(!launchError && child.exitCode === null && child.signalCode === null, `Desktop startup failed: ${launchError || errors}`);
    assert.ok(Date.now() < deadline, `Desktop did not initialize: ${errors}`);
    await delay(250);
  }
  await delay(2000);
  assert.equal(child.exitCode, null, `Desktop exited after initialization: ${errors}`);
  assert.equal(child.signalCode, null, `Desktop was terminated: ${errors}`);
  console.log(`Verified signed bundle, runtime hashes and isolated startup: ${app}`);
} finally {
  if (child.pid) {
    try { process.kill(-child.pid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
}
