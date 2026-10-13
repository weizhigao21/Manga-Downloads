"""阅读器窗口：单页自适应浏览本地/在线漫画。

设计要点：
- **独立非模态窗口**，可同时打开多部作品对比翻阅；
- 中央图片区；翻页与播放控制条是**独立停靠小窗**（ReaderControlBar）——
  固定停靠屏幕底部中央，图片窗口可以随图片大小缩放/移动，按钮位置不变；
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
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDrag, QGuiApplication, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui.reader.control_bar import (
    ReaderControlBar,
    raise_bar,
    register_bar,
    unregister_bar,
)
from ui.reader.page_provider import ReaderSession
from ui.reader.prefetch_worker import PageLoadWorker, PrefetchWorker
from ui.styles import PALETTE
from ui.threads import register

logger = logging.getLogger(__name__)


def build_image_drag_mime(path, image) -> QMimeData:
    """构造拖出图片用的拖拽数据：本地文件 URL + 原始图像数据。

    两者都给是为了兼容不同接收方：
    - 只给 URL：只认图像数据的程序（部分聊天窗口、画图类）收不到；
    - 只给图像数据：文件管理器/图库拿不到原始文件名，也可能被重新编码。
    """
    mime = QMimeData()
    if path:
        mime.setUrls([QUrl.fromLocalFile(str(Path(path).resolve()))])
    if image is not None and not image.isNull():
        mime.setImageData(image)
    return mime


class _ImageArea(QLabel):
    """图片显示区：等比缩放适配、左右半屏点击翻页，长按后可把图片拖出到其他程序。

    拖拽做成「长按后再拖」而不是直接拖动：图片区同时承担点击翻页，若一按下
    就能拖，轻微手抖会被判成拖拽、点击翻页会失灵。按住不动超过 LONG_PRESS_MS
    进入可拖拽态（光标变手型、底部提示），此后再移动才真正发起拖拽。
    """

    clicked_left = Signal()
    clicked_right = Signal()
    # 进入/退出「可拖拽」态：由窗口在底部状态栏给出文字提示
    drag_armed = Signal(bool)

    # 长按判定阈值：按住不动超过该毫秒数才允许拖出
    LONG_PRESS_MS = 350

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumSize(1, 1)
        self.setToolTip("点击左右半屏翻页；长按后拖动可把图片拖到其他程序")
        self._source: QPixmap | None = None
        self._source_path: str = ""
        self._press_pos = None
        self._drag_ready = False
        self._dragging = False
        # 长按定时器：按下后不动，到点进入可拖拽态
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(self.LONG_PRESS_MS)
        self._long_press_timer.timeout.connect(self._on_long_press)

    def set_source(self, pixmap: QPixmap | None, path=None):
        """设置原图与对应的本地文件路径；窗口尺寸变化时按当前尺寸重新缩放。

        path 是拖拽要用的：只有给出文件 URL，对方程序才能拿到原始画质与
        原始文件名，而不是本控件缩放后的位图。
        """
        self._source = pixmap
        self._source_path = (
            str(path) if path and pixmap is not None and not pixmap.isNull() else ""
        )
        self._stop_long_press()
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

    # ---- 长按拖出到其他程序 ----

    def _stop_long_press(self):
        """复位长按/拖拽手势状态（翻页、切图、鼠标松开时都要调）。"""
        self._long_press_timer.stop()
        armed = self._drag_ready
        self._drag_ready = False
        self._dragging = False
        self._press_pos = None
        self.unsetCursor()
        if armed:
            self.drag_armed.emit(False)

    def _on_long_press(self):
        """按住不动到点：进入可拖拽态，给出光标与文字反馈。"""
        if self._press_pos is None or self._source is None:
            return
        self._drag_ready = True
        self.setCursor(Qt.OpenHandCursor)
        self.drag_armed.emit(True)

    def drag_mime(self) -> QMimeData | None:
        """当前图片的拖拽数据；没有图片时返回 None。"""
        if self._source is None or self._source.isNull():
            return None
        return build_image_drag_mime(self._source_path, self._source.toImage())

    def _start_drag(self):
        """发起拖拽：进入 Qt 的 DnD 事件循环，直到对方接收或取消。"""
        mime = self.drag_mime()
        if mime is None:
            self._stop_long_press()
            return
        self._dragging = True
        self._long_press_timer.stop()
        self.setCursor(Qt.ClosedHandCursor)
        drag = QDrag(self)
        drag.setMimeData(mime)
        # 跟随光标的缩略图用**当前显示尺寸**，避免拖一张巨图当预览
        preview = self.pixmap()
        if preview is not None and not preview.isNull():
            thumb = preview.scaled(
                160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            drag.setPixmap(thumb)
            drag.setHotSpot(thumb.rect().center())
        drag.exec(Qt.CopyAction)
        # exec 返回即本次拖拽结束；_dragging 留到鼠标松开再清，
        # 否则松手时会被当成一次点击而翻页
        self._drag_ready = False
        self._press_pos = None
        self.unsetCursor()
        self.drag_armed.emit(False)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._source is not None:
            self._press_pos = event.position().toPoint()
            self._drag_ready = False
            self._dragging = False
            self._long_press_timer.start()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """长按就绪后再移动超过拖拽阈值，才真正发起拖出。"""
        if (
            self._drag_ready
            and not self._dragging
            and self._press_pos is not None
            and event.buttons() & Qt.LeftButton
            and (event.position().toPoint() - self._press_pos).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            self._start_drag()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """点击左半屏上一页、右半屏下一页（与主流阅读器一致）。

        拖出图片的那次手势不再翻页；只长按没有拖走仍按普通点击处理。
        """
        dragged = self._dragging
        self._stop_long_press()
        if (
            not dragged
            and event.button() == Qt.LeftButton
            and self._source is not None
        ):
            if event.position().x() < self.width() / 2:
                self.clicked_left.emit()
            else:
                self.clicked_right.emit()
        super().mouseReleaseEvent(event)


class ReaderWindow(QWidget):
    """漫画阅读器窗口。"""

    # 缓存进度变化（缓存张数, 期望总数），供卡片进度圈等外部订阅
    cache_progress_changed = Signal(int, int)
    reader_closed = Signal()
    back_requested = Signal()

    def __init__(self, comic: dict, parent=None, session: ReaderSession | None = None):
        super().__init__(parent)
        self.comic = dict(comic or {})
        self.session = session or ReaderSession(self.comic)
        self._page = 1
        self._total = 0
        self._load_worker: PageLoadWorker | None = None
        self._prefetch_worker: PrefetchWorker | None = None
        # 快速翻页时旧加载线程可能仍在收尾；保留它们直到 finished，
        # 防止引用被新线程覆盖后 QThread 在运行中析构。
        self._active_workers: list = []
        self._loaded_page: int | None = None
        self._loaded_seq = -1
        # 加载请求序号：每次发起加载自增，回调据此丢弃过期结果
        self._load_seq = 0
        self._slideshow_seconds = 5
        self._slideshow_playing = False
        self._slideshow_timer = QTimer(self)
        self._slideshow_timer.setSingleShot(True)
        self._slideshow_timer.timeout.connect(self._advance_slideshow)
        self._initial_top_placed = False
        # 控制条是否已登记/钉屏（showEvent 里一次性完成）
        self._bar_registered = False

        title = self.comic.get("translated_title") or self.comic.get("title") or "阅读"
        self.setWindowTitle(f"阅读 - {title}")
        self.setMinimumSize(330, 220)
        self.resize(640, 720)
        self.setStyleSheet(_READER_QSS)

        self._build_ui()
        self._bind_shortcuts()
        self._bootstrap()

    # ---- UI 构建 ----

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 中央图片区
        self.image_area = _ImageArea()
        self.image_area.clicked_left.connect(self.go_prev)
        self.image_area.clicked_right.connect(self.go_next)
        self.image_area.drag_armed.connect(self._on_drag_armed)
        root.addWidget(self.image_area, stretch=1)

        # 控制条：**独立停靠小窗**，固定屏幕底部中央。图片窗口照旧随图片
        # 宽高自动缩放/移动，按钮的屏幕位置全程不变（用户明确要求：
        # 「图片窗口可以动，但是按钮不能动」）。控件归控制条所有，窗口侧
        # 通过属性转发继续以 self.next_btn 等方式访问。
        self.bar = ReaderControlBar(self)
        self.back_btn = self.bar.back_btn
        self.prev_btn = self.bar.prev_btn
        self.next_btn = self.bar.next_btn
        self.play_btn = self.bar.play_btn
        self.status_label = self.bar.status_label
        self.page_label = self.bar.page_label
        self.cache_label = self.bar.cache_label
        self.back_btn.clicked.connect(self._on_back_requested)
        self.prev_btn.clicked.connect(self.go_prev)
        self.next_btn.clicked.connect(self.go_next)
        self.play_btn.clicked.connect(self.toggle_slideshow)
        # 控制条虽设了 WindowDoesNotAcceptFocus，部分平台仍可能抢到激活；
        # 点击后主动把激活还给图片窗口，保证方向键/空格翻页不失效
        for button in (self.back_btn, self.prev_btn, self.next_btn, self.play_btn):
            button.clicked.connect(self._reclaim_window_activation)

    def _reclaim_window_activation(self):
        """点击控制条按钮后，确保图片窗口仍是活动窗口（快捷键继续可用）。"""
        if not self.isActiveWindow():
            self.activateWindow()

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

    def _on_back_requested(self):
        self.back_requested.emit()
        self.close()

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
        self._slideshow_timer.stop()
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
        self._active_workers.append(worker)
        worker.loaded.connect(self._on_page_loaded)
        worker.failed.connect(self._on_page_failed)
        # 清理：先清引用（避免界面后续访问已销毁的 C++ 对象），
        # 再 deleteLater。统一挂**原生 finished**（线程完全停止后才触发），
        # 绝不挂业务信号——它们在 run() 返回前 emit，会触发
        # "QThread: Destroyed while thread is still running" 崩溃。
        worker.finished.connect(lambda w=worker: self._clear_worker("_load_worker", w))
        worker.finished.connect(worker.deleteLater)
        register(worker)
        worker.start()

    def _clear_worker(self, attr: str, worker):
        """线程结束后清空引用（仅当仍是当前 worker 时，避免误清新线程）。"""
        if getattr(self, attr, None) is worker:
            setattr(self, attr, None)
        if worker in self._active_workers:
            self._active_workers.remove(worker)

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
        self._loaded_seq = seq
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._on_page_failed(seq, page_no, "图片无法解码")
            return
        # 图片与窗口尺寸一起提交，避免先在旧尺寸绘制新图、随后又缩放和
        # 移动顶层窗口。快速翻页时这些中间帧会表现为短暂的白色小窗闪动。
        self.setUpdatesEnabled(False)
        try:
            self._fit_window_to_image(pixmap)
            self.image_area.set_source(pixmap, path)
            self.status_label.setText(f"第 {page_no} 页")
        finally:
            self.setUpdatesEnabled(True)
        self.update()
        self._update_cache_label()
        self.cache_progress_changed.emit(self.session.local_count, self._total)
        self._save_progress()
        # 默认不做任何预取：只有用户真正翻页时才去取那一页。
        # 若用户在设置里显式开启了预取，才在页显示完成后补拉相邻窗口。
        self._maybe_prefetch(page_no)
        if self._slideshow_playing:
            if page_no >= self._total:
                self._stop_slideshow()
            else:
                self._slideshow_timer.start(self._slideshow_seconds * 1000)

    def _fit_window_to_image(self, pixmap: QPixmap):
        """按图片比例收紧普通窗口，保留底部控制条并限制在屏幕内。"""
        if pixmap.isNull() or self.isFullScreen() or self.isMaximized():
            return
        # 未显示的窗口尚未绑定稳定的屏幕；此时直接用主屏幕，避免
        # 离屏/快速打开关闭场景访问无效的 Qt window handle。
        screen = (self.screen() if self.isVisible() else None) or QGuiApplication.primaryScreen()
        if screen is None:
            return

        available = screen.availableGeometry()
        frame_width = max(0, self.frameGeometry().width() - self.width())
        frame_height = max(32, self.frameGeometry().height() - self.height())
        max_width = max(1, available.width() - frame_width - 24)
        max_height = max(1, available.height() - frame_height - 24)
        layout = self.layout()
        # 底部控制条是**独立停靠小窗**（不在本窗口布局内，见 ReaderControlBar）：
        # 预留它占用的屏幕底边空间，避免高图窗口滑到控制条底下被盖住。
        bar_reserve = self.bar.sizeHint().height() + 24
        padding = 16
        image_width = pixmap.width() / pixmap.devicePixelRatioF()
        image_height = pixmap.height() / pixmap.devicePixelRatioF()
        scale = min(
            1.0,
            max(1, max_width - padding) / image_width,
            max(1, max_height - bar_reserve) / image_height,
        )
        min_width = max(self.minimumWidth(), layout.minimumSize().width())
        target_width = min(max_width, max(min_width, round(image_width * scale) + padding))
        target_height = min(
            max_height,
            max(self.minimumHeight(), round(image_height * scale) + padding),
        )
        if not self.isVisible():
            if self.size().width() != target_width or self.size().height() != target_height:
                self.resize(target_width, target_height)
            return

        # setGeometry 一次完成尺寸和位置变化，避免 resize + move 让 Windows
        # 先显示居中的临时窗口，再把它移到屏幕顶部。
        frame = self.frameGeometry()
        client = self.geometry()
        left_margin = client.left() - frame.left()
        top_margin = client.top() - frame.top()
        outer_width = target_width + frame.width() - client.width()
        frame_left = available.left() + max(0, (available.width() - outer_width) // 2)
        target = (frame_left + left_margin, available.top() + top_margin,
                  target_width, target_height)
        if (client.x(), client.y(), client.width(), client.height()) != target:
            self.setGeometry(*target)

    def moveEvent(self, event):
        """换屏时把控制条跟到新屏幕底部（同屏内移动不动控制条）。

        控制条固定停靠屏幕底部中央是硬约束（用户要求「按钮不能动」），
        但读者把窗口拖到另一块显示器时，控制条必须跟着过去，否则会留在
        旧屏幕上与窗口分离。
        """
        super().moveEvent(event)
        screen = self.screen()
        if screen is not None and screen is not self.bar.pinned_screen:
            self.bar.pin_to(screen)

    def changeEvent(self, event):
        """本窗口被激活时，把它的控制条提为最前（隐藏其他阅读器的控制条）。"""
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            raise_bar(self.bar)

    def _on_drag_armed(self, armed: bool):
        """长按进入/退出可拖拽态时，在底部状态栏给出文字提示。"""
        if armed:
            self.status_label.setText("可拖拽：按住图片拖到其他程序")
        elif self._loaded_page:
            self.status_label.setText(f"第 {self._loaded_page} 页")

    def _on_page_failed(self, seq: int, page_no: int, message: str):
        # 过期请求（用户已翻页）的错误不覆盖当前页提示
        if self._is_stale(seq, page_no):
            return
        self.status_label.setText(f"第 {page_no} 页加载失败：{message}")
        self.image_area.set_source(None)
        self.image_area.setText("加载失败\n\n点击重试")
        self._stop_slideshow()

    def retry_current_page(self):
        self.load_page(self._page)

    # ---- 图片播放 ----

    def toggle_slideshow(self):
        """设置每张图片停留时间，或停止正在进行的自动翻页。"""
        if self._slideshow_playing:
            self._stop_slideshow()
            return
        seconds, accepted = QInputDialog.getInt(
            self, "图片播放", "每张图片显示几秒后自动翻页：",
            self._slideshow_seconds, 1, 3600, 1,
        )
        if not accepted:
            return
        self._slideshow_seconds = seconds
        self._slideshow_playing = True
        self.play_btn.setText("■ 停止播放")
        self._update_page_ui()
        if self._loaded_seq == self._load_seq:
            self._slideshow_timer.start(seconds * 1000)

    def _stop_slideshow(self):
        self._slideshow_timer.stop()
        self._slideshow_playing = False
        self.play_btn.setText("▶ 图片播放")
        self._update_page_ui()

    def _advance_slideshow(self):
        """只在当前图片完整显示后翻页；下一次计时由加载成功回调启动。"""
        if not self._slideshow_playing or self._loaded_seq != self._load_seq:
            return
        if self._page >= self._total:
            self._stop_slideshow()
            return
        self.go_next()

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
        self._active_workers.append(worker)
        worker.page_ready.connect(self._on_prefetch_page_ready)
        worker.progress.connect(self._on_prefetch_progress)
        # 先清引用再 deleteLater（同 _start_load 的清理约定）
        worker.finished.connect(
            lambda w=worker: self._clear_worker("_prefetch_worker", w)
        )
        worker.finished.connect(worker.deleteLater)
        register(worker)
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
        self.play_btn.setEnabled(self._slideshow_playing or self._page < self._total)

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

    def _center_at_screen_top(self, screen):
        """按窗口外框居中，并将外框顶部对齐屏幕可用区域。"""
        available = screen.availableGeometry()
        frame_width = self.frameGeometry().width()
        left = available.left() + max(0, (available.width() - frame_width) // 2)
        self.move(left, available.top())

    def showEvent(self, event):
        """首次显示时将窗口置于屏幕顶部中央，并登记/钉住控制条。"""
        super().showEvent(event)
        self._ensure_bar_shown()
        if self._initial_top_placed or self.isFullScreen():
            return
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        self._center_at_screen_top(screen)
        self._initial_top_placed = True

    def _ensure_bar_shown(self):
        """（可重复调用）登记控制条、钉到当前屏幕底部中央、提为最前。

        显示动作推迟到事件循环的下一拍：在 showEvent 里直接 show 子 Tool
        窗属于 Qt reentrancy 坑（实测与后台加载线程的信号 emit 并发时
        offscreen 平台直接 access violation）。
        """
        if self._bar_registered:
            return
        self._bar_registered = True
        register_bar(self.bar)
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            self.bar.pin_to(screen)
        QTimer.singleShot(0, self._raise_bar_deferred)

    def _raise_bar_deferred(self):
        try:
            raise_bar(self.bar)
        except RuntimeError:
            pass  # 窗口已关闭：无需再置前

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            if self.image_area._source is not None:
                self._fit_window_to_image(self.image_area._source)
        else:
            self.showFullScreen()

    # ---- 关闭收尾 ----

    def closeEvent(self, event):
        """关闭窗口：保存进度并**等待**后台线程收尾，再放行关闭。

        必须同步等待（而非仅 requestInterruption）：QThread 对象若在线程仍
        运行时被销毁，会触发 "QThread: Destroyed while thread is still
        running" 直接 abort。无事件循环的场景（如离屏测试、程序退出）尤其
        明显——deleteLater 不会被执行，只能在此限时 join。
        """
        self._stop_slideshow()
        self._save_progress()
        self._shutdown_workers()
        # 注销控制条：关闭它，并把「最前」让给还活着的阅读器
        unregister_bar(self.bar)
        self._bar_registered = False
        self.reader_closed.emit()
        super().closeEvent(event)

    def _shutdown_workers(self, timeout_ms: int = 2500):
        """请求中断并限时等待所有后台线程结束（参考 DownloadManager.shutdown）。

        单页加载线程的粒度是一张图（可能正在网络 IO），因此给予有限等待；
        超时后强制 terminate，保证关闭不卡死、也不留下运行中的 QThread。
        """
        workers = list(self._active_workers)
        for worker in (self._load_worker, self._prefetch_worker):
            if worker is not None and worker not in workers:
                workers.append(worker)
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
/* 控制条底色由 ReaderControlBar.paintEvent 自绘（带半透明背景的顶层
   QWidget 不绘制 QSS background），这里只负责清掉标签自己的底色 */
#readerBottom QLabel {{
    background: transparent;
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
    min-width: 60px;
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
QPushButton#readerPlayButton {{
    background-color: #059669;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 13px;
}}
QPushButton#readerPlayButton:hover {{
    background-color: #047857;
}}
QPushButton#readerPlayButton:disabled {{
    background-color: #3A3F47;
    color: #6B7280;
}}
QInputDialog QSpinBox {{
    background-color: #F9FAFB;
    color: #111827;
    padding: 4px;
}}
QInputDialog QPushButton {{
    background-color: #34383F;
    color: #F3F4F6;
    padding: 5px 12px;
}}
QLabel {{
    color: #D1D5DB;
}}
"""
