"""漫画网格视图：承载卡片并负责响应式布局。

从 main_window 拆出，独立成模块：负责根据可视宽度动态计算列数、
渲染 ComicCard，以及无数据时的空状态展示。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QScrollArea, QWidget

from ui.models import ComicItem
from ui.widgets.comic_card import ComicCard


class ComicsGrid(QScrollArea):
    """漫画卡片网格视图。"""

    # 每列最小宽度（px），窗口变宽时列数自动增加
    MIN_COLUMN_WIDTH = 340

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._content.setStyleSheet("background-color: transparent;")
        self._grid = QGridLayout(self._content)
        self._grid.setContentsMargins(24, 24, 24, 24)
        self._grid.setSpacing(20)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.setWidget(self._content)

        # 当前持有的卡片与已计算列数；列数未变化时 resize 重排直接跳过
        self._cards: list[ComicCard] = []
        self._columns = 0
        self._empty_label: QLabel | None = None

    def render(self, comics: list[ComicItem]):
        """渲染漫画列表（刷新/翻页/搜索产生新数据时调用，自动回到列表顶部）。

        与全量重建相比：
        - 已下载状态批量预计算（一次磁盘检查替代每卡片独立扫描）；
        - 按 detail_url 复用相同作品的卡片（更新数据），仅重建新增/移除的。
        """
        if self._empty_label is not None:
            self._grid.removeWidget(self._empty_label)
            self._empty_label.deleteLater()
            self._empty_label = None

        # 批量预计算"已下载"状态：单次遍历缓存 + 每作品一次目录检查
        from ui.database import is_comics_downloaded

        downloaded_map = is_comics_downloaded(
            [
                (c.detail_url, c.title, c.translated_title, c.display_category_label)
                for c in comics
            ]
        )

        # 按 detail_url diff：复用相同作品卡片，仅重建新增与移除的
        old = {c.comic.detail_url: c for c in self._cards}
        new_cards: list[ComicCard] = []
        for comic in comics:
            card = old.pop(comic.detail_url, None)
            if card is None:
                card = ComicCard(comic, downloaded=downloaded_map.get(comic.detail_url))
            else:
                card.update_comic(
                    comic, downloaded=downloaded_map.get(comic.detail_url)
                )
            new_cards.append(card)
        for stale in old.values():
            stale.deleteLater()

        self._cards = new_cards
        self._columns = 0
        self.verticalScrollBar().setValue(0)

        if not comics:
            empty_label = QLabel("暂无数据")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("font-size: 16px; color: #9CA3AF; margin-top: 80px;")
            self._empty_label = empty_label
            self._grid.addWidget(empty_label, 0, 0, 1, 1)
            self._grid.setColumnStretch(1, 1)
            return

        self._reflow()

    def reflow(self):
        """窗口大小变化后按需重排：列数未变时不重建任何控件。"""
        if not self._cards:
            return
        self._reflow()

    def _reflow(self):
        """按当前可视宽度重排已有卡片。

        列数变化时仅移动卡片在网格中的位置（不重建控件、不重载封面、
        不查数据库），避免窗口缩放时反复销毁/重建大量卡片导致卡顿。
        """
        width = max(self.viewport().width(), self.MIN_COLUMN_WIDTH)
        columns = max(1, width // self.MIN_COLUMN_WIDTH)
        if columns == self._columns:
            return
        self._columns = columns

        for index, card in enumerate(self._cards):
            self._grid.removeWidget(card)
            self._grid.addWidget(card, index // columns, index % columns)

        # 让最后一列伸展，避免卡片被拉伸
        for col in range(columns + 1):
            self._grid.setColumnStretch(col, 0)
        self._grid.setColumnStretch(columns, 1)
