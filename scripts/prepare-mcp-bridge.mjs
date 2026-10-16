import { execFileSync } from 'node:child_process';
import { copyFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const profile = process.argv.includes('--debug') ? 'debug' : 'release';
const metadata = JSON.parse(execFileSync('cargo', ['metadata', '--locked', '--no-deps', '--format-version', '1'], { cwd: root, encoding: 'utf8' }));
const target = execFileSync('rustc', ['-vV'], { encoding: 'utf8' }).match(/^host: (.+)$/m)?.[1];
if (!target) throw new Error('Cannot determine MCP bridge target');
execFileSync('cargo', ['build', '--locked', '-p', 'huifa-mcp', '--bin', 'huifa-mcp-bridge', ...(profile === 'release' ? ['--release'] : [])], { cwd: root, stdio: 'inherit' });
const extension = process.platform === 'win32' ? '.exe' : '';
const source = path.join(metadata.target_directory, profile, `huifa-mcp-bridge${extension}`);
mkdirSync(path.join(root, 'src-tauri/binaries'), { recursive: true });
copyFileSync(source, path.join(root, 'src-tauri/binaries', `huifa-mcp-bridge-${target}${extension}`));
