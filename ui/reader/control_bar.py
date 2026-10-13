"""阅读器控制条：独立停靠窗口，固定停靠屏幕底部中央。

设计由来（用户两次反馈，最终澄清的诉求）：
- 旧形态：控制条是阅读器窗口布局的一部分，窗口按图片比例缩放/移动时
  底部按钮跟着跑，连点「下一页」必点空；
- v1.3.13 曾尝试「冻结图片窗口几何」解决，但用户要的是
  **「图片窗口可以动，按钮不能动」**——窗口照旧随图片大小自适应，
  按钮则必须钉死在固定位置。

因此把控制条从图片窗口里剥出来：一个 Qt::Tool 小窗，永远停在当前屏幕
可用区域的底部中央。硬约束：
- 位置固定：除「跟随读者换屏」外，任何图片宽高变化、窗口缩放/移动都
 不影响它——按钮的屏幕位置全程不变；
- 宽度固定：状态文字长短变化不会让按钮横向漂移；
- 不抢焦点（WindowDoesNotAcceptFocus + WA_ShowWithoutActivating）：
  点按钮不影响图片窗口的方向键/空格翻页；
- 多阅读器并存时，只有最前面那个阅读器的控制条可见（见 raise_bar）。
"""
import logging

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# 控制条底边与屏幕可用区域底边的间距（任务栏之上）
_BOTTOM_MARGIN = 16
# 控制条最小宽度：固定宽度是「按钮不横向漂移」的前提
_MIN_WIDTH = 660

# 控制条配色（卡片底 + 描边 + 圆角半径，与窗口 QSS 的色系一致）
_BAR_BG = QColor("#23262B")
_BAR_BORDER = QColor("#34383F")
_BAR_RADIUS = 10

# 存活的控制条注册表（按置前顺序，最后一个是最前）。多阅读器并存时，
# 只有最前阅读器的控制条可见，避免两条控制条叠在同一位置互相遮挡。
_OPEN_BARS: list["ReaderControlBar"] = []


def _drop_bar(bar: "ReaderControlBar") -> None:
    """把已失效的控制条清出注册表（destroyed 回调）。

    只做列表摘除，**不调用 bar 的任何方法**——destroyed 触发时其 C++
    对象正处于析构中途，调用方法是未定义行为。
    """
    if bar in _OPEN_BARS:
        _OPEN_BARS.remove(bar)


def register_bar(bar: "ReaderControlBar") -> None:
    """登记控制条（阅读器窗口首次显示时调用）。"""
    if bar not in _OPEN_BARS:
        _OPEN_BARS.append(bar)
        # 控制条的 C++ 对象随宿主窗口一起销毁（宿主被 GC / deleteLater），
        # 届时注册表里留着的就是僵尸包装器——下一次 parentWidget/setVisible
        # 会直接段错误（PySide 不保证抛 RuntimeError）。挂 destroyed 同步清理。
        try:
            bar.destroyed.connect(lambda *_: _drop_bar(bar))
        except RuntimeError:
            _drop_bar(bar)


def unregister_bar(bar: "ReaderControlBar") -> None:
    """注销控制条（阅读器窗口关闭时调用），并把最前让给还活着的读者。"""
    was_registered = bar in _OPEN_BARS
    _drop_bar(bar)
    try:
        bar.close()
    except RuntimeError:
        return  # C++ 对象已销毁（重复关闭）：无需再管
    if was_registered and _OPEN_BARS:
        candidate = _OPEN_BARS[-1]
        owner = candidate.parentWidget()
        if owner is not None and owner.isVisible():
            raise_bar(candidate)


def raise_bar(bar: "ReaderControlBar") -> None:
    """把 bar 提为最前：显示它，隐藏其他阅读器的控制条。"""
    if bar in _OPEN_BARS:
        _OPEN_BARS.remove(bar)
    _OPEN_BARS.append(bar)
    for other in _OPEN_BARS:
        owner = other.parentWidget()
        visible = other is bar and owner is not None and owner.isVisible()
        try:
            other.setVisible(visible)
        except RuntimeError:
            continue  # 已销毁的控制条：跳过


