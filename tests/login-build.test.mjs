import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

for (const mode of ['local', 'prebuilt', 'missing']) {
  test(`native login verification preserves its checks with ${mode} frontend`, () => {
    const result = spawnSync(process.execPath, ['--input-type=module', '-e', `
      import assert from 'node:assert/strict';
      import cp from 'node:child_process';
      import fs from 'node:fs';
      import { syncBuiltinESMExports } from 'node:module';
      Object.defineProperty(process, 'platform', {value:'win32'});
      const mode = process.env.HUIFA_TEST_FRONTEND;
      process.argv = [process.execPath, 'check-login-browser.mjs', ...(mode === 'local' ? [] : ['--prebuilt-frontend'])];
      const calls = [];
      fs.existsSync = () => mode !== 'missing';
      fs.mkdirSync = () => {};
      fs.writeFileSync = () => {};
      cp.execFileSync = (command, args) => {
        calls.push([command, args]);
        if (command === 'cargo') return JSON.stringify({reason:'compiler-artifact',target:{name:'huifa_desktop_lib'},profile:{test:true},executable:'native-login-test.exe'});
        if (command === 'where.exe') return 'mt.exe';
      };
      syncBuiltinESMExports();
      if (mode === 'missing') {
        await assert.rejects(import(process.env.HUIFA_TEST_SCRIPT), /checked frontend artifact is required/);
        assert.deepEqual(calls, []);
      } else {
        await import(process.env.HUIFA_TEST_SCRIPT);
        const builds = calls.filter(([, args]) => args[0].includes('typescript') || args[0].includes('vite'));
        assert.equal(builds.length, mode === 'local' ? 2 : 0);
        assert(calls.some(([command, args]) => command === 'cargo' && args.includes('login-smoke')));
        assert(calls.some(([command]) => command === 'mt.exe'));
        assert(calls.some(([command, args]) => command === 'native-login-test.exe' && args.includes('--include-ignored')));
      }
    `], { encoding: 'utf8', env: { ...process.env, HUIFA_TEST_FRONTEND: mode, HUIFA_TEST_SCRIPT: new URL('../scripts/check-login-browser.mjs', import.meta.url).href } });
    assert.equal(result.status, 0, result.stderr);
  });
}
