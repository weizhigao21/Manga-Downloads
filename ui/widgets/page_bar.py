"""底部翻页栏组件。

从 main_window 拆出，独立成模块：封装上一页/下一页按钮、当前页码
显示与跳页输入，点击或提交时通过 page_requested 信号通知外部。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QWidget,
)

# 站点列表页最多 5 位数页码，控件固定宽度以容纳「第 12345 页」
_PAGE_SPIN_WIDTH = 88
_PAGE_SPIN_MAX = 99_999


class PageBar(QFrame):
    """翻页栏：上一页 / 页码(可输入跳页) / 下一页。"""

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

        # 翻页按钮组：上一页 | 第 N 页(可编辑跳页) | 下一页（紧凑排列，水平居中）
        group = QWidget()
        group_layout = QHBoxLayout(group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(12)

        self.prev_btn = QPushButton("上一页")
        self.prev_btn.setObjectName("pageButton")
        self.prev_btn.clicked.connect(lambda: self.page_requested.emit(self._page - 1))
        group_layout.addWidget(self.prev_btn)

        # 页码框：直接输入数字回车即跳页（替换原纯展示 QLabel）。
        # 用 setFixedWidth 压住尺寸——QSpinBox 的 sizeHint 会随 maximum
        # 位数膨胀，不固定宽度时布局会大幅偏移。
        self.page_spin = QSpinBox()
        self.page_spin.setObjectName("pageSpinBox")
        self.page_spin.setRange(1, _PAGE_SPIN_MAX)
        self.page_spin.setPrefix("第 ")
        self.page_spin.setSuffix(" 页")
        self.page_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.page_spin.setAlignment(Qt.AlignCenter)
        self.page_spin.setKeyboardTracking(False)  # 输入过程不实时触发，提交时才处理
        self.page_spin.setFixedWidth(_PAGE_SPIN_WIDTH)
        self.page_spin.editingFinished.connect(self._on_editing_finished)
        group_layout.addWidget(self.page_spin)

        self.next_btn = QPushButton("下一页")
        self.next_btn.setObjectName("pageButton")
        self.next_btn.clicked.connect(lambda: self.page_requested.emit(self._page + 1))
        group_layout.addWidget(self.next_btn)

        layout.addStretch()
        layout.addWidget(group)
        layout.addStretch()

    def _on_editing_finished(self):
        """页码框提交（回车/失焦）：页码变化时才请求跳转。

        失焦也会触发 editingFinished，值未变时直接忽略，避免重复抓取。
        """
        target = self.page_spin.value()
        if target != self._page:
            self.page_requested.emit(target)

    def set_page(self, page: int):
        """更新当前页码并刷新翻页栏状态。"""
        self._page = max(1, page)
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(self._page)
        self.page_spin.blockSignals(False)
        self.prev_btn.setEnabled(self._page > 1)
