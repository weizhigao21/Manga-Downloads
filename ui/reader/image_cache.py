"""阅读器图片内存缓存：解码结果的**有界滑动窗口**。

为什么需要它
------------
纯靠磁盘缓存时，每翻一页都要 `QPixmap(path)` 从磁盘重新解码一张扫描图
（2000x3000 的 JPEG 约几十到上百毫秒），来回翻看会反复付这笔开销；反过来，
把整部作品的解码结果都留在内存里，200 页就是 4~5 GB——内存随阅读时长
持续增长。

折中方案是「窗口缓存」：
- 窗口 = ``[当前页 - behind, 当前页 + ahead]``，默认「当前页 + 后两页」共 3 页；
- 窗口内的页保存**已解码**的图片，翻到该页时直接显示，不再读盘、不再联网；
- 窗口随当前页滑动，离开窗口的页立即从字典里摘掉——没有其他引用时
  QPixmap 占用的内存随之回收（正在显示的那张会被图片控件自己持有，不会
  在屏幕上"消失"）；
- 迟到的结果（页号已滑出窗口，比如快速连翻后预取才回来）**直接拒收**，
  避免预取把内存顶起来。

线程约定
--------
本类只在 GUI 线程读写（调用方是 ``ReaderWindow``）。后台线程只负责把文件
**解码成 QImage**（见 ``decode_image``），经信号交回主线程后再转成 QPixmap
存进来——QPixmap 不能在非 GUI 线程创建。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtGui import QImage, QImageReader, QPixmap


def decode_image(path) -> QImage | None:
    """把本地图片解码成 QImage（**可在后台线程调用**，纯光栅操作）。

    与 ``QPixmap(path)`` 保持一致的解码行为：Qt 的 QPixmap 文件构造器内部
    就是 ``QImageReader`` + ``setAutoTransform(True)``（按 EXIF 方向摆正），
    这里显式用同样的参数，避免"预取来的图"与"现读的图"方向/色彩不一致。

    失败（文件缺失、损坏、格式不支持）返回 None，由调用方决定降级策略。
    """
    if not path:
        return None
    try:
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        image = reader.read()
    except Exception:  # noqa: BLE001 - 解码失败只影响本次预取，不应影响阅读
        return None
    return None if image.isNull() else image


@dataclass(frozen=True)
class CachedPage:
    """一页缓存内容：解码后的位图 + 来源文件路径（拖出图片要用原始路径）。"""

    pixmap: QPixmap
    path: str = ""


@dataclass
class PageImageCache:
    """按页号保存解码图的有界窗口缓存。

    典型用法（全部在 GUI 线程）：

        cache = PageImageCache(ahead=2, behind=0)
        cache.slide_to(5)              # 窗口变为 [5, 7]，淘汰窗口外的页
        cache.put(6, pixmap, path)     # 预取结果入窗（窗外自动拒收）
        cache.get(6)                   # 翻到第 6 页时直接取用
    """

    ahead: int = 2
    behind: int = 0
    # {页码: 缓存条目}；用 field 是为了让实例各自持有（默认值不能共享）
    _items: dict[int, CachedPage] = field(default_factory=dict, repr=False)
    _center: int = 1

    def __post_init__(self):
        self.ahead = max(0, int(self.ahead))
        self.behind = max(0, int(self.behind))

    # ---- 窗口 ----

    def configure(self, ahead: int, behind: int) -> None:
        """更新窗口大小（设置变更后调用）；下一次 ``slide_to`` 立即按新窗口收缩。"""
        self.ahead = max(0, int(ahead))
        self.behind = max(0, int(behind))

    @property
    def window_size(self) -> int:
        """窗口最多保存几页（含当前页）。"""
        return self.ahead + self.behind + 1

    @property
    def window(self) -> tuple[int, int]:
        """当前窗口的闭区间 (lo, hi)。"""
        return (self._center - self.behind, self._center + self.ahead)

    def in_window(self, page_no: int) -> bool:
        lo, hi = self.window
        return lo <= int(page_no) <= hi

    def slide_to(self, page_no: int) -> list[int]:
        """把窗口滑到以 page_no 为中心，淘汰窗口外的页；返回被淘汰的页码。"""
        self._center = int(page_no)
        lo, hi = self.window
        evicted = [no for no in self._items if not (lo <= no <= hi)]
        for no in evicted:
            self._items.pop(no, None)
        return evicted

    # ---- 读写 ----

    def get(self, page_no: int) -> CachedPage | None:
        return self._items.get(int(page_no))

    def put(self, page_no: int, pixmap: QPixmap | None, path="") -> bool:
        """存入一页的解码结果；返回是否真的存下了。

        - 空图（解码失败）不缓存，避免"坏页"把错误固化下来；
        - **窗口外的页直接拒收**：迟到的预取结果不该再占内存。
        """
        no = int(page_no)
        if pixmap is None or pixmap.isNull():
            return False
        if not self.in_window(no):
            return False
        self._items[no] = CachedPage(pixmap=pixmap, path=str(path or ""))
        self._enforce_cap()
        return True

    def clear(self) -> None:
        self._items.clear()

    def pages(self) -> list[int]:
        """当前缓存的页码（升序），供测试与调试。"""
        return sorted(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, page_no) -> bool:
        return int(page_no) in self._items

    # ---- 内部 ----

    def _enforce_cap(self) -> None:
        """硬上限兜底：任何情况下不超过窗口大小，优先淘汰离中心页最远的。"""
        while len(self._items) > self.window_size:
            victim = max(
                self._items,
                key=lambda no: (abs(no - self._center), no),
            )
            self._items.pop(victim, None)
