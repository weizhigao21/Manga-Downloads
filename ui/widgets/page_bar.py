"""底部翻页栏组件。

从 main_window 拆出，独立成模块：封装上一页/下一页按钮、当前页码
显示与翻页状态，点击时通过 page_requested 信号通知外部。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget


class PageBar(QFrame):
    """翻页栏：上一页 / 页码 / 下一页。"""

    # 请求跳转的目标页码
    page_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # 背景由外层底部栏（main_window 中的 QFrame#pageBar）统一提供，
        # 自身保持透明，避免两个色块叠加产生色差
        # 水平扩展：铺满底部栏整行，让按钮组以窗口中心为基准居中
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._page = 1

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(12)

        # 翻页按钮组：上一页 | 第 1 页 | 下一页（紧凑排列，水平居中）
        group = QWidget()
        group_layout = QHBoxLayout(group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(12)

        self.prev_btn = QPushButton("上一页")
        self.prev_btn.setObjectName("pageButton")
        self.prev_btn.clicked.connect(lambda: self.page_requested.emit(self._page - 1))
        group_layout.addWidget(self.prev_btn)

        self.page_label = QLabel("第 1 页")
        self.page_label.setObjectName("pageLabel")
        self.page_label.setAlignment(Qt.AlignCenter)
        group_layout.addWidget(self.page_label)

        self.next_btn = QPushButton("下一页")
        self.next_btn.setObjectName("pageButton")
        self.next_btn.clicked.connect(lambda: self.page_requested.emit(self._page + 1))
        group_layout.addWidget(self.next_btn)

        layout.addStretch()
        layout.addWidget(group)
        layout.addStretch()

    def set_page(self, page: int):
        """更新当前页码并刷新翻页栏状态。"""
        self._page = max(1, page)
        self.page_label.setText(f"第 {self._page} 页")
        self.prev_btn.setEnabled(self._page > 1)
