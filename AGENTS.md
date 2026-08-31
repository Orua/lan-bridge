# LAN BRIDGE Maintenance Entry

This is the only active LAN BRIDGE source repository. The retired
`C:\Users\Administrator\Documents\CODE-CN-BRIDGE` repository must never be
used to build or deploy the product.

## Fixed paths

- Source: `C:\Users\Administrator\Documents\LAN-BRIDGE`
- Runtime: `D:\code_cn_bridge`
- Release staging: `C:\Users\Administrator\lan_bridge_release`
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
