"""漫画网格视图：承载卡片并负责响应式布局。

从 main_window 拆出，独立成模块：负责根据可视宽度动态计算列数、
渲染 ComicCard，以及无数据时的空状态展示。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QScrollArea, QWidget

from ui.models import ComicItem
from ui.widgets.comic_card import ComicCard


class ComicsGrid(QScrollArea):
    """漫画卡片网格视图。"""

    # 每列最小宽度（px），窗口变宽时列数自动增加
    MIN_COLUMN_WIDTH = ComicCard.CARD_WIDTH + 20

    # 卡片右键「查看类似作品」转发到主窗口
    similar_requested = Signal(object)
    # 卡片「阅读」转发到主窗口（打开阅读器窗口）
    read_requested = Signal(object)
    favorite_changed = Signal(object, bool)
    author_requested = Signal(str)

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
        from ui.reader.cache_index import cache_progress, find_local_dir

        downloaded_map = is_comics_downloaded(
            [
                (c.detail_url, c.title, c.translated_title, c.display_category_label)
                for c in comics
            ]
        )

        # 批量预计算缓存进度（每作品一次目录扫描，替代卡片各自扫盘）
        cache_map: dict[str, tuple[int, int, int]] = {}
        for comic in comics:
            folder = find_local_dir(
                {
                    "title": comic.title,
                    "detail_url": comic.detail_url,
                    "category_label": comic.display_category_label,
                    "original_category_label": comic.category_label,
                    "translated_title": comic.translated_title,
                }
            )
            progress = cache_progress(folder, comic.image_count)
            cache_map[comic.detail_url] = progress
            if progress[0] > 0:
                downloaded_map[comic.detail_url] = True

        # 按 detail_url diff：复用相同作品卡片，仅重建新增与移除的
        old = {c.comic.detail_url: c for c in self._cards}
        new_cards: list[ComicCard] = []
        for comic in comics:
            card = old.pop(comic.detail_url, None)
            if card is None:
                card = ComicCard(
                    comic, parent=self._content,
                    downloaded=downloaded_map.get(comic.detail_url),
                )
                # 转发右键「查看类似」与「阅读」信号（仅新建卡片连接）
                card.similar_requested.connect(self.similar_requested)
                card.read_requested.connect(self.read_requested)
                card.favorite_changed.connect(self.favorite_changed)
                card.author_requested.connect(self.author_requested)
            else:
                card.update_comic(
                    comic, downloaded=downloaded_map.get(comic.detail_url)
                )
            cached, total, percent = cache_map.get(comic.detail_url, (0, 0, 0))
            card.set_cache_progress(percent, cached, total)
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

    def update_cache_progress(self, detail_url: str, cached: int, total: int):
        """阅读器缓存新图片时更新对应卡片的按钮进度（进度条 + 百分比）。"""
        percent = min(100, int(cached * 100 / total)) if total > 0 else 0
        for card in self._cards:
            if card.comic.detail_url == detail_url:
                # 不再把「缓存了哪怕一张」当成已下载：未满 100% 时按钮显示
                # 进度条，满 100% 时由卡片自行转成绿色「已下载」。
                card.set_cache_progress(percent, cached, total)
                break

    def refresh_comic_status(self, detail_url: str):
        """阅读窗口关闭后，重新核对该作品的本地图片与下载标记。"""
        for card in self._cards:
            comic = card.comic
            if comic.detail_url != detail_url:
                continue

            from ui.database import is_comics_downloaded
            from ui.reader.cache_index import cache_progress, find_local_dir

            folder = find_local_dir(
                {
                    "title": comic.title,
                    "detail_url": comic.detail_url,
                    "category_label": comic.display_category_label,
                    "original_category_label": comic.category_label,
                    "translated_title": comic.translated_title,
                }
            )
            cached, total, percent = cache_progress(folder, comic.image_count)
            downloaded = is_comics_downloaded(
                [(
                    comic.detail_url, comic.title,
                    comic.translated_title, comic.display_category_label,
                )]
            ).get(detail_url, False)
            # 只把数据库/目录认定的「已下载」标为完成；未满 100% 的
            # 缓存进度交由按钮显示为进度条（见 set_cache_progress）。
            card.set_downloaded_state(downloaded)
            card.set_cache_progress(percent, cached, total)
            break

    def reflow(self):
        """窗口大小变化后按需重排：列数未变时不重建任何控件。"""
        if not self._cards:
            return
        self._reflow()

    def width_for_columns(self, columns: int) -> int:
        """容纳指定列数所需的滚动区宽度，预留垂直滚动条。"""
        columns = max(1, int(columns))
        margins = self._grid.contentsMargins()
        spacing = self._grid.horizontalSpacing()
        return (
            columns * ComicCard.CARD_WIDTH
            + (columns - 1) * spacing
            + margins.left()
            + margins.right()
            + 2 * self.frameWidth()
            + self.verticalScrollBar().sizeHint().width()
        )

    def apply_similar_map(self, similar_map: dict[str, list]):
        """后台相似扫描完成后批量更新卡片左下角 ≈N 角标（不重建卡片）。

        Args:
            similar_map: {detail_url: [相似记录, ...]}，未出现的作品角标清零。
                相似记录含 downloaded 字段：其中存在已下载时角标变红提示。
        """
        for card in self._cards:
            url = card.comic.detail_url
            matches = similar_map.get(url)
            has_downloaded = bool(
                matches and any(r.get("downloaded") for r in matches)
            )
            card.set_similar_count(len(matches) if matches else 0, has_downloaded)

    def _reflow(self):
        """按当前可视宽度重排已有卡片。

        列数变化时仅移动卡片在网格中的位置（不重建控件、不重载封面、
        不查数据库），避免窗口缩放时反复销毁/重建大量卡片导致卡顿。
        """
        margins = self._grid.contentsMargins()
        spacing = self._grid.horizontalSpacing()
        usable = self.viewport().width() - margins.left() - margins.right()
        columns = max(1, (usable + spacing) // (ComicCard.CARD_WIDTH + spacing))
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
