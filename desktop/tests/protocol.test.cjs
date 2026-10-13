const assert = require('node:assert/strict');
const { test } = require('node:test');
const { validateCommand, externalUrl } = require('../dist/protocol.js');
test('commands reject prototype methods, extra fields and unbounded payloads', () => {
  assert.deepEqual(validateCommand({ target: 'app', action: 'startTask', args: ['safe'] }).args, ['safe']);
  for (const value of [null, { target: '__proto__', action: 'startTask', args: [] }, { target: 'app', action: 'x', args: [], path: 'C:/' }, { target: 'app', action: 'x', args: ['x'.repeat(1048576)] }]) assert.throws(() => validateCommand(value));
});
test('external links accept HTTPS without embedded credentials', () => {
  assert.equal(externalUrl('https://github.com/'), 'https://github.com/');
  for (const url of ['file:///C:/Windows', 'javascript:alert(1)', 'https://secret@example.com/', 'http://example.com']) assert.throws(() => externalUrl(url));
});
test('Velopack restart flags retain the adjacent version and skip ordinary app arguments', () => {
  const { lifecycleArguments } = require('../dist/protocol.js');
  assert.deepEqual(lifecycleArguments(['desktop', '--veloapp-restarted', '0.1.16', '--remote-debugging-port=9393']), ['--veloapp-restarted', '0.1.16']);
  assert.deepEqual(lifecycleArguments(['--veloapp-updated=0.1.16', '--foo']), ['--veloapp-updated=0.1.16']);
});
