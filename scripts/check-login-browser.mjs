import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

if (process.platform !== 'win32') throw new Error('This smoke test requires Windows WebView2 and Edge or Chrome');
const cwd = fileURLToPath(new URL('../', import.meta.url));
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
execFileSync(artifact.executable, ['real_login_cookie_export_persistence_popups_and_ipc_isolation', '--ignored', '--test-threads=1', '--nocapture'], { cwd, stdio: 'inherit' });
