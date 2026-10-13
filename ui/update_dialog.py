"""更新界面：检查新版本 → 下载更新包 → 启动独立更新程序并退出主程序。

流程（用户可见）：
1. 启动静默检查 / 设置页「检查更新」→ 后台查 GitHub Release；
2. 有新版本 → 弹窗展示版本与更新说明；
3. 点「下载更新包」→ 后台下载（进度条，可取消）；
4. 下载完成后按钮变「立即更新并重启」→ 启动独立更新程序、本程序退出。

线程约定与项目一致：QThread 的业务信号命名 `done`，清理只连原生
`finished`（绝不给 `done` 连接 deleteLater）。
"""
import logging
import os
import sys

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from src.update_apply import launch_updater
from src.update_check import (
    RELEASE_PAGE,
    UpdateError,
    download_asset,
    fetch_latest_release,
    is_newer,
)
from ui.styles import PALETTE
from ui.threads import register
from ui.version import __version__

logger = logging.getLogger(__name__)

_DIALOG_QSS = f"""
QDialog {{
    background-color: {PALETTE["background"]};
}}
QLabel {{
    color: {PALETTE["text"]};
    font-size: 13px;
}}
QLabel#updateSubtitle {{
    color: {PALETTE["text_secondary"]};
    font-size: 12px;
}}
QLabel#updateHint {{
    color: {PALETTE["text_secondary"]};
    font-size: 12px;
}}
QTextBrowser#updateNotes {{
    background-color: {PALETTE["surface"]};
    color: {PALETTE["text"]};
    border: 1px solid {PALETTE["border"]};
    border-radius: 6px;
    padding: 8px;
    font-size: 12px;
}}
QPushButton#updatePrimaryButton {{
    background-color: {PALETTE["primary"]};
    color: white;
    border: none;
    border-radius: 6px;
    padding: 7px 18px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#updatePrimaryButton:hover {{
    background-color: {PALETTE["primary_hover"]};
}}
QPushButton#updatePrimaryButton:disabled {{
    background-color: {PALETTE["border"]};
    color: {PALETTE["text_secondary"]};
}}
QPushButton#updateSecondaryButton {{
    background-color: {PALETTE["surface"]};
    color: {PALETTE["text"]};
    border: 1px solid {PALETTE["border"]};
    border-radius: 6px;
    padding: 7px 14px;
    font-size: 13px;
}}
QPushButton#updateSecondaryButton:hover {{
    background-color: {PALETTE["surface_hover"]};
}}
"""


def is_frozen() -> bool:
    """是否为打包后的可执行程序（源码运行时不支持自我更新）。"""
    return bool(getattr(sys, "frozen", False))


def _format_size(size: int) -> str:
    if size <= 0:
        return "大小未知"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _quit_application() -> None:
    """真正退出程序：走托盘的「退出」路径，避免被"关闭窗口=最小化到托盘"拦截。"""
    app = QApplication.instance()
    if app is None:
        return
    from ui import tray as tray_module

    controller = getattr(tray_module, "_controller", None)
    if controller is not None:
        controller.quit_app()
    else:
        app.quit()


# ---- 后台线程 ----


class UpdateCheckWorker(QThread):
    """后台查询最新版本（网络不可用时静默失败，不打扰阅读/下载）。"""

    done = Signal(object)   # ReleaseInfo
    failed = Signal(str)

    def run(self):
        try:
            release = fetch_latest_release()
        except UpdateError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # 兜底：任何异常都不允许逃逸线程
            logger.exception("检查更新异常")
            self.failed.emit(str(exc))
            return
        self.done.emit(release)


