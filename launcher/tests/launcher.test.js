import test from 'node:test';
import assert from 'node:assert/strict';
import { assertLoopbackAddress, chooseDestination, parseArgs } from '../src/launcher.js';

test('fresh and interrupted setup routes to setup; completed onboarding routes to the application', () => {
  assert.equal(chooseDestination(null), '/setup');
  assert.equal(chooseDestination({ completed: false }), '/setup');
  assert.equal(chooseDestination({ completed: true }), '/login');
});

test('launcher arguments are explicit and reject unknown options', () => {
  assert.deepEqual(parseArgs(['--development', '--simulated', '--no-browser', '--data-dir', '/tmp/sc-data']), {
    dataDirectory: '/tmp/sc-data', development: true, simulated: true, openBrowser: false,
  });
  assert.throws(() => parseArgs(['--bind', '0.0.0.0']), /Unknown launcher option/);
  assert.deepEqual(parseArgs(['--help']).help, true);
});

test('privileged local launch requires an explicit loopback bind', () => {
  assert.doesNotThrow(() => assertLoopbackAddress('127.0.0.1'));
  assert.throws(() => assertLoopbackAddress('0.0.0.0'), /bind explicitly to 127.0.0.1/);
});
