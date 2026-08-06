"""后台抓取/搜索线程，避免阻塞 UI。

从 main_window 拆出，独立成模块：支持列表抓取与关键词搜索两种模式。
"""
from PySide6.QtCore import QThread, Signal

from src.wnacg_crawler import fetch_page, search_comics


class FetchWorker(QThread):
    """后台抓取线程。

    - keyword 非空：按关键词搜索（search_comics）
    - 否则：抓取列表页第 page 页（fetch_page）
    """

    finished = Signal(list)
    error = Signal(str)

    def __init__(self, url: str | None = None, page: int = 1, keyword: str = ""):
        super().__init__()
        self.url = url
        self.page = page
        self.keyword = keyword

    def run(self):
        try:
            if self.keyword:
                comics = search_comics(self.keyword, self.page)
            else:
                comics = fetch_page(self.page, self.url)
            self.finished.emit(comics)
        except Exception as exc:
            self.error.emit(str(exc))
