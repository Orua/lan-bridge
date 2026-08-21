# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = [
    "code_cn_bridge",
    "code_cn_bridge.adapters",
    "code_cn_bridge.adapters.deepseek",
    "code_cn_bridge.adapters.qwen",
    "code_cn_bridge.adapters.kimi",
    "code_cn_bridge.adapters.doubao",
    "code_cn_bridge.adapters.glm",
    "code_cn_bridge.adapters.openai",
]

collected = collect_all("code_cn_bridge")
datas += collected[0]
binaries += collected[1]
hiddenimports += collected[2]

a = Analysis(
    ["_entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="lan-bridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
