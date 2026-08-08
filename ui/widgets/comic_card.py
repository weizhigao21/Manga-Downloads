from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QContextMenuEvent, QPixmap, QPixmapCache
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ui.download_manager import DownloadManager, DownloadTask
from ui.models import ComicItem
from ui.styles import PALETTE


class ComicCard(QFrame):
    """漫画条目卡片组件。"""

    COVER_WIDTH = 160
    COVER_HEIGHT = 220

    def __init__(self, comic: ComicItem, parent=None):
        super().__init__(parent)
        self.comic = comic
        # 本地已有封面时不创建网络管理器（避免每张卡片都初始化网络栈）
        self._network: QNetworkAccessManager | None = None
        self._manager = DownloadManager.instance()
        self._setup_ui()
        self._load_cover()
        # 下载任务状态变化时同步按钮（卡片被销毁后 Qt 会自动断开连接）
        self._manager.task_updated.connect(self._sync_download_button)
        self._sync_download_button()

    def _setup_ui(self):
        self.setObjectName("comicCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(320, 262)

        # 卡片整体布局
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # 封面容器（用于承载封面图与右上角角标）
        self.cover_container = QFrame()
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
        # 定位到左上角，留出 8px 边距
        self.downloaded_badge.move(8, 8)
        self.downloaded_badge.setVisible(self._is_downloaded())

        # 右上角分类角标
        self.category_badge = QLabel(self.cover_container)
        self.category_badge.setAlignment(Qt.AlignCenter)
        self.category_badge.setWordWrap(False)
        self.category_badge.setStyleSheet(
            f"""
            QLabel {{
                background-color: rgba(0, 0, 0, 0.55);
                color: white;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
                font-weight: 500;
            }}
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

        layout.addWidget(self.cover_container)

        # 右侧信息
        info_layout = QVBoxLayout()
        info_layout.setSpacing(8)
        info_layout.setAlignment(Qt.AlignTop)

        # 标题超过 40 个字符时截断显示省略号，完整标题放入提示
        full_title = self.comic.display_title
        shown_title = full_title if len(full_title) <= 40 else full_title[:40] + "..."
        self.title_label = QLabel(shown_title)
        if shown_title != full_title:
            self.title_label.setToolTip(full_title)
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet(
            f"""
            QLabel {{
                font-size: 15px;
                font-weight: 600;
                color: {PALETTE["text"]};
            }}
            """
        )

        self.count_label = QLabel(f"图片: {self.comic.count_text}")
        self.count_label.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 13px;"
        )

        self.date_label = QLabel(f"上传: {self.comic.formatted_date}")
        self.date_label.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 13px;"
        )

        self.url_label = QLabel(self.comic.detail_url)
        self.url_label.setWordWrap(True)
        self.url_label.setStyleSheet(
            f"color: {PALETTE['primary']}; font-size: 12px;"
        )

        info_layout.addWidget(self.title_label)
        info_layout.addWidget(self.count_label)
        info_layout.addWidget(self.date_label)
        info_layout.addWidget(self.url_label)
        info_layout.addStretch()

        # 卡片右下角下载按钮
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)
        bottom_row.addStretch()
        self.download_btn = QPushButton("下载")
        self.download_btn.setObjectName("downloadButton")
        self.download_btn.setCursor(Qt.PointingHandCursor)
        self.download_btn.setFixedHeight(28)
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

    def _is_downloaded(self) -> bool:
        """判断当前作品是否已下载（任务完成、数据库标记或下载目录文件夹）。"""
        task = self._manager.task_for(self.comic)
        if task and task.status == DownloadTask.DONE:
            return True
        from ui.database import is_downloaded

        return is_downloaded(self.comic.detail_url, self.comic.title)

    def _sync_download_button(self):
        """根据下载管理器中的任务状态，同步按钮文本与可用性。"""
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
        elif task.status == DownloadTask.FAILED:
            self.download_btn.setEnabled(True)
            self.download_btn.setText("重试")

    def contextMenuEvent(self, event: QContextMenuEvent):
        """右键卡片弹出上下文菜单：复制作品名称到剪贴板。

        QMenu 是独立弹出窗口，不继承项目浅色主题；系统深色模式下默认黑底，
        与项目 QSS 的深色文字叠加会出现黑底黑字，因此显式设置浅色样式。
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
        menu.addAction("复制作品名称", self._copy_title)
        menu.exec(event.globalPos())

    def _copy_title(self):
        """将作品显示名称（含繁简转换）复制到剪贴板。"""
        text = self.comic.display_title
        QApplication.clipboard().setText(text)
        self._show_copied_hint()

    def _show_copied_hint(self):
        """在下载按钮位置短暂显示“已复制”提示，提供操作反馈。"""
        if not hasattr(self, "download_btn"):
            return
        from PySide6.QtCore import QTimer

        original_text = self.download_btn.text()
        self.download_btn.setText("已复制")
        QTimer.singleShot(1200, lambda: self.download_btn.setText(original_text))
