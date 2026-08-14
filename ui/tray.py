"""系统托盘控制：最小化到托盘后台运行 + 下载完成系统通知。

- TrayController：创建 QSystemTrayIcon 与右键菜单（显示主窗口 / 退出），
  关闭窗口时最小化到托盘继续后台下载，托盘菜单「退出」才真正退出程序。
- NotifyMerger：将短时间内的多条完成通知合并为一条，避免批量下载完成时
  通知刷屏。
- 托盘不可用（无系统托盘环境，如 offscreen 测试/精简桌面）时所有操作
  安全降级为 no-op，不影响主流程。
"""
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

# 通知合并窗口（毫秒）：窗口内的多条通知合并为一条
NOTIFY_MERGE_MS = 1000

_controller: "TrayController | None" = None


class NotifyMerger(QObject):
    """把短时间内的多条通知合并为一条后统一发出。

    push() 累计待发标题并（重新）启动合并计时器；窗口到期后发出
    flushed(list)，调用方自行展示。窗口内只发一次，避免刷屏。
    """

    flushed = Signal(list)  # 合并后的标题列表

    def __init__(self, window_ms: int = NOTIFY_MERGE_MS, parent: QObject | None = None):
        super().__init__(parent)
        self._window_ms = window_ms
        self._pending: list[str] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(window_ms)
        self._timer.timeout.connect(self._flush)

    def push(self, title: str):
        """累计一条待通知标题（重复标题去重）。"""
        if title and title not in self._pending:
            self._pending.append(title)
        self._timer.start()

    def _flush(self):
        if not self._pending:
            return
        batch = self._pending
        self._pending = []
        self.flushed.emit(batch)

    @property
    def pending(self) -> list[str]:
        """当前累计但尚未发出的标题（测试用）。"""
        return list(self._pending)


class TrayController(QObject):
    """系统托盘单例控制器。

    窗口关闭时（托盘可用且非退出流程）将窗口隐藏而非退出，下载在后台
    继续；托盘菜单「显示主窗口」恢复窗口，「退出」走正常收尾退出。
    """

    def __init__(
        self,
        icon_path: str | None = None,
        parent: QObject | None = None,
        available: bool | None = None,
    ):
        super().__init__(parent)
        if available is None:
            available = QSystemTrayIcon.isSystemTrayAvailable()
        self._available = bool(available)
        self._window = None
        self._quitting = False
        self._minimize_hint_shown = False
        self._tray: QSystemTrayIcon | None = None
        self._icon = None
        if icon_path and Path(icon_path).exists():
            self._icon = QIcon(str(icon_path))
        # 通知合并器：完成通知统一经它合并，避免批量下载刷屏
        self._merger = NotifyMerger(parent=self)
        self._merger.flushed.connect(self._show_merged_notify)
        if self._available:
            self._tray = QSystemTrayIcon(self._icon or QIcon(), self)
            self._tray.setToolTip("漫画浏览器（后台运行中）")
            menu = QMenu()
            show_action = menu.addAction("显示主窗口")
            show_action.triggered.connect(self.show_window)
            menu.addSeparator()
            quit_action = menu.addAction("退出")
            quit_action.triggered.connect(self.quit_app)
            self._tray.setContextMenu(menu)
            self._tray.activated.connect(self._on_activated)
            self._tray.show()

    # ---- 状态 ----

    @property
    def available(self) -> bool:
        """当前是否真正启用了托盘（不可用时窗口关闭仍按原逻辑退出）。"""
        return self._available

    @property
    def is_quitting(self) -> bool:
        """是否处于「托盘菜单退出」流程（窗口关闭事件应放行退出）。"""
        return self._quitting

    def attach(self, window):
        """关联主窗口（显示/隐藏与退出流程使用）。"""
        self._window = window

    # ---- 窗口控制 ----

    def show_window(self):
        """显示并前置主窗口（托盘双击 / 菜单触发）。"""
        if self._window is not None:
            self._window.showNormal()
            self._window.raise_()
            self._window.activateWindow()

    def on_window_hidden(self):
        """窗口被关闭（隐藏到托盘）后调用：首次提示一次后台运行说明。"""
        if self._available and not self._minimize_hint_shown and self._tray is not None:
            self._minimize_hint_shown = True
            self._tray.showMessage(
                "漫画浏览器",
                "已最小化到系统托盘，下载任务将在后台继续运行。",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    def quit_app(self):
        """托盘菜单「退出」：标记退出流程并关闭应用（走正常收尾）。"""
        self._quitting = True
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ---- 通知 ----

    def notify_download_done(self, title: str):
        """报告一条下载完成事件（多条自动合并，托盘不可用时静默）。"""
        if self._available and self._tray is not None:
            self._merger.push(title)

    def _show_merged_notify(self, titles: list[str]):
        if not self._available or self._tray is None:
            return
        if len(titles) == 1:
            body = f"「{titles[0]}」下载完成"
        else:
            body = f"{len(titles)} 个任务下载完成：{titles[0]} 等"
        self._tray.showMessage(
            "下载完成",
            body,
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason):
        """托盘图标激活（单击/双击）时显示主窗口。"""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window()


def get_tray(icon_path: str | None = None) -> TrayController:
    """获取全局托盘控制器（懒创建，首次调用可传图标路径）。"""
    global _controller
    if _controller is None:
        _controller = TrayController(icon_path)
    return _controller


def reset_tray():
    """重置全局托盘控制器（测试隔离用）。"""
    global _controller
    _controller = None
