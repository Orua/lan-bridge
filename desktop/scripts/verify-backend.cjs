const fs = require('fs');
const path = require('path');

const MIN_BACKEND_BYTES = 1024 * 1024;

function backendName(platform) {
  return platform === 'win32' ? 'lan-bridge.exe' : 'lan-bridge';
}

function assertUsableBackend(filePath) {
  let stat;
  try {
    stat = fs.statSync(filePath);
  } catch {
    throw new Error(`Bridge backend is missing: ${filePath}. Build dist-backend before packaging.`);
  }
  if (!stat.isFile() || stat.size < MIN_BACKEND_BYTES) {
    throw new Error(`Bridge backend is invalid or truncated: ${filePath} (${stat.size} bytes).`);
  }
  return filePath;
}

function verifySourceBackend(desktopDir = path.resolve(__dirname, '..'), platform = process.platform) {
  return assertUsableBackend(path.resolve(desktopDir, '..', 'dist-backend', backendName(platform)));
}

function verifyPackagedBackend(appOutDir, platform = process.platform) {
  const resourceRoot = platform === 'darwin'
    ? path.join(appOutDir, 'LAN BRIDGE.app', 'Contents', 'Resources')
    : path.join(appOutDir, 'resources');
  return assertUsableBackend(path.join(resourceRoot, 'backend', backendName(platform)));
}

async function beforePack(context) {
  verifySourceBackend(context.packager.info.appDir, context.electronPlatformName);
}

async function afterPack(context) {
  verifyPackagedBackend(context.appOutDir, context.electronPlatformName);
}

module.exports = {
  MIN_BACKEND_BYTES,
  afterPack,
  assertUsableBackend,
  beforePack,
  verifyPackagedBackend,
  verifySourceBackend,
};

if (require.main === module) {
  const verified = verifySourceBackend();
  process.stdout.write(`Verified bridge backend: ${verified}\n`);
}
