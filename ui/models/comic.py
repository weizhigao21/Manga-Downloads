from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.utils import clean_title_markup, find_existing_translation, split_title_segments
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
    favorite: bool = False
    chapter_count: int = 0
    similar_count: int = 0  # 本地库中相似记录数（>0 时卡片显示 ≈N 角标）
    similar_downloaded: bool = False  # 相似记录中是否存在已下载（角标变红提示）

    def __post_init__(self):
        self.title = clean_title_markup(self.title)
        self._settings = AppSettings()

    @property
    def display_title(self) -> str:
        """卡片显示名称，按以下优先级取用：

        1. AI 翻译结果（translated_title，已持久化）；
        2. 标题自带中文翻译（`日文名｜中文名`）或本身就是中文时，取中文部分；
        3. 回落原标题（繁简转换）。

        方括号块（社团/作者/标签）始终原样保留，仅对主体文本做识别。
        """
        if self.translated_title:
            return self.translated_title
        parts: list[str] = []
        for text, translatable in split_title_segments(self.title):
            if not translatable or not text.strip():
                parts.append(text)
                continue
            # 保留主体片段原有首尾空白，仅替换中间内容
            leading = text[: len(text) - len(text.lstrip())]
            trailing = text[len(text.rstrip()):]
            existing = find_existing_translation(text)
            core = (
                self._settings.convert(existing)
                if existing is not None
                else self._settings.convert(text.strip())
            )
            parts.append(f"{leading}{core}{trailing}")
        return "".join(parts)

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
        return f"{self.image_count} 张" if self.image_count > 0 else "未知"

    @property
    def count_label_text(self) -> str:
        if self.chapter_count > 0:
            return f"章节: {self.chapter_count} 话"
        return f"图片: {self.count_text}"