class UpdateDownloadWorker(QThread):
    """后台下载更新包（可取消：取消后删除半截文件）。"""

    progress = Signal(int, int)   # (已下载字节, 总字节)
    done = Signal(str)            # zip 路径
    failed = Signal(str)

    def __init__(self, release, parent=None):
        super().__init__(parent)
        self.release = release

    def cancel(self) -> None:
        self.requestInterruption()

    def run(self):
        try:
            path = download_asset(
                self.release,
                on_progress=self._emit_progress,
                cancel_check=self.isInterruptionRequested,
            )
        except UpdateError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:
            logger.exception("下载更新包异常")
            self.failed.emit(str(exc))
            return
        if not self.isInterruptionRequested():
            self.done.emit(str(path))

    def _emit_progress(self, done: int, total: int) -> None:
        if not self.isInterruptionRequested():
            self.progress.emit(done, total)


# ---- 更新对话框 ----


class UpdateDialog(QDialog):
    """发现新版本：展示说明 → 下载 → 立即更新并重启。"""

    def __init__(self, release, parent=None):
        super().__init__(parent)
        self.release = release
        self._zip_path: str | None = None
        self._download_worker: UpdateDownloadWorker | None = None

        self.setWindowTitle("发现新版本")
        self.setMinimumWidth(520)
        self.setStyleSheet(_DIALOG_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        title = QLabel(f"发现新版本 v{release.version}")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        layout.addWidget(title)

        subtitle = QLabel(
            f"当前版本 v{__version__}　→　新版本 v{release.version}"
            f"（{_format_size(release.asset_size)}）"
        )
        subtitle.setObjectName("updateSubtitle")
        layout.addWidget(subtitle)

        notes = QTextBrowser()
        notes.setObjectName("updateNotes")
        notes.setOpenExternalLinks(True)
        notes.setMinimumHeight(180)
        notes.setPlainText(str(release.notes or "（该版本没有填写更新说明）"))
        layout.addWidget(notes, 1)

        self.hint_label = QLabel("")
        self.hint_label.setObjectName("updateHint")
        layout.addWidget(self.hint_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        self.browser_btn = QPushButton("在浏览器中打开")
        self.browser_btn.setObjectName("updateSecondaryButton")
        self.browser_btn.setCursor(Qt.PointingHandCursor)
        self.browser_btn.clicked.connect(self._open_release_page)
        buttons.addWidget(self.browser_btn)
        buttons.addStretch()

        self.later_btn = QPushButton("稍后再说")
        self.later_btn.setObjectName("updateSecondaryButton")
        self.later_btn.setCursor(Qt.PointingHandCursor)
        self.later_btn.clicked.connect(self.reject)
        buttons.addWidget(self.later_btn)

        self.action_btn = QPushButton("下载更新包")
        self.action_btn.setObjectName("updatePrimaryButton")
        self.action_btn.setCursor(Qt.PointingHandCursor)
        self.action_btn.clicked.connect(self._on_action)
        buttons.addWidget(self.action_btn)
        layout.addLayout(buttons)

        if not is_frozen():
            # 源码运行时替换的是开发目录，风险大且无意义：只允许下载/查看
            self.hint_label.setText("开发模式下不支持自动更新，请手动替换或到发布页下载。")

    # ---- 交互 ----

    def _open_release_page(self) -> None:
        QDesktopServices.openUrl(QUrl(self.release.page_url))

    def _on_action(self) -> None:
        if self._zip_path is not None:
            self._apply_update()
            return
        self._start_download()

    def _start_download(self) -> None:
        self.action_btn.setEnabled(False)
        self.later_btn.setText("取消下载")
        self.progress.setVisible(True)
        self.hint_label.setText("正在下载更新包…")

        worker = UpdateDownloadWorker(self.release, parent=None)
        self._download_worker = worker
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_downloaded)
        worker.failed.connect(self._on_download_failed)
        worker.finished.connect(lambda w=worker: self._clear_worker(w))
        worker.finished.connect(worker.deleteLater)
        register(worker)
        worker.start()

    def _clear_worker(self, worker) -> None:
        if self._download_worker is worker:
            self._download_worker = None

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(100, int(done * 100 / total)))
            self.hint_label.setText(
                f"正在下载更新包… {done / 1024 / 1024:.1f} MB"
                f" / {total / 1024 / 1024:.1f} MB"
            )
        else:
            self.hint_label.setText(f"正在下载更新包… {done / 1024 / 1024:.1f} MB")

    def _on_downloaded(self, path: str) -> None:
        self._zip_path = path
        self.progress.setValue(100)
        self.hint_label.setText("更新包已就绪，点击「立即更新并重启」开始安装。")
        self.later_btn.setText("稍后再说")
        self.action_btn.setText("立即更新并重启")
        self.action_btn.setEnabled(True)

    def _on_download_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.later_btn.setText("稍后再说")
        self.action_btn.setEnabled(True)
        if self.isVisible():
            self.hint_label.setText(f"下载失败：{message}")
            QMessageBox.warning(self, "下载失败", f"{message}\n\n可点「在浏览器中打开」手动下载。")

    def _apply_update(self) -> None:
        install_dir = os.path.dirname(os.path.abspath(sys.executable)) if is_frozen() else ""
        if not install_dir or not self._zip_path:
            self.hint_label.setText("无法确定安装目录，请到发布页手动下载。")
            return
        try:
            launch_updater(install_dir, self._zip_path)
        except UpdateError as exc:
            QMessageBox.warning(self, "无法启动更新程序", str(exc))
            return
        logger.info("已启动更新程序，准备退出主程序")
        self.accept()
        _quit_application()

    # ---- 关闭时收尾 ----

    def reject(self) -> None:
        worker = self._download_worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait(3000)
        super().reject()


