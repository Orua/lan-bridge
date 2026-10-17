const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');

// Exercise the actual compiled lifecycle functions with controlled probe timing.
function harness(probes = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../dist-electron/main.js'), 'utf8');
  const code = source.slice(source.indexOf('function cancelBridgeRestart()'), source.indexOf('function createWindow()'));
  let spawns = 0;
  let watchdog;
  const context = vm.createContext({
    bridgeProcess: null, bridgeDesiredRunning: true, isQuitting: false,
    bridgeStartInFlight: false, bridgeRestartTimer: null, bridgeStableTimer: null,
    bridgeWatchdogTimer: null, bridgeWatchdogChecking: false, bridgeHealthFailureCount: 0,
    bridgeOwnedByApp: false, bridgeRestartCount: 0, MAX_RESTART: 5, BRIDGE_PORT: 8765,
    STABLE_RUN_RESET_MS: 30000, STARTUP_HEALTH_TIMEOUT_MS: 30000,
    process, console: { log() {}, error() {} },
    writeDesktopLog() {}, safeSend() {},
    isDev: true,
    bridgeIsHealthy: probes.health || (async () => false),
    bridgePortIsOpen: probes.port || (async () => false),
    waitForBridgeHealthy: async () => true,
    getBridgeCommand: () => ({ cmd: 'fake-backend', args: [] }),
    child_process_1: { spawn() { spawns++; return Object.assign(new EventEmitter(), { exitCode: null, kill() {} }); } },
    bridgeLifecycle_1: require('../dist-electron/bridgeLifecycle.js'),
    setInterval(fn) { watchdog = fn; return 1; }, clearInterval() {},
    setTimeout() { return 1; }, clearTimeout() {},
  });
  vm.runInContext(code, context);
  return { context, spawns: () => spawns, watchdog: () => watchdog() };
}

for (const probe of ['health', 'port']) {
  for (const action of ['stop', 'quit']) {
    test(`${action} during ${probe} probe prevents backend startup`, async () => {
      let resolve;
      let entered;
      const ready = new Promise(r => { entered = r; });
      const h = harness({ [probe]: () => { entered(); return new Promise(r => { resolve = r; }); } });
      const pending = h.context.startBridgeProcess();
      await ready;
      if (action === 'stop') h.context.stopBridgeProcess();
      else h.context.isQuitting = true;
      resolve(false);
      await pending;
      assert.equal(h.spawns(), 0);
    });
  }
}

test('watchdog respects a scheduled crash backoff', async () => {
  const h = harness();
  h.context.bridgeRestartTimer = 123;
  h.context.startBridgeWatchdog();
  await h.watchdog();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.spawns(), 0);
});
