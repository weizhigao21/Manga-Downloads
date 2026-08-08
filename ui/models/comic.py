from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ui.settings import AppSettings


@dataclass
class ComicItem:
    """漫画列表项数据模型。"""

    title: str
    image_count: int
    upload_date: str
    cover_path: str | Path | None = None
    cover_url: str = ""
    detail_url: str = ""
    category: str = ""
    category_label: str = ""
    translated_title: str = ""

    def __post_init__(self):
        self._settings = AppSettings()

    @property
    def display_title(self) -> str:
        """卡片显示名称：已翻译时优先显示翻译结果，否则显示原标题（繁简转换）。"""
        if self.translated_title:
            return self.translated_title
        return self._settings.convert(self.title)

    @property
    def display_category_label(self) -> str:
        return self._settings.convert(self.category_label)

    @property
    def formatted_date(self) -> str:
        """尝试标准化日期显示。"""
        try:
            dt = datetime.strptime(self.upload_date, "%Y-%m-%d")
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return self.upload_date or "未知"

    @property
    def count_text(self) -> str:
        return f"{self.image_count} 张"
