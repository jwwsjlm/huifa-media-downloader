import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

if (process.platform !== 'win32') throw new Error('This smoke test requires Windows WebView2 and Edge or Chrome');
const cwd = fileURLToPath(new URL('../', import.meta.url));
execFileSync(process.execPath, ['node_modules/typescript/bin/tsc', '--noEmit'], { cwd: fileURLToPath(new URL('../frontend/', import.meta.url)), stdio: 'inherit' });
execFileSync(process.execPath, ['node_modules/vite/bin/vite.js', 'build'], { cwd: fileURLToPath(new URL('../frontend/', import.meta.url)), stdio: 'inherit' });
mkdirSync(new URL('../runtime-tools/', import.meta.url), { recursive: true });
execFileSync(process.execPath, ['scripts/prepare-mcp-bridge.mjs', '--debug'], { cwd, stdio: 'inherit' });
const output = execFileSync('cargo', ['test', '--locked', '-p', 'huifa-desktop', '--features', 'login-smoke', '--no-run', '--message-format=json'], { cwd, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024, stdio: ['ignore', 'pipe', 'inherit'] });
const artifact = output.split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line)).find(item => item.reason === 'compiler-artifact' && item.target.name === 'huifa_desktop_lib' && item.profile.test && item.executable);
if (!artifact) throw new Error('Native login test executable was not built');
// Cargo test executables do not receive Tauri's binary-only Common Controls manifest.
writeFileSync(`${artifact.executable}.manifest`, `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <dependency><dependentAssembly><assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*" /></dependentAssembly></dependency>
</assembly>
`);
let manifestTool;
try { manifestTool = execFileSync('where.exe', ['mt.exe'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim().split(/\r?\n/)[0]; } catch {}
if (!manifestTool) {
  const sdk = path.join(process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'Windows Kits', '10', 'bin');
  manifestTool = readdirSync(sdk).filter(name => /^\d+\.\d+\.\d+\.\d+$/.test(name))
    .sort((a, b) => b.localeCompare(a, 'en', { numeric: true }))
    .map(version => path.join(sdk, version, 'x64', 'mt.exe')).find(existsSync);
}
if (!manifestTool) throw new Error('Windows SDK mt.exe is required to prepare the WebView2 test executable');
execFileSync(manifestTool, ['-nologo', '-manifest', `${artifact.executable}.manifest`, `-outputresource:${artifact.executable};#1`], { cwd, stdio: 'inherit' });
execFileSync(artifact.executable, ['login_browser::smoke::', '--include-ignored', '--test-threads=1', '--nocapture'], { cwd, stdio: 'inherit', timeout: 180_000 });
