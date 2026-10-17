const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { writeRecoveryState } = require('../dist-electron/recovery.js');

test('manual stop, quit and restart replace recovery intent atomically', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lan-bridge-recovery-'));
  try {
    const file = path.join(dir, 'state.json');
    const state = { enabled: true, desiredRunning: true, desktopPid: 42, desktopExecutable: 'LAN BRIDGE.exe', version: 'test' };
    for (const update of [{}, { desiredRunning: false }, { enabled: false, desiredRunning: false }, { desktopPid: 43 }]) {
      writeRecoveryState(file, { ...state, ...update });
      const actual = JSON.parse(fs.readFileSync(file, 'utf8'));
      assert.equal(actual.enabled, update.enabled ?? state.enabled);
      assert.equal(actual.desiredRunning, update.desiredRunning ?? state.desiredRunning);
      assert.equal(actual.desktopPid, update.desktopPid ?? state.desktopPid);
      assert(actual.updatedAt);
      assert.deepEqual(fs.readdirSync(dir), ['state.json']);
    }
  } finally { fs.rmSync(dir, { recursive: true }); }
});

test('Windows watcher policy respects intentional stops, duplicate starts and path identity', { skip: process.platform !== 'win32' }, () => {
  const result = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', path.join(__dirname, 'recovery-policy.tests.ps1')], { encoding: 'utf8', windowsHide: true });
  assert.match(result, /Recovery policy: passed/);
});
