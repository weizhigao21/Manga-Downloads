# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：独立更新程序（单文件 exe）。

单文件（onefile）理由：更新程序会被主程序复制到 %TEMP% 单独运行（运行中的
exe 无法原地替换），一个文件最省事；且它只依赖标准库（进度窗用 tkinter）
与 `src/update_apply`，体积小、启动快，不引入 PySide6。

用法: pyinstaller 更新程序.spec
"""

a = Analysis(
    ["updater.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 更新程序不该拖上任何第三方依赖：本机装的重型库/误收集包全部排除
    excludes=[
        "PySide6",
        "shiboken6",
        "requests",
        "lxml",
        "opencc",
        "openai",
        "PIL",
        "Pillow",
        "curl_cffi",
        "numpy",
        "pandas",
        "scipy",
        "matplotlib",
        "torch",
        "torchvision",
        "torchaudio",
        "numba",
        "llvmlite",
        "imageio",
        "imageio_ffmpeg",
        "playwright",
        "sklearn",
        "scikit_learn",
        "keras",
        "tensorflow",
        "pywin32",
        "win32",
        "win32com",
        "win32api",
        "win32con",
        "win32gui",
        "Pythonwin",
        "pythonnet",
        "sounddevice",
        "werkzeug",
        "flask",
        "websockets",
        "safehttpx",
        # 主程序自己的模块（更新程序只用 src.update_apply，其余别带进来）
        "ui",
        "src.wnacg_crawler",
        "src.downloader",
        "src.net",
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
    name="更新程序",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["Config/ui.ico"],
)
