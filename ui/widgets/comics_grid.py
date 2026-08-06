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

    def render(self, comics: list[ComicItem]):
        """渲染漫画列表，替换旧卡片。"""
        self._clear()

        if not comics:
            empty_label = QLabel("暂无数据")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("font-size: 16px; color: #9CA3AF; margin-top: 80px;")
            self._grid.addWidget(empty_label, 0, 0, 1, 1)
            self._grid.setColumnStretch(1, 1)
            return

        width = max(self.viewport().width(), self.MIN_COLUMN_WIDTH)
        columns = max(1, width // self.MIN_COLUMN_WIDTH)

        for index, comic in enumerate(comics):
            card = ComicCard(comic)
            self._grid.addWidget(card, index // columns, index % columns)

        # 让最后一列伸展，避免卡片被拉伸
        for col in range(columns):
            self._grid.setColumnStretch(col, 0)
        self._grid.setColumnStretch(columns, 1)

    def _clear(self):
        """清空网格中的所有旧卡片。"""
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
