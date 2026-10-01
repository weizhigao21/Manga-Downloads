from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QContextMenuEvent, QIcon, QPainter, QPixmap, QPixmapCache
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from ui.download_manager import DownloadManager, DownloadTask
from ui.models import ComicItem
from ui.styles import PALETTE
from ui.threads import register
from ui.widgets.cache_ring import CacheProgressRing


class ComicCard(QFrame):
    """漫画条目卡片组件。"""

    CARD_WIDTH = 440
    COVER_WIDTH = 160
    COVER_HEIGHT = 220

    # 右键菜单「查看类似作品」触发（emit 当前 ComicItem），由主窗口切换相似视图
    similar_requested = Signal(object)
    # 「阅读」按钮触发（emit 当前 ComicItem），由主窗口打开阅读器窗口
    read_requested = Signal(object)
    favorite_changed = Signal(object, bool)
    author_requested = Signal(str)

    def __init__(self, comic: ComicItem, parent=None, downloaded: bool | None = None):
        super().__init__(parent)
        self.comic = comic
        # 网格批量预计算的"已下载"结果（True/False 时直接使用，避免卡片
        # 各自遍历全表与磁盘 stat）；None 表示卡片按需自查（独立使用场景）
        self._pre_downloaded: bool | None = downloaded
        # 发布时预计算的缓存进度（percent, cached, total）；None 表示卡片自查
        self._pre_cache: tuple[int, int, int] | None = None
        # 本地已有封面时不创建网络管理器（避免每张卡片都初始化网络栈）
        self._network: QNetworkAccessManager | None = None
        self._manager = DownloadManager.instance()
        self._task_id = self._manager._extract_id(comic.detail_url)
        self._translate_worker = None  # 保持翻译线程引用，防止被回收
        self._setup_ui()
        self._load_cover()
        # 按任务定向响应：仅本作品的任务变化才同步按钮（避免每张卡片
        # 在每次广播时都遍历全部任务）；卡片销毁后 Qt 自动断开连接
        self._manager.task_changed.connect(self._on_task_changed)
        self._manager.task_deleted.connect(self._on_task_deleted)
        self._sync_download_button()

    def update_comic(self, comic: ComicItem, downloaded: bool | None = None):
        """网格 diff 复用时更新卡片数据（标题/封面/下载状态）。"""
        old = self.comic
        self.comic = comic
        self._task_id = self._manager._extract_id(comic.detail_url)
        if downloaded is not None:
            self._pre_downloaded = downloaded
        self._update_title_label()
        self._update_author_button()
        self._update_favorite_button()
        self.count_label.setText(self.comic.count_label_text)
        self.date_label.setText(f"上传: {self.comic.formatted_date}")
        self.url_label.setText(self.comic.detail_url)
        self._sync_download_button()
        self.downloaded_badge.setVisible(self._is_downloaded())
        self._position_cover_badges()
        self._update_similar_badge()
        # 复用卡片时缓存进度由网格重新预计算（set_cache_progress）覆盖
        self._pre_cache = None
        # 封面来源变化时重新加载（QPixmapCache 命中则直接复用，无需网络）
        if comic.cover_path != old.cover_path or comic.cover_url != old.cover_url:
            self._load_cover()

    def _on_task_changed(self, task):
        """下载任务变化回调：只响应本作品的任务（O(1) 过滤，免遍历任务列表）。"""
        if task.task_id and task.task_id == self._task_id:
            self._sync_download_button(task)
            if task.status == DownloadTask.DONE:
                self._pre_cache = None
                self.refresh_cache_progress()

    def _on_task_deleted(self, task):
        """任务被删除回调：本作品任务被移除后，按钮重新自查（回到未下载状态）。"""
        if task.task_id and task.task_id == self._task_id:
            self._sync_download_button()

    def set_similar_count(self, count: int, has_downloaded: bool = False):
        """更新本地库相似信息并刷新左下角角标（后台扫描完成后调用）。

        Args:
            count: 相似记录数（0 时隐藏角标）。
            has_downloaded: 相似记录中是否存在已下载作品（角标变红提示）。
        """
        if count == self.comic.similar_count and (
            has_downloaded == self.comic.similar_downloaded
        ):
            return
        self.comic.similar_count = count
        self.comic.similar_downloaded = has_downloaded
        self._update_similar_badge()

    def _update_similar_badge(self):
        """刷新相似角标文本/颜色/定位（仅 similar_count > 0 时显示）。

        颜色语义：橙色 = 有相似作品可查看；红色 = 相似记录中已有下载。
        """
        color = "#DC2626" if self.comic.similar_downloaded else "#F97316"
        self.similar_badge.setStyleSheet(
            f"""
            QLabel {{
                background-color: {color};
                color: white;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
                font-weight: 600;
            }}
            """
        )
        if self.comic.similar_count > 0:
            self.similar_badge.setText(f"≈{self.comic.similar_count}")
            self.similar_badge.adjustSize()
            self.similar_badge.move(
                8,
                self.COVER_HEIGHT - self.similar_badge.height() - 8,
            )
        self.similar_badge.setVisible(self.comic.similar_count > 0)

    def _setup_ui(self):
        self.setObjectName("comicCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.CARD_WIDTH, 262)

        # 卡片整体布局
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # 封面容器（用于承载封面图与右上角角标）
        self.cover_container = QFrame(self)
        self.cover_container.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)
        self.cover_container.setStyleSheet(
            "QFrame { background-color: transparent; border-radius: 8px; }"
        )

        self.cover_label = QLabel(self.cover_container)
        self.cover_label.setFixedSize(self.COVER_WIDTH, self.COVER_HEIGHT)
        self.cover_label.setScaledContents(True)
        self.cover_label.setStyleSheet(
            f"""
            QLabel {{
                background-color: {PALETTE["border"]};
                border-radius: 8px;
            }}
            """
        )

        # 左上角"已下载"角标（绿色，仅已下载作品显示）
        self.downloaded_badge = QLabel(self.cover_container)
        self.downloaded_badge.setText("已下载")
        self.downloaded_badge.setAlignment(Qt.AlignCenter)
        self.downloaded_badge.setWordWrap(False)
        self.downloaded_badge.setStyleSheet(
            """
            QLabel {
                background-color: #16A34A;
                color: white;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
                font-weight: 600;
            }
            """
        )
        self.downloaded_badge.adjustSize()
        self.downloaded_badge.setFixedHeight(32)
        # 定位到左上角，留出 8px 边距
        self.downloaded_badge.move(8, 8)
        self.downloaded_badge.setVisible(self._is_downloaded())

        # 右上角分类角标
        self.category_badge = QLabel(self.cover_container)
        self.category_badge.setAlignment(Qt.AlignCenter)
        self.category_badge.setWordWrap(False)
        self.category_badge.setStyleSheet(
            """
            QLabel {
                background-color: rgba(0, 0, 0, 0.55);
                color: white;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
                font-weight: 500;
            }
            """
        )
        self.category_badge.adjustSize()
        # 定位到右上角，留出 8px 边距
        self.category_badge.move(
            self.COVER_WIDTH - self.category_badge.width() - 8,
            8,
        )
        if self.comic.display_category_label:
            self.category_badge.setText(self.comic.display_category_label)
            self.category_badge.adjustSize()
            self.category_badge.move(
                self.COVER_WIDTH - self.category_badge.width() - 8,
                8,
            )
            self.category_badge.setVisible(True)
        else:
            self.category_badge.setVisible(False)

        self.cache_ring = CacheProgressRing(
            self.cover_container, size=self.downloaded_badge.height(), thickness=3
        )
        self._position_cover_badges()

        # 左下角"相似"角标（本地库存在相似作品时显示 ≈N；
        # 相似记录中有已下载时数字变红提示，否则橙色）
        self.similar_badge = QLabel(self.cover_container)
        self.similar_badge.setAlignment(Qt.AlignCenter)
        self.similar_badge.setWordWrap(False)
        self.similar_badge.setToolTip("本地数据库存在相似作品，右键卡片可查看")
        self._update_similar_badge()
        self.similar_badge.setVisible(self.comic.similar_count > 0)

        self.favorite_btn = QPushButton(self.cover_container)
        self.favorite_btn.setObjectName("favoriteButton")
        self.favorite_btn.setFixedSize(34, 34)
        self.favorite_btn.move(
            self.COVER_WIDTH - self.favorite_btn.width() - 8,
            self.COVER_HEIGHT - self.favorite_btn.height() - 8,
        )
        self.favorite_btn.setCursor(Qt.PointingHandCursor)
        self.favorite_btn.setFocusPolicy(Qt.NoFocus)
        self.favorite_btn.clicked.connect(self._toggle_favorite)
        self._update_favorite_button()

        layout.addWidget(self.cover_container)

        # 右侧信息
        info_layout = QVBoxLayout()
        info_layout.setSpacing(8)
        info_layout.setAlignment(Qt.AlignTop)

        self.title_label = QLabel(self)
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.title_label.setStyleSheet(
            f"""
            QLabel {{
                font-size: 15px;
                font-weight: 600;
                color: {PALETTE["text"]};
            }}
            """
        )
        self._update_title_label()

        # 作者按钮可能在加入布局前就被设为可见；必须先指定父控件，
        # 否则 Qt 会把它短暂显示成桌面上的独立小窗口。
        self.author_btn = QPushButton(self)
        self.author_btn.setObjectName("authorButton")
        self.author_btn.setCursor(Qt.PointingHandCursor)
        self.author_btn.setFocusPolicy(Qt.NoFocus)
        self.author_btn.setFixedHeight(24)
        self.author_btn.setMaximumWidth(205)
        self.author_btn.setStyleSheet(
            f"""
            QPushButton {{
                color: {PALETTE['primary']};
                background-color: #EFF6FF;
                border: 1px solid #BFDBFE;
                border-radius: 5px;
                padding: 2px 6px;
                text-align: left;
                font-size: 11px;
            }}
            QPushButton:hover {{ background-color: #DBEAFE; }}
            """
        )
        self.author_btn.clicked.connect(self._on_author_clicked)
        self._update_author_button()

        self.count_label = QLabel(self.comic.count_label_text, self)
        self.count_label.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 13px;"
        )

        self.date_label = QLabel(f"上传: {self.comic.formatted_date}", self)
        self.date_label.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 13px;"
        )

        self.url_label = QLabel(self.comic.detail_url, self)
        self.url_label.setWordWrap(True)
        self.url_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.url_label.setStyleSheet(
            f"color: {PALETTE['primary']}; font-size: 12px;"
        )

        info_layout.addWidget(self.title_label)
        info_layout.addWidget(self.author_btn)
        info_layout.addWidget(self.count_label)
        info_layout.addWidget(self.date_label)
        info_layout.addWidget(self.url_label)
        info_layout.addStretch()

        # 卡片右下角按钮区：阅读 + 下载
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(12)

        # 「阅读」按钮：打开阅读器（本地优先，缺页边看边缓存）
        self.read_btn = QPushButton("阅读", self)
        self.read_btn.setObjectName("readButton")
        self.read_btn.setCursor(Qt.PointingHandCursor)
        self.read_btn.setFocusPolicy(Qt.NoFocus)
        self.read_btn.setFixedSize(104, 32)
        self.read_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {PALETTE["surface"]};
                color: {PALETTE["primary"]};
                border: 1px solid {PALETTE["primary"]};
                border-radius: 6px;
                padding: 4px 14px;
                font-size: 13px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: #EFF6FF;
            }}
            QPushButton:disabled {{
                background-color: {PALETTE["border"]};
                color: {PALETTE["text_secondary"]};
                border-color: {PALETTE["border"]};
            }}
            """
        )
        self.read_btn.clicked.connect(self._on_read_clicked)
        bottom_row.addWidget(self.read_btn)
        bottom_row.addStretch(1)

        self.download_btn = QPushButton("下载", self)
        self.download_btn.setObjectName("downloadButton")
        self.download_btn.setCursor(Qt.PointingHandCursor)
        # 点击不抢焦点：按钮状态切换（下载→下载中/翻译中→已下载/重试）会
        # setEnabled(False)，若按钮获焦，焦点移除会触发 QScrollArea 自动
        # ensureVisible 滚动——按钮恰好部分位于视口边缘外时页面会"自己滑动"。
        # NoFocus 后点击不获焦，状态切换无焦点变化，滚动不再发生。
        self.download_btn.setFocusPolicy(Qt.NoFocus)
        # 固定宽度：文本在「下载/下载中.../已下载/重试/翻译中...」间切换时
        # 宽度保持稳定，避免按钮膨胀导致卡片内布局重排、界面视觉跳动
        self.download_btn.setFixedSize(104, 32)
        self.download_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {PALETTE["primary"]};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 4px 14px;
                font-size: 13px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {PALETTE["primary_hover"]};
            }}
            QPushButton:disabled {{
                background-color: {PALETTE["border"]};
                color: {PALETTE["text_secondary"]};
            }}
            """
        )
        self.download_btn.clicked.connect(self._on_download_clicked)
        bottom_row.addWidget(self.download_btn)
        info_layout.addLayout(bottom_row)

        layout.addLayout(info_layout, stretch=1)

        # 阴影与边框
        self.setStyleSheet(
            f"""
            QFrame#comicCard {{
                background-color: {PALETTE["surface"]};
                border: 1px solid {PALETTE["border"]};
                border-radius: 12px;
            }}
            QFrame#comicCard:hover {{
                background-color: {PALETTE["surface_hover"]};
                border: 1px solid {PALETTE["primary"]};
            }}
            """
        )

    def _load_cover(self):
        """优先加载本地封面，其次网络封面，失败时显示占位图。"""
        pixmap = QPixmap()
        if self.comic.cover_path:
            path = Path(self.comic.cover_path)
            if path.exists():
                pixmap.load(str(path))

        if not pixmap.isNull():
            self._set_cover(pixmap)
            return

        if self.comic.cover_url:
            if self._network is None:
                self._network = QNetworkAccessManager(self)
                self._network.finished.connect(self._on_cover_reply)
            request = QNetworkRequest(self.comic.cover_url)
            # 封面请求设置超时：QNAM 默认无限等待，站点慢时 reply 会长期挂起
            request.setTransferTimeout(15000)
            self._network.get(request)
            return

        self._set_placeholder()

    def _on_cover_reply(self, reply: QNetworkReply):
        if reply.error() == QNetworkReply.NoError:
            data = bytes(reply.readAll())
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            if not pixmap.isNull():
                self._cache_cover(data)
                self._set_cover(pixmap)
                reply.deleteLater()
                return
        self._set_placeholder()
        reply.deleteLater()

    def _cache_cover(self, data: bytes):
        """将封面数据写入本地缓存，下次从数据库/缓存加载时直接使用本地文件。"""
        from ui.database import AID_RE, COVERS_DIR

        match = AID_RE.search(self.comic.detail_url or "")
        if not match:
            return
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        path = COVERS_DIR / f"{match.group(1)}.jpg"
        try:
            path.write_bytes(data)
            self.comic.cover_path = str(path)
        except OSError:
            pass

    def _set_cover(self, pixmap: QPixmap):
        """缩放并显示封面；缩放结果缓存在 QPixmapCache 中，重建卡片时直接复用。"""
        source = self.comic.cover_path or self.comic.cover_url
        key = f"comic_cover:{self.COVER_WIDTH}x{self.COVER_HEIGHT}:{source}"
        scaled = QPixmapCache.find(key)
        if scaled is None or scaled.isNull():
            scaled = pixmap.scaled(
                self.COVER_WIDTH,
                self.COVER_HEIGHT,
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            QPixmapCache.insert(key, scaled)
        self.cover_label.setPixmap(scaled)

    def _set_placeholder(self):
        key = f"comic_cover:{self.COVER_WIDTH}x{self.COVER_HEIGHT}:"
        pixmap = QPixmapCache.find(key)
        if pixmap is None or pixmap.isNull():
            pixmap = QPixmap(self.COVER_WIDTH, self.COVER_HEIGHT)
            pixmap.fill(PALETTE["border"])
            QPixmapCache.insert(key, pixmap)
        self.cover_label.setPixmap(pixmap)

    def _on_download_clicked(self):
        """点击下载按钮：交由下载管理器创建后台任务。"""
        if not self.comic.detail_url:
            QMessageBox.information(self, "下载", "该作品没有详情页链接，无法下载。")
            return
        self._manager.start_download(self.comic)
        self._sync_download_button()

    def _on_read_clicked(self):
        """点击阅读按钮：通知主窗口打开阅读器窗口。"""
        if not self.comic.detail_url:
            QMessageBox.information(self, "阅读", "该作品没有详情页链接，无法阅读。")
            return
        self.read_requested.emit(self.comic)

    def _update_favorite_button(self):
        favored = bool(self.comic.favorite)
        self.favorite_btn.setText("★" if favored else "☆")
        self.favorite_btn.setToolTip("取消收藏" if favored else "收藏作品")
        self.favorite_btn.setStyleSheet(
            "QPushButton { background: rgba(20, 24, 31, 0.72); border: none; "
            f"border-radius: 17px; color: {'#FACC15' if favored else '#FFFFFF'}; "
            "font-size: 26px; padding: 0; }"
            "QPushButton:hover { background: rgba(20, 24, 31, 0.9); }"
        )

    def _toggle_favorite(self):
        from ui.database import DatabaseManager

        target = not self.comic.favorite
        record = {
            "title": self.comic.title,
            "detail_url": self.comic.detail_url,
            "date": self.comic.upload_date,
            "image_count": self.comic.image_count,
            "chapter_count": self.comic.chapter_count,
            "cover_url": self.comic.cover_url,
            "category": self.comic.category,
            "category_label": self.comic.category_label,
            "translated_title": self.comic.translated_title,
        }
        try:
            DatabaseManager.instance().set_favorite(record, target)
        except Exception as exc:
            QMessageBox.warning(self, "收藏失败", str(exc))
            return
        self.comic.favorite = target
        self._update_favorite_button()
        self.favorite_changed.emit(self.comic, target)

    # ---- 缓存进度圈 ----

    def set_downloaded_state(self, downloaded: bool):
        """同步当前作品的已下载角标及其关联的缓存进度环。"""
        self._pre_downloaded = bool(downloaded)
        self.downloaded_badge.setVisible(self._pre_downloaded)
        self._position_cover_badges()

    def _position_cover_badges(self):
        """仅在已下载角标出现时显示等高进度环，并避开分类角标。"""
        downloaded = not self.downloaded_badge.isHidden()
        self.cache_ring.setVisible(downloaded)
        ring_x = self.downloaded_badge.x() + self.downloaded_badge.width() + 6
        ring_y = self.downloaded_badge.y() + (
            self.downloaded_badge.height() - self.cache_ring.height()
        ) // 2
        self.cache_ring.move(ring_x, ring_y)
        if not self.category_badge.isHidden():
            overlaps = downloaded and ring_x + self.cache_ring.width() + 4 > self.category_badge.x()
            self.category_badge.move(
                self.category_badge.x(), self.downloaded_badge.height() + 16 if overlaps else 8
            )

    def set_cache_progress(self, percent: int, cached: int = 0, total: int = 0):
        """设置本地缓存进度（网格批量预计算后调用，避免卡片各自扫盘）。"""
        self._pre_cache = (int(percent), int(cached), int(total))
        self.cache_ring.set_progress(percent, cached, total)
        self._position_cover_badges()

    def refresh_cache_progress(self):
        """按需自查缓存进度（下载/阅读完成等状态变化后调用）。"""
        if self._pre_cache is not None:
            self.cache_ring.set_progress(*self._pre_cache)
            self._position_cover_badges()
            return
        from ui.reader.cache_index import cache_progress, find_local_dir

        folder = find_local_dir(
            {
                "title": self.comic.title,
                "detail_url": self.comic.detail_url,
                "category_label": self.comic.display_category_label,
                "original_category_label": self.comic.category_label,
                "translated_title": self.comic.translated_title,
            }
        )
        cached, total, percent = cache_progress(folder, self.comic.image_count)
        self.cache_ring.set_progress(percent, cached, total)
        self._position_cover_badges()

    def _is_downloaded(self) -> bool:
        """判断当前作品是否已下载（任务完成、数据库标记或下载目录文件夹）。

        优先使用网格批量预计算的结果（True/False），避免独立全表遍历与磁盘检查。
        """
        if self._pre_downloaded is not None:
            return self._pre_downloaded
        task = self._manager.task_for(self.comic)
        if task and task.status == DownloadTask.DONE:
            return True
        from ui.database import is_downloaded

        return is_downloaded(self.comic.detail_url, self.comic.title)

    def _sync_download_button(self, task: DownloadTask | None = None):
        """根据下载管理器中的任务状态，同步按钮文本与可用性。

        Args:
            task: 定向信号传入的本作品任务（免遍历任务列表）；None 时自行查找。
        """
        if task is None:
            task = self._manager.task_for(self.comic)
        if task is None:
            self.download_btn.setEnabled(True)
            self.download_btn.setText("下载")
            return

        if task.status in (DownloadTask.WAITING, DownloadTask.DOWNLOADING):
            self.download_btn.setEnabled(False)
            self.download_btn.setText("下载中...")
        elif task.status == DownloadTask.DONE:
            self.download_btn.setEnabled(True)
            self.download_btn.setText("已下载")
            self.downloaded_badge.setVisible(True)
            self._position_cover_badges()
        elif task.status == DownloadTask.FAILED:
            self.download_btn.setEnabled(True)
            self.download_btn.setText("重试")

    def contextMenuEvent(self, event: QContextMenuEvent):
        """右键卡片弹出上下文菜单：复制/翻译作品名称。

        QMenu 是独立弹出窗口，不继承项目浅色主题；系统深色模式下默认黑底，
        与项目 QSS 的深色文字叠加会出现黑底黑字，因此显式设置浅色样式。
        """
        self._build_context_menu().exec(event.globalPos())

    @staticmethod
    def _menu_color_icon(color: str) -> QIcon:
        """生成 12x12 圆角色块图标，作为右键菜单项的颜色提示。"""
        pixmap = QPixmap(12, 12)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(0, 0, 11, 11)
        painter.end()
        return QIcon(pixmap)

    def _build_context_menu(self) -> QMenu:
        """构建右键上下文菜单（独立方法便于测试）。

        菜单项带固定颜色圆点提示，与角标语义一致：
        - 复制作品名称：灰色
        - 查看类似作品：橙色（相似中有已下载时为红色）
        - 翻译作品名称：蓝色
        """
        menu = QMenu(self)
        menu.setStyleSheet(
            f"""
            QMenu {{
                background-color: {PALETTE["surface"]};
                color: {PALETTE["text"]};
                border: 1px solid {PALETTE["border"]};
                border-radius: 8px;
                padding: 6px;
            }}
            QMenu::item {{
                background-color: transparent;
                color: {PALETTE["text"]};
                padding: 6px 24px;
                border-radius: 6px;
                font-size: 13px;
            }}
            QMenu::item:selected {{
                background-color: {PALETTE["primary"]};
                color: white;
            }}
            """
        )
        menu.addAction(
            self._menu_color_icon("#9CA3AF"), "复制作品名称", self._copy_title
        )
        menu.addSeparator()
        # 「阅读作品」：本地优先；无本地缓存时在线读并边看边缓存
        menu.addAction(
            self._menu_color_icon("#10B981"), "阅读作品", self._on_read_clicked
        )
        menu.addSeparator()
        # 仅本地库存在相似作品时提供「查看类似」，避免无意义的空视图
        if self.comic.similar_count > 0:
            similar_color = "#DC2626" if self.comic.similar_downloaded else "#F97316"
            menu.addAction(
                self._menu_color_icon(similar_color),
                "查看类似作品",
                self._on_view_similar,
            )
            menu.addSeparator()
        menu.addAction(
            self._menu_color_icon("#3B82F6"), "翻译作品名称", self._translate_title
        )
        return menu

    def _on_view_similar(self):
        """触发「查看类似作品」：由主窗口切换为只显示该作品与相似作品。"""
        self.similar_requested.emit(self.comic)

    def _copy_title(self):
        """将作品显示名称（含繁简转换）复制到剪贴板，左下角状态栏提示。"""
        text = self.comic.display_title
        QApplication.clipboard().setText(text)
        self._show_status(f"已复制作品名称：{text}")

    def _update_title_label(self):
        """刷新标题显示（翻译结果更新后调用）。"""
        full_title = self.comic.display_title
        shown_title = full_title if len(full_title) <= 40 else full_title[:40] + "..."
        self.title_label.setText(shown_title)
        self.title_label.setToolTip(full_title if shown_title != full_title else "")

    def _update_author_button(self):
        from ui.database import extract_author

        self._author = extract_author(self.comic.title)
        if self._author:
            text = f"作者：{self._author}"
            shown = self.author_btn.fontMetrics().elidedText(text, Qt.ElideRight, 205)
            self.author_btn.setText(shown)
            self.author_btn.setToolTip(f"搜索作者：{self._author}")
        self.author_btn.setVisible(bool(self._author))

    def _on_author_clicked(self):
        if self._author:
            self.author_requested.emit(self._author)

    def _show_status(self, text: str):
        """在左下角状态栏显示提示（经由主窗口 set_status，超长截断）。"""
        window = self.window()
        set_status = getattr(window, "set_status", None)
        if set_status:
            shown = text if len(text) <= 60 else text[:60] + "..."
            set_status(shown)

    def _translate_title(self):
        """翻译作品名称：异步调用 AI 接口，完成后入库、刷新标题并复制。"""
        from ui.settings import AppSettings
        from ui.translate import TranslateWorker

        settings = AppSettings()
        if not settings.translate_api_key:
            QMessageBox.information(
                self,
                "翻译",
                "尚未配置 API Key，请先到 设置 → 名称翻译设置 中填写。",
            )
            return

        self.download_btn.setText("翻译中...")
        self.download_btn.setEnabled(False)

        worker = TranslateWorker(
            self.comic.title,
            settings.translate_api_base,
            settings.translate_api_key,
            settings.translate_model,
        )
        self._translate_worker = worker  # 保持引用，防止线程被回收
        worker.done.connect(self._on_translate_done)
        worker.error.connect(self._on_translate_error)
        # 清理连原生 finished（线程完全停止后才触发）：done/error 在 run()
        # 返回前 emit，若连 deleteLater 会在底层线程仍在运行时销毁 QThread，
        # 触发 "QThread: Destroyed while thread is still running" 闪退
        worker.finished.connect(worker.deleteLater)
        register(worker)
        worker.start()

    def _on_translate_done(self, result: str):
        self._translate_worker = None
        self._sync_download_button()
        # 翻译结果写入数据库并刷新卡片标题（下次启动仍显示翻译名）
        self.comic.translated_title = result
        self._update_title_label()
        from ui.database import DatabaseManager

        DatabaseManager.instance().save_translated_title(
            self.comic.detail_url, result
        )
        QApplication.clipboard().setText(result)
        self._show_status(f"翻译成功：{result}（已复制到剪贴板）")

    def _on_translate_error(self, message: str):
        self._translate_worker = None
        self._sync_download_button()
        QMessageBox.warning(self, "翻译失败", message)
