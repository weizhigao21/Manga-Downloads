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

    def render(self, comics: list[ComicItem]):
        """渲染漫画列表，替换旧卡片（仅数据变化时调用）。

        刷新/翻页/搜索产生新数据时自动回到列表顶部。
        """
        self._clear()
        self.verticalScrollBar().setValue(0)
        self._cards = []
        self._columns = 0

        if not comics:
            empty_label = QLabel("暂无数据")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("font-size: 16px; color: #9CA3AF; margin-top: 80px;")
            self._grid.addWidget(empty_label, 0, 0, 1, 1)
            self._grid.setColumnStretch(1, 1)
            return

        for comic in comics:
            self._cards.append(ComicCard(comic))
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

    def _clear(self):
        """清空网格中的所有旧卡片。"""
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
