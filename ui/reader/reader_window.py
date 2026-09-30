"""阅读器窗口：单页自适应浏览本地/在线漫画。

设计要点：
- **独立非模态窗口**，可同时打开多部作品对比翻阅；
- 顶部工具条 + 中央图片区 + 底部状态条三层结构；
- 图片**优先读本地缓存**（ReaderSession 本地优先），未命中才联网补拉；
- **纯按需加载**：只在用户点「下一页/上一页」时获取该页，不主动预取
  （如需更顺滑的翻页，可在设置里显式开启"后台预取相邻页"）；
- 关闭时中断所有后台线程（业务信号全名 `done`，清理连接原生 `finished`）。

线程约定（与项目既有实践一致）：
- PageLoadWorker（前台加载）/ PrefetchWorker（后台预取）的 deleteLater
  一律连接**原生 finished**，绝不连接业务信号，避免
  "QThread: Destroyed while thread is still running" 崩溃。
"""
import logging
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui.reader.page_provider import ReaderSession
from ui.reader.prefetch_worker import PageLoadWorker, PrefetchWorker
from ui.styles import PALETTE

logger = logging.getLogger(__name__)


class _ImageArea(QLabel):
    """图片显示区：等比缩放适配，并支持左右半屏点击翻页。"""

    clicked_left = Signal()
    clicked_right = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumSize(1, 1)
        self._source: QPixmap | None = None

    def set_source(self, pixmap: QPixmap | None):
        """设置原图；窗口尺寸变化时按当前尺寸重新缩放。"""
        self._source = pixmap
        self._rescale()

    def _rescale(self):
        if self._source is None or self._source.isNull():
            self.clear()
            return
        scaled = self._source.scaled(
            self.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def mouseReleaseEvent(self, event):
        """点击左半屏上一页、右半屏下一页（与主流阅读器一致）。"""
        if event.button() == Qt.LeftButton and self._source is not None:
            if event.position().x() < self.width() / 2:
                self.clicked_left.emit()
            else:
                self.clicked_right.emit()
        super().mouseReleaseEvent(event)


class ReaderWindow(QWidget):
    """漫画阅读器窗口。"""

    # 缓存进度变化（缓存张数, 期望总数），供卡片进度圈等外部订阅
    cache_progress_changed = Signal(int, int)

    def __init__(self, comic: dict, parent=None, session: ReaderSession | None = None):
        super().__init__(parent)
        self.comic = dict(comic or {})
        self.session = session or ReaderSession(self.comic)
        self._page = 1
        self._total = 0
        self._load_worker: PageLoadWorker | None = None
        self._prefetch_worker: PrefetchWorker | None = None
        self._loaded_page: int | None = None
        # 加载请求序号：每次发起加载自增，回调据此丢弃过期结果
        self._load_seq = 0

        title = self.comic.get("translated_title") or self.comic.get("title") or "阅读"
        self.setWindowTitle(f"阅读 - {title}")
        self.setMinimumSize(720, 540)
        self.resize(1080, 800)
        self.setStyleSheet(_READER_QSS)

        self._build_ui()
        self._bind_shortcuts()
        self._bootstrap()

    # ---- UI 构建 ----

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部工具条：仅标题与窗口操作，翻页统一放到底部
        top = QFrame()
        top.setObjectName("readerTop")
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(16, 10, 16, 10)
        top_layout.setSpacing(12)

        self.title_label = QLabel(self.windowTitle().removeprefix("阅读 - "))
        self.title_label.setObjectName("readerTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        top_layout.addWidget(self.title_label, stretch=1)

        self.fit_btn = QPushButton("全屏")
        self.fit_btn.setObjectName("readerToolButton")
        self.fit_btn.setCursor(Qt.PointingHandCursor)
        self.fit_btn.setFocusPolicy(Qt.NoFocus)
        self.fit_btn.setToolTip("全屏切换（F）")
        self.fit_btn.clicked.connect(self.toggle_fullscreen)
        top_layout.addWidget(self.fit_btn)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("readerCloseButton")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.setToolTip("关闭（Esc）")
        close_btn.clicked.connect(self.close)
        top_layout.addWidget(close_btn)

        root.addWidget(top)

        # 中央图片区
        self.image_area = _ImageArea()
        self.image_area.clicked_left.connect(self.go_prev)
        self.image_area.clicked_right.connect(self.go_next)
        root.addWidget(self.image_area, stretch=1)

        # 底部工具条：上一页 | 状态/缓存/页码 | 下一页
        bottom = QFrame()
        bottom.setObjectName("readerBottom")
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(16, 10, 16, 10)
        bottom_layout.setSpacing(12)

        self.prev_btn = QPushButton("‹ 上一页")
        self.prev_btn.setObjectName("readerNavButton")
        self.prev_btn.setCursor(Qt.PointingHandCursor)
        self.prev_btn.setFocusPolicy(Qt.NoFocus)
        self.prev_btn.setMinimumWidth(96)
        self.prev_btn.clicked.connect(self.go_prev)
        bottom_layout.addWidget(self.prev_btn)

        self.status_label = QLabel("准备中…")
        self.status_label.setObjectName("readerStatus")
        bottom_layout.addStretch()

        self.cache_label = QLabel("")
        self.cache_label.setObjectName("readerCache")
        bottom_layout.addWidget(self.cache_label)

        self.page_label = QLabel("1 / ?")
        self.page_label.setObjectName("readerPage")
        self.page_label.setAlignment(Qt.AlignCenter)
        bottom_layout.addWidget(self.page_label)

        bottom_layout.addWidget(self.status_label)
        bottom_layout.addStretch()

        self.next_btn = QPushButton("下一页 ›")
        self.next_btn.setObjectName("readerNavButton")
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.setFocusPolicy(Qt.NoFocus)
        self.next_btn.setMinimumWidth(96)
        self.next_btn.clicked.connect(self.go_next)
        bottom_layout.addWidget(self.next_btn)

        root.addWidget(bottom)

    def _bind_shortcuts(self):
        """绑定键盘快捷键（对齐常见阅读器）。"""
        binds = {
            "Right": self.go_next,
            "Down": self.go_next,
            "Space": self.go_next,
            "PgDown": self.go_next,
            "Left": self.go_prev,
            "Up": self.go_prev,
            "PgUp": self.go_prev,
            "Home": self.go_first,
            "End": self.go_last,
            "Esc": self.close,
            "F": self.toggle_fullscreen,
        }
        self._shortcuts = []
        for key, handler in binds.items():
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)

    # ---- 启动流程 ----

    def _bootstrap(self):
        """打开阅读器时初始化会话并加载首页（后台线程，避免卡界面）。"""
        self.status_label.setText("正在准备…")
        self.session.ensure_ready()
        self._total = max(1, self.session.resolve_total_pages())
        # 断点续读：上次读到哪页
        last = self.session.index.load_last_page(self.session.aid)
        self._page = min(max(1, last), self._total)
        self._update_page_ui()
        self._update_cache_label()
        self.load_page(self._page)

    # ---- 翻页 ----

    def go_prev(self):
        if self._page > 1:
            self.load_page(self._page - 1)

    def go_next(self):
        if self._page < self._total:
            self.load_page(self._page + 1)

    def go_first(self):
        self.load_page(1)

    def go_last(self):
        self.load_page(self._total)

    def load_page(self, page_no: int):
        """加载指定页：本地命中的同步路径由工作线程统一处理。"""
        page_no = max(1, min(int(page_no), self._total))
        self._page = page_no
        self._update_page_ui()
        # 已有加载线程在跑：请求中断，其原生 finished 会清空引用；
        # 新线程照常启动（旧线程在下一个检查点自行退出）。
        # 注意：中断只是「尽力」，旧线程仍可能跑完并 emit 结果，
        # 因此这里还必须递增请求序号，由回调按序号丢弃过期结果，
        # 否则旧页画面会覆盖到新页上（「点了下一页还显示上一页」）。
        self._interrupt_worker("_load_worker")
        self._load_seq += 1
        self._start_load(page_no, self._load_seq)

    def _interrupt_worker(self, attr: str):
        """安全请求中断指定 worker，并清空已失效的引用。

        worker 的清理回调连接了原生 finished（先 clear 引用、再 deleteLater），
        因此此处引用要么有效（请求中断），要么已被置为 None；直接用
        `isRunning()` 检查即可，不需要额外判活。
        """
        worker = getattr(self, attr, None)
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
        except RuntimeError:
            # C++ 对象已销毁（清理竞态）：直接丢弃引用
            setattr(self, attr, None)

    def _start_load(self, page_no: int, seq: int):
        self.status_label.setText(f"加载第 {page_no} 页…")
        worker = PageLoadWorker(self.session, page_no, seq=seq, parent=None)
        self._load_worker = worker
        worker.loaded.connect(self._on_page_loaded)
        worker.failed.connect(self._on_page_failed)
        # 清理：先清引用（避免界面后续访问已销毁的 C++ 对象），
        # 再 deleteLater。统一挂**原生 finished**（线程完全停止后才触发），
        # 绝不挂业务信号——它们在 run() 返回前 emit，会触发
        # "QThread: Destroyed while thread is still running" 崩溃。
        worker.finished.connect(lambda w=worker: self._clear_worker("_load_worker", w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _clear_worker(self, attr: str, worker):
        """线程结束后清空引用（仅当仍是当前 worker 时，避免误清新线程）。"""
        if getattr(self, attr, None) is worker:
            setattr(self, attr, None)

    def _is_stale(self, seq: int, page_no: int) -> bool:
        """该结果是否已过期（用户已翻到别的页 / 又发起了更新请求）。"""
        return seq != self._load_seq or page_no != self._page

    def _on_page_loaded(self, seq: int, page_no: int, path):
        # 过期结果直接丢弃：旧线程晚于新线程返回时，若不拦截会把旧页
        # 画面覆盖到当前页上。
        if self._is_stale(seq, page_no):
            logger.debug("丢弃过期加载结果: seq=%s page=%s", seq, page_no)
            return
        self._loaded_page = page_no
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._on_page_failed(seq, page_no, "图片无法解码")
            return
        self.image_area.set_source(pixmap)
        self.status_label.setText(f"第 {page_no} 页")
        self._update_cache_label()
        self._save_progress()
        # 默认不做任何预取：只有用户真正翻页时才去取那一页。
        # 若用户在设置里显式开启了预取，才在页显示完成后补拉相邻窗口。
        self._maybe_prefetch(page_no)

    def _on_page_failed(self, seq: int, page_no: int, message: str):
        # 过期请求（用户已翻页）的错误不覆盖当前页提示
        if self._is_stale(seq, page_no):
            return
        self.status_label.setText(f"第 {page_no} 页加载失败：{message}")
        self.image_area.set_source(None)
        self.image_area.setText("加载失败\n\n点击重试")

    def retry_current_page(self):
        self.load_page(self._page)

    # ---- 预取（默认关闭；仅当用户在设置中显式开启时才生效） ----

    def _maybe_prefetch(self, page_no: int):
        """按设置决定是否预取相邻页。

        默认 `reader_prefetch_enabled=False`，此方法直接返回——阅读器
        始终只取用户翻到的那一页，符合「点下一页时再获取」的预期。
        """
        from ui.settings import AppSettings

        settings = AppSettings()
        if not settings.reader_prefetch_enabled:
            return
        if settings.reader_prefetch_ahead <= 0 and settings.reader_prefetch_behind <= 0:
            return
        self._start_prefetch(page_no)

    def _start_prefetch(self, page_no: int):
        """为当前页启动后台预取；已有预取线程先取消（窗口已过期）。"""
        from ui.settings import AppSettings

        settings = AppSettings()
        # 旧预取线程请求中断后自行退出，其原生 finished 会清空引用
        self._interrupt_worker("_prefetch_worker")
        worker = PrefetchWorker(
            self.session,
            page_no,
            ahead=settings.reader_prefetch_ahead,
            behind=settings.reader_prefetch_behind,
            parent=None,
        )
        self._prefetch_worker = worker
        worker.page_ready.connect(self._on_prefetch_page_ready)
        worker.progress.connect(self._on_prefetch_progress)
        # 先清引用再 deleteLater（同 _start_load 的清理约定）
        worker.finished.connect(
            lambda w=worker: self._clear_worker("_prefetch_worker", w)
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_prefetch_page_ready(self, page_no: int):
        """预取补拉了当前页（首次加载失败后的补偿）时，刷新当前页显示。"""
        if page_no == self._page and (
            self.image_area.pixmap() is None or self.image_area.pixmap().isNull()
        ):
            self.load_page(self._page)

    def _on_prefetch_progress(self, cached: int, total: int):
        self._update_cache_label(cached, total)
        self.cache_progress_changed.emit(cached, total)

    # ---- 界面状态 ----

    def _update_page_ui(self):
        self.page_label.setText(f"{self._page} / {self._total}")
        self.prev_btn.setEnabled(self._page > 1)
        self.next_btn.setEnabled(self._page < self._total)

    def _update_cache_label(self, cached: int | None = None, total: int | None = None):
        if cached is None:
            cached = self.session.local_count
        if total is None:
            total = self._total
        if total > 0:
            percent = min(100, int(cached * 100 / total))
            self.cache_label.setText(f"缓存 {cached}/{total}（{percent}%）")
        else:
            self.cache_label.setText(f"缓存 {cached} 张")

    def _save_progress(self):
        try:
            self.session.index.save_last_page(self.session.aid, self._page, self._total)
        except Exception as exc:  # 进度写入失败不影响阅读
            logger.warning("保存阅读进度失败: %s", exc)

    # ---- 全屏 ----

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.fit_btn.setText("全屏")
        else:
            self.showFullScreen()
            self.fit_btn.setText("退出全屏")

    # ---- 关闭收尾 ----

    def closeEvent(self, event):
        """关闭窗口：保存进度并**等待**后台线程收尾，再放行关闭。

        必须同步等待（而非仅 requestInterruption）：QThread 对象若在线程仍
        运行时被销毁，会触发 "QThread: Destroyed while thread is still
        running" 直接 abort。无事件循环的场景（如离屏测试、程序退出）尤其
        明显——deleteLater 不会被执行，只能在此限时 join。
        """
        self._save_progress()
        self._shutdown_workers()
        super().closeEvent(event)

    def _shutdown_workers(self, timeout_ms: int = 2500):
        """请求中断并限时等待所有后台线程结束（参考 DownloadManager.shutdown）。

        单页加载线程的粒度是一张图（可能正在网络 IO），因此给予有限等待；
        超时后强制 terminate，保证关闭不卡死、也不留下运行中的 QThread。
        """
        workers = [
            w
            for w in (self._load_worker, self._prefetch_worker)
            if w is not None
        ]
        if not workers:
            return
        for worker in workers:
            try:
                worker.requestInterruption()
            except RuntimeError:
                continue  # C++ 对象已销毁
        deadline = time.monotonic() + timeout_ms / 1000
        for worker in workers:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            try:
                worker.wait(remaining_ms)
            except RuntimeError:
                continue
        for worker in workers:
            try:
                if worker.isRunning():
                    worker.terminate()
                    worker.wait(500)
            except RuntimeError:
                continue


# 阅读器专用浅色样式（深色图片背景更利于观图）
_READER_QSS = f"""
QWidget {{
    background-color: #1B1D21;
    font-family: "Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif;
}}
QFrame#readerTop {{
    background-color: #23262B;
    border-bottom: 1px solid #34383F;
}}
QFrame#readerBottom {{
    background-color: #23262B;
    border-top: 1px solid #34383F;
}}
QLabel#readerTitle {{
    color: #F3F4F6;
    font-size: 15px;
    font-weight: 600;
}}
QLabel#readerStatus {{
    color: #9CA3AF;
    font-size: 13px;
}}
QLabel#readerCache {{
    color: #F59E0B;
    font-size: 13px;
}}
QLabel#readerPage {{
    color: #F3F4F6;
    font-size: 14px;
    font-weight: 600;
    min-width: 96px;
}}
QPushButton#readerNavButton {{
    background-color: {PALETTE["primary"]};
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 16px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#readerNavButton:hover {{
    background-color: {PALETTE["primary_hover"]};
}}
QPushButton#readerNavButton:disabled {{
    background-color: #3A3F47;
    color: #6B7280;
}}
QPushButton#readerToolButton {{
    background-color: #34383F;
    color: #E5E7EB;
    border: none;
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 13px;
}}
QPushButton#readerToolButton:hover {{
    background-color: #40454E;
}}
QPushButton#readerCloseButton {{
    background-color: transparent;
    color: #E5E7EB;
    border: none;
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 15px;
}}
QPushButton#readerCloseButton:hover {{
    background-color: #B91C1C;
    color: white;
}}
QLabel {{
    color: #D1D5DB;
}}
"""
