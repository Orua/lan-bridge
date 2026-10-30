// Verify the actual desktop archive, not just source package metadata.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { verifyPackagedBackend } = require('./verify-backend.cjs');

async function main() {
  const asar = await import('@electron/asar');
  const root = path.resolve(__dirname, '../..');
  const version = fs.readFileSync(path.join(root, 'VERSION'), 'utf8').trim();
  const output = path.resolve(process.argv[2] || path.join(root, 'agent/output/windows', version));
  const manifestPath = path.join(output, 'RELEASE.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const sourceCommit = execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], { encoding: 'utf8', windowsHide: true }).trim();
  if (manifest.version !== version || manifest.sourceCommit !== sourceCommit || manifest.sourceClean !== true) {
    throw new Error('Release source manifest does not match this checkout. Rebuild from the committed source.');
  }
  const appDir = path.join(output, 'win-unpacked');
  const desktop = path.join(appDir, 'LAN BRIDGE.exe');
  const backend = verifyPackagedBackend(appDir, 'win32');
  const archive = path.join(appDir, 'resources/app.asar');
  const pkg = JSON.parse(asar.extractFile(archive, 'package.json').toString());
  if (pkg.version !== version) throw new Error(`Desktop version ${pkg.version} != ${version}`);
  const backendVersion = execFileSync(backend, ['--version'], { encoding: 'utf8', windowsHide: true }).trim();
  if (backendVersion !== `lan-bridge, version ${version}`) throw new Error(`Backend version mismatch: ${backendVersion}`);
  const html = asar.extractFile(archive, 'dist/index.html').toString();
  if (!html.includes('Content-Security-Policy') || !html.includes('connect-src http://localhost:')) {
    throw new Error('Packaged desktop is missing its local-only CSP.');
  }
  const jsAssets = asar.listPackage(archive).filter(name => /[\\/]dist[\\/]assets[\\/].*\.js$/.test(name));
  if (!jsAssets.some(name => asar.extractFile(archive, name.replace(/^[/\\]/, '')).includes(Buffer.from(version)))) {
    throw new Error('UI version is missing from the packaged JavaScript.');
  }
  const files = [desktop, backend, archive, manifestPath];
  for (const name of [`LAN BRIDGE Setup ${version}.exe`, `LAN BRIDGE ${version}.exe`]) {
    const file = path.join(output, name);
    if (!fs.existsSync(file)) throw new Error(`Release asset missing: ${name}`);
    files.push(file);
  }
  const checksums = files.map(file => `${crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')}  ${path.relative(output, file).replaceAll('\\', '/')}`).join('\n') + '\n';
  if (process.argv.includes('--write-checksums')) fs.writeFileSync(path.join(output, 'SHA256SUMS.txt'), checksums);
  else if (fs.readFileSync(path.join(output, 'SHA256SUMS.txt'), 'utf8') !== checksums) throw new Error('Release checksum mismatch. Rebuild before starting.');
  process.stdout.write(`Verified LAN BRIDGE ${version}: executable names, backend, desktop/UI versions, CSP and SHA-256.\n`);
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
