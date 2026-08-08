# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成单文件窗口版 exe。

用法: pyinstaller 漫画浏览器.spec
"""

a = Analysis(
    ["run_ui.py"],
    pathex=[],
    binaries=[],
    datas=[("Config/ui.ico", "Config")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="漫画浏览器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["Config/ui.ico"],
)
