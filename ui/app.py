"""Qt6 漫画浏览器应用入口。"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ui import __version__
from ui.main_window import MainWindow


def _icon_path() -> Path:
    """定位程序图标 Config/ui.ico。

    兼容三种运行环境：
    - 源码运行：项目根/Config/ui.ico
    - PyInstaller 打包（onefile/onedir）：解压临时目录 _MEIPASS/Config/ui.ico
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "Config" / "ui.ico"
    return Path(__file__).resolve().parent.parent / "Config" / "ui.ico"


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("漫画浏览器")
    app.setApplicationDisplayName("漫画浏览器")
    app.setApplicationVersion(__version__)
    icon = _icon_path()
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    window = MainWindow()
    if icon.exists():
        window.setWindowIcon(QIcon(str(icon)))
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
