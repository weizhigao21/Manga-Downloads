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
    # 排除本机已安装但项目未使用的重型依赖：
    # PyInstaller 会扫描全部已装包的 hook，可能误收集 torch(CUDA ~3GB)/numba/ffmpeg 等
    excludes=[
        "torch",
        "torchvision",
        "torchaudio",
        "numba",
        "llvmlite",
        "imageio",
        "imageio_ffmpeg",
        "playwright",
        "numpy",
        "pandas",
        "scipy",
        "matplotlib",
        "sklearn",
        "scikit_learn",
        "keras",
        "tensorflow",
    ],
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
