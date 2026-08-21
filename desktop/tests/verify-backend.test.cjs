const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { MIN_BACKEND_BYTES, verifySourceBackend } = require('../scripts/verify-backend.cjs');

function withReleaseTree(run) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-package-test-'));
  const desktop = path.join(root, 'desktop');
  fs.mkdirSync(desktop);
  try {
    run({ root, desktop, backend: path.join(root, 'dist-backend', 'lan-bridge.exe') });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test('packaging fails when backend is missing', () => withReleaseTree(({ desktop }) => {
  assert.throws(() => verifySourceBackend(desktop, 'win32'), /backend is missing/i);
}));

test('packaging fails when backend is truncated', () => withReleaseTree(({ desktop, backend }) => {
  fs.mkdirSync(path.dirname(backend));
  fs.writeFileSync(backend, 'not an executable');
  assert.throws(() => verifySourceBackend(desktop, 'win32'), /invalid or truncated/i);
}));

test('packaging accepts a non-truncated backend', () => withReleaseTree(({ desktop, backend }) => {
  fs.mkdirSync(path.dirname(backend));
  fs.writeFileSync(backend, Buffer.alloc(MIN_BACKEND_BYTES));
  assert.equal(verifySourceBackend(desktop, 'win32'), backend);
}));