class ReaderControlBar(QWidget):
    """钉在屏幕底部中央的阅读器控制条（独立置顶小窗，不随图片窗口移动）。

    控件命名沿用旧底部工具条（objectName 均为 readerBottom / readerNavButton
    等），因此窗口侧的既有引用（window.next_btn 等）、QSS 与相关守卫测试
    都可以原样保留。
    """

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus,
        )
        # objectName 同时用于 QSS 与「按钮归属底部工具条」的守卫断言
        self.setObjectName("readerBottom")
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 关键：QWidget 子类的 QSS 背景**默认不绘制**（Qt 只在 QFrame 等
        # 自带 paintEvent 的类上画样式背景），必须开 WA_StyledBackground，
        # 否则整条控制条是透明的——只看得见按钮和标签自己的底色。
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._pinned_screen = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 6, 12, 6)
        root.setSpacing(2)

        info_layout = QHBoxLayout()
        info_layout.setSpacing(8)
        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(8)

        self.back_btn = QPushButton("← 返回列表")
        self.back_btn.setObjectName("readerNavButton")
        self.back_btn.setCursor(Qt.PointingHandCursor)
        self.back_btn.setFocusPolicy(Qt.NoFocus)
        info_layout.addWidget(self.back_btn)

        self.prev_btn = QPushButton("‹ 上一页")
        self.prev_btn.setObjectName("readerNavButton")
        self.prev_btn.setCursor(Qt.PointingHandCursor)
        self.prev_btn.setFocusPolicy(Qt.NoFocus)
        self.prev_btn.setMinimumWidth(82)
        nav_layout.addWidget(self.prev_btn)

        self.status_label = QLabel("准备中…")
        self.status_label.setObjectName("readerStatus")
        info_layout.addWidget(self.status_label)
        info_layout.addStretch()

        self.cache_label = QLabel("")
        self.cache_label.setObjectName("readerCache")

        self.page_label = QLabel("1 / ?")
        self.page_label.setObjectName("readerPage")
        self.page_label.setAlignment(Qt.AlignCenter)
        info_layout.addWidget(self.page_label)
        info_layout.addStretch()
        info_layout.addWidget(self.cache_label)

        self.play_btn = QPushButton("▶ 图片播放")
        self.play_btn.setObjectName("readerPlayButton")
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setFocusPolicy(Qt.NoFocus)
        self.play_btn.setFixedWidth(100)
        nav_layout.addStretch()
        nav_layout.addWidget(self.play_btn)
        nav_layout.addStretch()

        self.next_btn = QPushButton("下一页 ›")
        self.next_btn.setObjectName("readerNavButton")
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.setFocusPolicy(Qt.NoFocus)
        self.next_btn.setMinimumWidth(82)
        nav_layout.addWidget(self.next_btn)

        root.addLayout(info_layout)
        root.addLayout(nav_layout)

        # 标签透明：全局 `QWidget { background-color }` 规则会给每个标签画上
        # 深色底，浮在圆角卡片上就是一个个突兀的小方块（用户截图所见）。
        # 标签自身样式表优先级高于从窗口继承的规则。
        for label in (self.status_label, self.page_label, self.cache_label):
            label.setStyleSheet("background: transparent;")

    def paintEvent(self, event):
        """自绘圆角卡片底与描边。

        为什么不交给 QSS：`ReaderControlBar` 是**顶层 QWidget + 半透明背景**，
        QSS 的 `background-color` 在这种组合下不会绘制（实测整条透明、只剩
        按钮和标签自己的底色），即使设置 `WA_StyledBackground` 也无效。
        自绘跨平台稳定，也避免样式来源两处。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setBrush(_BAR_BG)
        painter.setPen(QPen(_BAR_BORDER, 1))
        painter.drawRoundedRect(rect, _BAR_RADIUS, _BAR_RADIUS)

    @property
    def pinned_screen(self):
        """控制条当前停靠的屏幕（换屏判定用）。"""
        return self._pinned_screen

    def pin_to(self, screen) -> None:
        """把控制条钉到指定屏幕可用区域的底部中央，并固定尺寸。

        宽度/高度一经固定就不再随内容变化——这是「按钮永不漂移」的
        结构保证：状态文字变长只会让 QLabel 省略显示，按钮位置纹丝不动。
        """
        if screen is None:
            return
        self._pinned_screen = screen
        self.layout().activate()
        self.adjustSize()
        available = screen.availableGeometry()
        width = max(self.sizeHint().width(), _MIN_WIDTH)
        width = min(width, max(240, available.width() - 32))
        height = self.sizeHint().height()
        x = available.left() + max(0, (available.width() - width) // 2)
        y = available.bottom() - height - _BOTTOM_MARGIN
        self.setFixedSize(width, height)
        self.move(x, y)
