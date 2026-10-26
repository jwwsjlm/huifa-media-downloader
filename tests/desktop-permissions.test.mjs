import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

test('every desktop command is declared and granted only to its intended webview', () => {
  const read = path => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
  const registered = read('src-tauri/src/lib.rs').match(/generate_handler!\[([\s\S]*?)\]/)[1].split(',').map(name => name.trim()).filter(Boolean).sort();
  const declarations = read('src-tauri/build.rs').match(/\.commands\(\s*&\s*\[([\s\S]*?)\]\s*\)/)[1];
  const declared = [...declarations.matchAll(/"([a-z_]+)"/g)].map(match => match[1]).sort();
  assert.deepEqual(declared, registered);
  const main = JSON.parse(read('src-tauri/capabilities/main.json'));
  const toolbar = JSON.parse(read('src-tauri/capabilities/login-toolbar.json'));
  assert.deepEqual(main.windows, ['main']);
  assert.deepEqual(main.permissions.filter(permission => permission.startsWith('allow-')).sort(), registered.filter(command => command !== 'login_navigation').map(command => `allow-${command.replaceAll('_', '-')}`).sort());
  assert.deepEqual(toolbar.webviews, ['login-*-toolbar']);
  assert.deepEqual(toolbar.permissions, ['allow-login-navigation']);
});
