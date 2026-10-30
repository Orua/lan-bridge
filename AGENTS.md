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

Every release/deployment must include all intended product changes, including new untracked modules and their dependencies/tests. Review the remaining Git diff explicitly; do not silently publish an older committed subset as the latest complete product. Resolve or report unfinished features before release.

Use `scripts/windows.ps1 -Action Build` from the final committed source. It rejects dirty product files, writes version-isolated output under `agent/output/windows/<VERSION>/`, and records the exact source commit in `RELEASE.json`. Agent notes/backups/temp/output are not product source.

Publish only the current version's Setup, portable, checksums and source manifest. Confirm the published asset hashes match the verified local files and the release tag points to the manifest's source commit. Never replace an existing version's binaries with different source; increment VERSION for a new complete release.

Before local runtime deployment, back up the fixed runtime to G:, deploy this same verified unpacked release, then use one direct status check to confirm the running version. Report separately whether source push, release publication and local runtime deployment actually happened; do not imply one proves the others.

Before deployment, run the Python tests, desktop typecheck/build/tests, build
the backend, and package Electron. Verify the staged executable names, hashes,
embedded CSP, and displayed version. Back up the current fixed runtime to `G:`
before mirroring the verified unpacked release into `D:\code_cn_bridge`.
