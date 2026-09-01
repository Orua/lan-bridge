const test = require('node:test');
const assert = require('node:assert/strict');

const {
  bridgeRestartDelayMs,
  readServerBooleanSetting,
  shouldRecoverUnownedBridge,
  shouldRestartBridge,
} = require('../dist-electron/bridgeLifecycle.js');

test('manual stop never restarts the bridge', () => {
  assert.equal(shouldRestartBridge({
    desiredRunning: false,
    isQuitting: false,
    restartCount: 0,
    maxRestarts: 5,
  }), false);
});

test('application quit never restarts the bridge', () => {
  assert.equal(shouldRestartBridge({
    desiredRunning: true,
    isQuitting: true,
    restartCount: 0,
    maxRestarts: 5,
  }), false);
});

test('an unrequested clean exit is restarted', () => {
  assert.equal(shouldRestartBridge({
    desiredRunning: true,
    isQuitting: false,
    restartCount: 0,
    maxRestarts: 5,
  }), true);
});

test('restart remains enabled after the former retry limit', () => {
  assert.equal(shouldRestartBridge({
    desiredRunning: true,
    isQuitting: false,
    restartCount: 5,
    maxRestarts: 5,
  }), true);
});

test('restart backoff is bounded without giving up', () => {
  assert.equal(bridgeRestartDelayMs(0), 1000);
  assert.equal(bridgeRestartDelayMs(3), 8000);
  assert.equal(bridgeRestartDelayMs(100), 30000);
});

test('desktop lifecycle settings are read from the server mapping', () => {
  const yaml = [
    'providers:',
    '  sample:',
    '    auto_start: true',
    'server:',
    '  auto_start: false',
    '  launch_at_login: true',
    '  close_to_tray: false',
    'model_mapping: {}',
  ].join('\n');

  assert.equal(readServerBooleanSetting(yaml, 'auto_start', true), false);
  assert.equal(readServerBooleanSetting(yaml, 'launch_at_login', false), true);
  assert.equal(readServerBooleanSetting(yaml, 'close_to_tray', true), false);
});

test('missing or malformed lifecycle settings keep safe defaults', () => {
  const yaml = 'server:\n  auto_start: sometimes\n';
  assert.equal(readServerBooleanSetting(yaml, 'auto_start', true), true);
  assert.equal(readServerBooleanSetting(yaml, 'launch_at_login', false), false);
});

test('watchdog recovers only a desired unowned bridge', () => {
  assert.equal(shouldRecoverUnownedBridge({
    desiredRunning: true,
    isQuitting: false,
    hasChildProcess: false,
    startInFlight: false,
  }), true);
  for (const override of [
    { desiredRunning: false },
    { isQuitting: true },
    { hasChildProcess: true },
    { startInFlight: true },
  ]) {
    assert.equal(shouldRecoverUnownedBridge({
      desiredRunning: true,
      isQuitting: false,
      hasChildProcess: false,
      startInFlight: false,
      ...override,
    }), false);
  }
});
