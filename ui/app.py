"""Qt6 漫画浏览器应用入口。"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ui.download_manager import DownloadManager
from ui.main_window import MainWindow
from ui.settings import AppSettings
from ui.single_instance import SINGLE_INSTANCE_KEY, SingleInstanceGuard
from ui.threads import shutdown_all
from ui.tray import get_tray
from ui.version import __version__


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

    # 单实例守护：已有实例在运行时，通知其唤起主窗口后本进程直接退出
    guard = SingleInstanceGuard(SINGLE_INSTANCE_KEY)
    if not guard.is_primary:
        return 0

    window = MainWindow()
    if icon.exists():
        window.setWindowIcon(QIcon(str(icon)))
    window.show()

    # 系统托盘：关闭窗口最小化到托盘，下载在后台继续；
    # 托盘菜单「退出」才真正结束程序（见 ui/tray.py 与 main_window.closeEvent）
    tray = get_tray(str(icon) if icon.exists() else None)
    tray.attach(window)

    # 二次启动/重新双击程序时：唤起已最小化到托盘的主窗口，而非多开实例
    guard.showRequested.connect(tray.show_window)

    # 下载完成系统通知（可合并、可开关；走托盘气泡，无托盘时静默）
    def _on_task_finished(task, success: bool, error: str):
        if success and AppSettings().notify_on_download_done:
            tray.notify_download_done(task.title)

    DownloadManager.instance().task_finished.connect(_on_task_finished)

    # 应用退出兜底：统一收尾所有后台线程（下载线程 + 抓取/翻译线程）。
    # 短时等待后仍未结束的线程强制终止，保证退出不卡顿。
    # 主窗口关闭流程也会调用，此处确保即使窗口异常关闭也安全退出。
    app.aboutToQuit.connect(DownloadManager.instance().shutdown)
    app.aboutToQuit.connect(lambda: shutdown_all(timeout_ms=2000))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
