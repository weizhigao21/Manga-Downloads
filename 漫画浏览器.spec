# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成目录版（onedir）窗口程序。

onedir 模式启动时无需解压归档（onefile 每次启动都解压 ~80MB 到临时目录），
打开窗口速度明显更快；整个 dist/漫画浏览器/ 目录即完整程序，可整体分发。

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
        # 重型 AI/数据科学库
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
        # 本机已装但项目未使用的库（hook 误收集）
        "PIL",
        "Pillow",
        "cryptography",
        "pywin32",
        "win32",
        "win32com",
        "win32api",
        "win32con",
        "win32gui",
        "Pythonwin",
        # 未使用的 Qt 模块（项目为纯 Widgets 应用）
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.QtQuickWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtMultimedia",
        "PySide6.QtCharts",
        "PySide6.Qt3DCore",
        "PySide6.QtDataVisualization",
        "PySide6.QtWebSockets",
        "PySide6.QtBluetooth",
        "PySide6.QtSensors",
        "PySide6.QtPositioning",
        "PySide6.QtSql",
        "PySide6.QtTest",
        "PySide6.QtXml",
        "PySide6.QtDBus",
        "PySide6.QtDesigner",
        "PySide6.QtOpenGLWidgets",
    ],
    noarchive=False,
)

# PySide6 hook 会全量收集包内所有 DLL（excludes 拦不住），
# 手动过滤未使用的 Qt 模块动态库（QML/Quick/PDF 等，纯 Widgets 应用用不到）
_QT_UNUSED_KEYWORDS = (
    "qt6quick",
    "qt6qml",
    "qt6pdf",
    "qt6qmlmeta",
    "qt6qmlmodels",
    "qt6qmlworkerscript",
    "qt6opengl",
    "opengl32sw",
)


def _keep_qt_binary(entry):
    """返回 False 则从打包中移除该动态库。"""
    name = entry[0].lower()
    return not any(k in name for k in _QT_UNUSED_KEYWORDS)


a.binaries = [e for e in a.binaries if _keep_qt_binary(e)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="漫画浏览器",
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="漫画浏览器",
)
