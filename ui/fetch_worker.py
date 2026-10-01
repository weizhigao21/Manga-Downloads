"""后台抓取/搜索线程，避免阻塞 UI。

从 main_window 拆出，独立成模块：支持列表抓取与关键词搜索两种模式。
"""
from PySide6.QtCore import QThread, Signal

from src.wnacg_crawler import fetch_page, search_comics
from ui.threads import register


class FetchWorker(QThread):
    """后台抓取线程。

    - keyword 非空：按关键词搜索（search_comics）
    - 否则：抓取列表页第 page 页（fetch_page）

    在全局线程注册表登记，应用退出时统一收尾（见 ui/threads.py）。
    """

    # 业务完成信号。注意：不能命名为 finished——那会遮蔽 QThread 内置的
    # finished 信号，导致「线程完全停止后才触发」的清理时机丢失；
    # 清理（deleteLater）应连接原生 finished（见 main_window._launch_fetch）。
    done = Signal(list)
    error = Signal(str)

    def __init__(
        self, url: str | None = None, page: int = 1,
        keyword: str = "", category: str | None = None,
    ):
        super().__init__()
        self.url = url
        self.page = page
        self.keyword = keyword
        self.category = category

    def run(self):
        register(self)
        try:
            if self.keyword:
                comics = search_comics(self.keyword, self.page, self.category)
            else:
                comics = fetch_page(self.page, self.url)
            self.done.emit(comics)
        except Exception as exc:
            self.error.emit(str(exc))
