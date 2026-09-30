"""缓存进度环控件：用环形进度表达「本地已缓存百分比」。

绘制方式（对应用户指定要求）：
- 先画一整圈**灰色底环**（未完成部分）；
- 再在其上叠加一段**彩色圆弧**，弧长 = 百分比 × 360°，
  从 12 点方向顺时针增长 —— 50% 时正好是半圈彩色 + 半圈灰底；
- 圆心/中央显示百分比数字（0-99），100% 时显示 ✓ 并把整圈变绿。

颜色语义：
- 进行中/部分缓存：橙色（#F97316）
- 已缓存完整：绿色（#16A34A）
- 底环：浅灰（#E5E7EB）
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

# 颜色常量（供外部复用于 tooltip 等文案）
COLOR_PROGRESS = "#F97316"
COLOR_COMPLETE = "#16A34A"
COLOR_TRACK = "#E5E7EB"
COLOR_TEXT = "#6B7280"


class CacheProgressRing(QWidget):
    """环形缓存进度圈。

    Args:
        size: 控件边长（正方形），默认 28px。
            内圈直径 = size - 2×thickness，需 ≥16px 才能容纳两位数百分比，
            因此尺寸下限约 24px（3px 线宽时）。
        thickness: 圆环线宽，默认 3px。
        show_text: 是否在圆心显示百分比数字；圆环过小时可关闭。
    """

    def __init__(
        self,
        parent=None,
        size: int = 28,
        thickness: int = 3,
        show_text: bool = True,
    ):
        super().__init__(parent)
        self._size = int(size)
        self._thickness = int(thickness)
        self._show_text = bool(show_text)
        self._percent = 0          # 0-100
        self._cached = 0           # 已缓存张数（tooltip 用）
        self._total = 0            # 期望总数（tooltip 用）
        self.setFixedSize(self._size, self._size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._refresh_tooltip()

    # ---- 状态设置 ----

    def set_progress(self, percent: int, cached: int = 0, total: int = 0):
        """更新进度并重绘。

        Args:
            percent: 0-100 的整数百分比（越界自动夹取）。
            cached / total: 张数，仅用于 tooltip 展示。
        """
        percent = max(0, min(int(percent), 100))
        if (
            percent == self._percent
            and cached == self._cached
            and total == self._total
        ):
            return
        self._percent = percent
        self._cached = int(cached)
        self._total = int(total)
        self._refresh_tooltip()
        self.setVisible(percent > 0)
        self.update()

    @property
    def percent(self) -> int:
        return self._percent

    def _refresh_tooltip(self):
        if self._total > 0:
            self.setToolTip(f"已缓存 {self._cached} / {self._total} 张（{self._percent}%）")
        else:
            self.setToolTip(f"已缓存 {self._cached} 张")

    # ---- 绘制 ----

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 圆环矩形：留出线宽的一半，避免边缘被裁切
        inset = self._thickness / 2 + 1
        rect = self.rect().adjusted(inset, inset, -inset, -inset)

        # 1) 灰色底环（整圈）
        track_pen = QPen(QColor(COLOR_TRACK), self._thickness)
        track_pen.setCapStyle(Qt.FlatCap)
        painter.setPen(track_pen)
        painter.drawEllipse(rect)

        # 2) 彩色进度弧：从 12 点顺时针增长
        if self._percent > 0:
            color = QColor(COLOR_COMPLETE if self._percent >= 100 else COLOR_PROGRESS)
            arc_pen = QPen(color, self._thickness)
            arc_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(arc_pen)
            # Qt 角度单位 1/16 度；起始 90°（12 点），负跨度表示顺时针
            span = -int(360 * 16 * self._percent / 100)
            painter.drawArc(rect, 90 * 16, span)

        # 3) 中央文字：100% 显示对勾，否则显示百分比数字。
        #    字号按实际文字宽度自适应收缩，确保数字落在内圈之内；
        #    收缩到可读下限仍放不下时只画圆环（弧长本身已表达百分比）。
        if not self._show_text:
            painter.end()
            return
        inner = self._size - 2 * self._thickness
        if inner < 12:
            painter.end()
            return
        if self._percent >= 100:
            text = "✓"
        elif self._percent > 0:
            text = f"{self._percent}"
        else:
            text = ""
        if text:
            font = QFont(self.font())
            font.setBold(True)
            # 从内圈的 0.68 起，逐步缩小直到文字宽度（含 2px 边距）落入内圈
            available = inner - 2
            pixel = max(8, int(inner * 0.68))
            font.setPixelSize(pixel)
            while pixel > 8 and QFontMetrics(font).horizontalAdvance(text) > available:
                pixel -= 1
                font.setPixelSize(pixel)
            painter.setFont(font)
            painter.setPen(QColor(COLOR_COMPLETE if self._percent >= 100 else COLOR_TEXT))
            painter.drawText(self.rect(), Qt.AlignCenter, text)

        painter.end()
