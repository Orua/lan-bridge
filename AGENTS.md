# LAN BRIDGE Maintenance Entry

## Install/start requests: read this first

- The supported user product is the Windows x64 Electron desktop with its bundled backend.
- Read `README.md` and `docs/WINDOWS.md` before installing or helping a user start it.
- Prefer the latest GitHub Release **Setup** executable. No Python/Node is needed to run it.
- From a source checkout use `scripts/windows.ps1 -Action Build`, then `-Action Start`.
- Never present `pip install -e .`, `lan-bridge start`, a Chrome tab, or Vite on 5173 as a completed desktop installation.
- `npm run dev` / `electron:dev` launch the development desktop. `ui:dev` is only frontend debugging.
- Preserve Codex login. The bridge reads host file credentials; it does not use browser cookies.
- A client needs a real Bridge access key; never suggest `any-value`.
- See `docs/DEVELOPMENT.md` for build outputs and publishing. The fixed paths below apply only to this maintainer's computer, not end-user installations.

This is the only active LAN BRIDGE source repository. The retired
`C:\Users\Administrator\Documents\CODE-CN-BRIDGE` repository must never be
used to build or deploy the product.

## Fixed paths

- Source: `C:\Users\Administrator\Documents\LAN-BRIDGE`
- Runtime: `D:\code_cn_bridge`
- Release staging: `agent/output/windows/` under the active source checkout
- Recovery backups: `G:\CODE-CN-BRIDGE-BACKUPS`

The desktop shortcut and deployed executable must be named `LAN BRIDGE`.
The backend must be `resources\backend\lan-bridge.exe`.

## Version discipline

`VERSION` is the release version source of truth. Change versions only with:

```powershell
python scripts\version.py --set X.Y.Z
python scripts\version.py --check
```

The check must pass before building. Desktop packaging runs it automatically.
Runtime API, CLI, desktop package metadata, and the About page must report the
same version.

## Release checks

Before deployment, run the Python tests, desktop typecheck/build/tests, build
the backend, and package Electron. Verify the staged executable names, hashes,
embedded CSP, and displayed version. Back up the current fixed runtime to `G:`
before mirroring the verified unpacked release into `D:\code_cn_bridge`.
