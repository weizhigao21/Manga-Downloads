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
        # 环境里装过、被 hook 误收集进打包的非项目依赖
        "pythonnet",
        "sounddevice",
        "werkzeug",
        "flask",
        "websockets",
        "safehttpx",
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
        "PySide6.QtSvg",
        "PySide6.QtVirtualKeyboard",
    ],
    noarchive=False,
)

# PySide6 hook 会全量收集包内所有 DLL（excludes 拦不住），
# 手动过滤未使用的 Qt 模块动态库（QML/Quick/PDF/VirtualKeyboard/Svg 等，纯 Widgets 应用用不到）
_QT_UNUSED_KEYWORDS = (
    "qt6quick",
    "qt6qml",
    "qt6pdf",
    "qt6qmlmeta",
    "qt6qmlmodels",
    "qt6qmlworkerscript",
    "qt6opengl",
    "opengl32sw",
    "virtualkeyboard",
    "qt6svg",
    "qsvg",
    "qt6quickcontrols2",
)

# 本机环境里被 hook 误收集、项目完全用不到的动态库（排除纯模块拦不住 DLL）
_UNUSED_BIN_KEYWORDS = (
    "pythonnet",        # pythonnet CLR 运行时
    "_sounddevice_data",
    "portaudio",        # sounddevice 音频库
    "win32evtlog",      # pywin32 残留
    "pywintypes",
    "win32comext",
    "win32wnet",
    "win32security",
    "win32service",
    "win32api",
    "win32gui",
    "win32com",
    "werkzeug",
    "websockets",
)


def _keep_qt_binary(entry):
    """返回 False 则从打包中移除该动态库。"""
    name = entry[0].lower()
    if any(k in name for k in _QT_UNUSED_KEYWORDS):
        return False
    if any(k in name for k in _UNUSED_BIN_KEYWORDS):
        return False
    return True


a.binaries = [e for e in a.binaries if _keep_qt_binary(e)]

# Qt 翻译文件与 dist-info 元数据也走 datas（excludes 拦不住），同样手动过滤：
# - 翻译只保留中英文（项目未装 QTranslator，界面文字为硬编码中文，.qm 仅兜底）
# - 丢弃被误收集包（werkzeug/websockets/flask/sounddevice/pythonnet）的元数据残留
_KEEP_TRANSLATIONS = {
    "qtbase_zh_cn.qm",
    "qtbase_zh_tw.qm",
    "qtbase_en.qm",
    "qt_zh_cn.qm",
    "qt_zh_tw.qm",
    "qt_en.qm",
}
_DROP_DATA_PACKAGES = (
    "werkzeug",
    "websockets",
    "flask",
    "sounddevice",
    "pythonnet",
)


def _keep_data(entry):
    """返回 False 则从打包中移除该数据文件。"""
    # PyInstaller 在 Windows 上 TOC 用反斜杠，统一转正斜杠再匹配
    name = entry[0].lower().replace("\\", "/")
    for pkg in _DROP_DATA_PACKAGES:
        if f"{pkg}-" in name and ".dist-info" in name:
            return False
    if name.startswith("pythonnet/"):
        return False
    if "/translations/" in name and name.endswith(".qm"):
        return name.rsplit("/", 1)[1] in _KEEP_TRANSLATIONS
    return True


a.datas = [e for e in a.datas if _keep_data(e)]

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