# ---- 对外入口 ----


def _alive(widget) -> bool:
    """包装器对应的 C++ 对象是否仍有效。

    检查更新的线程可能在对话框关闭之后才返回结果，此时不能再拿已销毁的
    Qt 对象当父窗口（PySide 对已销毁对象不保证抛异常）。
    """
    if widget is None:
        return True
    try:
        import shiboken6

        return shiboken6.isValid(widget)
    except Exception:
        return True


def _fallback_parent():
    """找不到原父窗口时，退回到当前可见的主窗口。"""
    app = QApplication.instance()
    if app is None:
        return None
    from PySide6.QtWidgets import QMainWindow

    for widget in app.topLevelWidgets():
        if isinstance(widget, QMainWindow) and widget.isVisible():
            return widget
    return None


def check_updates(parent=None, silent: bool = False, on_available=None) -> None:
    """检查更新（后台线程）。

    Args:
        silent: True 用于启动静默检查——失败不提示，发现新版本也不弹窗，
            只调用 `on_available(release)` 由调用方决定展示方式。
        on_available: silent 模式下发现新版本时的回调。
    """
    worker = UpdateCheckWorker(parent=None)
    if parent is not None:
        # 挂在窗口上只是为了拿到生命周期；线程本身注册进全局表防止被回收
        parent._update_check_worker = worker

    def _on_done(release):
        if not is_newer(release.version, __version__):
            if not silent and _alive(parent):
                QMessageBox.information(
                    parent, "检查更新", f"当前已是最新版本 v{__version__}。"
                )
            return
        if silent:
            if on_available is not None:
                on_available(release)
            return
        owner = parent if _alive(parent) else _fallback_parent()
        UpdateDialog(release, owner).exec()

    def _on_failed(message: str):
        logger.info("检查更新失败: %s", message)
        if silent or not _alive(parent):
            return
        box = QMessageBox(parent)
        box.setWindowTitle("检查更新失败")
        box.setIcon(QMessageBox.Warning)
        box.setText(message)
        box.setInformativeText("可在浏览器中打开发布页手动下载完整包。")
        open_btn = box.addButton("打开发布页", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(RELEASE_PAGE))

    worker.done.connect(_on_done)
    worker.failed.connect(_on_failed)
    worker.finished.connect(lambda w=worker: _release(parent, w))
    worker.finished.connect(worker.deleteLater)
    register(worker)
    worker.start()


def _release(parent, worker) -> None:
    """线程结束后清掉窗口上的引用（仍是本线程时才清）。"""
    if parent is not None and getattr(parent, "_update_check_worker", None) is worker:
        parent._update_check_worker = None
