"""后台读取合集章节目录。"""

from PySide6.QtCore import QThread, Signal

from src.wnacg_crawler import G_URL, fetch, parse_collection_chapters
from ui.threads import register


class ChapterListWorker(QThread):
    done = Signal(list)
    error = Signal(str)

    def __init__(self, detail_url: str):
        super().__init__()
        self.detail_url = detail_url

    def run(self):
        register(self)
        try:
            url = self.detail_url
            if not url.startswith(("http://", "https://")):
                url = G_URL + "/" + url.lstrip("/")
            chapters = parse_collection_chapters(fetch(url))
            if not chapters:
                raise ValueError("详情页没有找到可阅读的章节")
            self.done.emit(chapters)
        except Exception as exc:
            self.error.emit(str(exc))
