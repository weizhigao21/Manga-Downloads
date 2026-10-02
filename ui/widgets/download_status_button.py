"""下载状态按钮：用**单个控件**承载「下载 / 进度 / 已下载 / 重试」四种状态。

设计目标（对应用户要求）：
- 作品下载完成 —— 按钮整体变绿并显示「已下载」，封面左上角不再叠加任何角标；
- 在线阅读的缓存进度 —— 按钮内部画绿色进度条并居中显示百分比；
- 下载任务进行中 —— 同样画进度条（下载进度优先于缓存进度）；
- 失败 —— 红色「重试」。

尺寸固定 104x32：文本在「下载 / 33% / 已下载 / 重试 / 翻译中...」之间切换、
或实心态与进度态切换时，卡片内布局都不会跳动。

绘制完全由 paintEvent 负责（不走 QSS），因此进度填充宽度可逐帧变化而无需
重新解析样式表——列表里同时滚动几十张卡片时也不会卡顿。
"""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QPushButton

from ui.styles import PALETTE

# 状态 -> (填充色, 悬停填充色, 文字色)
STATE_COLORS: dict[str, tuple[str, str, str]] = {
    "primary": (PALETTE["primary"], PALETTE["primary_hover"], "#FFFFFF"),
    # 用比主题 success(#10B981) 更深的绿：白字对比度更足，也更接近原角标色
    "success": ("#16A34A", "#15803D", "#FFFFFF"),
    "danger": (PALETTE["error"], "#DC2626", "#FFFFFF"),
    "muted": (PALETTE["border"], "#D1D5DB", PALETTE["text_secondary"]),
}

# 进度态配色：深绿填充 + 浅绿底槽 + 近黑百分比。
# 百分比数字不用白色：进度条后半段是浅色底槽，白字压在上面几乎看不见。
PROGRESS_FILL = "#16A34A"
PROGRESS_FILL_HOVER = "#15803D"
PROGRESS_TRACK = "#BBF7D0"
PROGRESS_TEXT = "#0F172A"


class DownloadStatusButton(QPushButton):
    """下载按钮：进度态画「底槽 + 填充条 + 百分比」，完成态为绿色实心按钮。"""

    WIDTH = 104
    HEIGHT = 32
    RADIUS = 6

    def __init__(self, text: str = "下载", parent=None):
        super().__init__(text, parent)
        self._variant = "primary"
        self._progress: int | None = None
        self._hovered = False
        self._setup()

    def _setup(self):
        self.setObjectName("downloadButton")
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        # 不抢焦点：状态切换会 setEnabled(False)，若按钮持有焦点，焦点移除会
        # 触发 QScrollArea 自动 ensureVisible 滚动——按钮恰好部分位于视口边缘
        # 外时页面会"自己滑动"。NoFocus 后点击不获焦，状态切换不改变焦点。
        self.setFocusPolicy(Qt.NoFocus)
        # 底色/描边全部由 paintEvent 画，清掉 QSS 背景避免两层叠加
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    # ---- 对外 API ----

    @property
    def variant(self) -> str:
        """当前配色语义：primary / success / danger / muted。"""
        return self._variant

    @property
    def progress(self) -> int | None:
        """当前进度百分比；None 表示实心按钮（非进度态）。"""
        return self._progress

    @property
    def is_progress(self) -> bool:
        return self._progress is not None

    def set_status(
        self,
        text: str,
        variant: str = "primary",
        progress: int | None = None,
        tooltip: str | None = None,
    ):
        """一次性设置文本 / 配色 / 进度。

        Args:
            text: 按钮文字（进度态通常传 "42%"）。
            variant: primary（进行中/未下载）、success（已下载）、
                danger（失败重试）、muted（翻译中等等待态）。
            progress: 0-100 时画进度条；None 画实心按钮（越界自动夹取）。
            tooltip: 非 None 时覆盖悬停提示。
        """
        progress = None if progress is None else max(0, min(int(progress), 100))
        if variant not in STATE_COLORS:
            variant = "primary"
        repaint = self._variant != variant or self._progress != progress
        self._variant = variant
        self._progress = progress
        if text != self.text():
            self.setText(text)
        if tooltip is not None and tooltip != self.toolTip():
            self.setToolTip(tooltip)
        if repaint:
            self.update()

    # ---- 交互反馈 ----

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    # ---- 绘制 ----

    def _fill_color(self) -> str:
        """实心按钮的填充色；进度态由 paintEvent 单独用绿色系绘制。"""
        fill, hover_fill, _ = STATE_COLORS[self._variant]
        if self._hovered and self.isEnabled():
            return hover_fill
        return fill

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(rect, self.RADIUS, self.RADIUS)

        hovering = self._hovered and self.isEnabled()
        if self._progress is None:
            painter.fillPath(path, QColor(self._fill_color()))
            text_color = QColor(STATE_COLORS[self._variant][2])
            weight = QFont.Weight.Medium
        else:
            # 进度态统一绿色系：未完成=浅绿底槽，已完成=深绿填充，
            # 百分比用近黑粗体，无论数字压在填充还是底槽上都清楚。
            painter.fillPath(path, QColor(PROGRESS_TRACK))
            if self._progress > 0:
                painter.save()
                painter.setClipPath(path)  # 裁剪成圆角，填充条不戳出按钮圆角
                painter.fillRect(
                    QRectF(0, 0, rect.width() * self._progress / 100.0, rect.height()),
                    QColor(PROGRESS_FILL_HOVER if hovering else PROGRESS_FILL),
                )
                painter.restore()
            text_color = QColor(PROGRESS_TEXT)
            weight = QFont.Weight.DemiBold

        font = QFont(self.font())
        font.setPixelSize(13)
        font.setWeight(weight)
        painter.setFont(font)
        painter.setPen(text_color)
        painter.drawText(self.rect(), Qt.AlignCenter, self.text())
        painter.end()
