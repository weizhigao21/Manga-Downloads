"""Qt6 漫画浏览器应用入口。"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ui import __version__
from ui.main_window import MainWindow


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("漫画浏览器")
    app.setApplicationDisplayName("漫画浏览器")
    app.setApplicationVersion(__version__)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
