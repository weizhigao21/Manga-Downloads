"""数据库管理：漫画列表 SQLite 持久化与封面图片本地缓存。

仅含数据层（无 Qt UI 依赖），对话框见 ui/dialogs/database_dialog.py。
"""
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from src.utils import data_root, display_title_for_comic, safe_filename

ROOT_DIR = data_root()
DB_PATH = ROOT_DIR / "Config" / "comics.db"
COVERS_DIR = ROOT_DIR / "Config" / "covers"

AID_RE = re.compile(r"aid-(\d+)")


def _extract_aid(detail_url: str) -> int | None:
    """从详情链接提取作品 ID（aid），无法提取时返回 None。"""
    match = AID_RE.search(detail_url or "")
    return int(match.group(1)) if match else None


def cover_cache_path(detail_url: str) -> Path | None:
    """根据详情链接返回封面缓存文件路径（文件不存在则返回 None）。"""
    match = AID_RE.search(detail_url or "")
    if not match:
        return None
    path = COVERS_DIR / f"{match.group(1)}.jpg"
    return path if path.exists() else None


def _match_download_folder(
    base: Path,
    title: str,
    label: str,
    translated_title: str = "",
    aid: str = "",
) -> bool:
    """检查下载目录中是否存在该作品对应的文件夹。

    兼容多种命名（均同时检查文件名清洗前后的写法）：
    - 旧格式：仅标题 / `[标签] 标题`
    - 新格式：显示名（AI 翻译名或标题自带中文）/ `[标签] 显示名`
    - 冲突格式：上述名称后追加 ` (aid)`
    """
    display = display_title_for_comic(title, translated_title)
    candidates: set[str] = {title, display}
    for t in list(candidates):
        candidates.add(f"[{label}] {t}".strip() if label else t)
    if aid:
        for t in list(candidates):
            candidates.add(f"{t} ({aid})")
    for raw in candidates:
        safe = safe_filename(raw)
        if (base / raw).is_dir() or (base / safe).is_dir():
            return True
    return False


def is_comic_downloaded(record: dict) -> bool:
    """判断作品是否已下载：数据库标记或下载目录中存在对应文件夹。"""
    if record.get("downloaded"):
        return True
    title = record.get("title", "")
    if not title:
        return False
    from src.config import ProjectConfig

    base = Path(ProjectConfig().download_dir)
    return _match_download_folder(
        base,
        title,
        record.get("category_label") or "",
        record.get("translated_title") or "",
        _extract_aid(record.get("detail_url", "")) or "",
    )


_comics_records: list[dict] | None = None


def _invalidate_records_cache():
    """数据库记录变化时使缓存失效（save_comics / mark_downloaded / clear_comics 调用）。"""
    global _comics_records
    _comics_records = None


def _update_records_cache(records: list[dict]):
    """将新抓取的记录增量合并进缓存（按 detail_url upsert）。

    相比直接失效缓存（下次访问全表重查），翻页抓取时缓存持续累积，
    避免每次翻页都触发一次 SQLite 全表查询。
    """
    global _comics_records
    if _comics_records is None:
        # 缓存未初始化：直接作为初始数据（与数据库内容一致）
        _comics_records = records
        return
    merged = {r["detail_url"]: r for r in _comics_records}
    for rec in records:
        url = rec.get("detail_url", "")
        prev = merged.get(url)
        if prev is not None:
            # 保留 SQL 中 COALESCE 保住、但爬虫数据里没有的字段（翻译名/下载标记/完成时间）
            merged_rec = dict(rec)
            merged_rec.setdefault("translated_title", prev.get("translated_title", ""))
            merged_rec.setdefault("downloaded", prev.get("downloaded", 0))
            merged_rec.setdefault("downloaded_at", prev.get("downloaded_at", ""))
            merged[url] = merged_rec
        else:
            merged[url] = rec
    _comics_records = list(merged.values())


def _upsert_cached_record(record: dict):
    """按 detail_url 就地更新缓存中的记录；缓存中不存在时追加一条。

    用于翻译名/下载标记等单条更新：保证缓存与数据库一致，
    即使该记录不在当前缓存（如重启后从 tasks.json 恢复的任务）。
    """
    global _comics_records
    if _comics_records is None:
        return
    url = record.get("detail_url", "")
    for rec in _comics_records:
        if rec.get("detail_url") == url:
            rec.update(record)
            return
    _comics_records.append(record)


def _cached_records() -> list[dict]:
    """读取全部漫画记录并缓存，避免渲染每张卡片时重复全表查询。"""
    global _comics_records
    if _comics_records is None:
        _comics_records = DatabaseManager.instance().load_comics()
    return _comics_records


def is_downloaded(detail_url: str, title: str) -> bool:
    """按详情链接与标题判断作品是否已下载（数据库标记或下载目录文件夹）。"""
    for record in _cached_records():
        if record.get("detail_url") == detail_url:
            return is_comic_downloaded(record)
    return is_comic_downloaded({"downloaded": 0, "title": title})


def is_comics_downloaded(items: list[tuple[str, str, str, str]]) -> dict[str, bool]:
    """批量判断一批作品是否已下载，返回 {detail_url: bool}。

    Args:
        items: [(detail_url, title, translated_title, category_label), ...]，
            数据来自 ComicItem，避免此处依赖 ui 层类型。

    与逐个调用 is_downloaded 相比：只做一次已下载标记查询与
    每作品一次的目录检查，替代"每卡片遍历全表 + 重复 stat"。
    """
    from src.config import ProjectConfig

    base = Path(ProjectConfig().download_dir)
    downloaded_urls = {
        r["detail_url"] for r in _cached_records() if r.get("downloaded")
    }
    result: dict[str, bool] = {}
    for detail_url, title, translated_title, category_label in items:
        if not detail_url:
            result[detail_url] = False
            continue
        if detail_url in downloaded_urls:
            result[detail_url] = True
            continue
        result[detail_url] = _match_download_folder(
            base,
            title,
            category_label,
            translated_title,
            _extract_aid(detail_url) or "",
        )
    return result


class DatabaseManager:
    """SQLite 数据库管理器（单例），负责漫画列表的持久化读写。"""

    _instance: "DatabaseManager | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_db()
        return cls._instance

    @classmethod
    def instance(cls) -> "DatabaseManager":
        return cls()

    def _init_db(self):
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS comics (
                    detail_url TEXT PRIMARY KEY,
                    aid INTEGER,
                    title TEXT NOT NULL,
                    translated_title TEXT DEFAULT '',
                    date TEXT DEFAULT '',
                    image_count INTEGER DEFAULT 0,
                    cover_url TEXT DEFAULT '',
                    category TEXT DEFAULT '',
                    category_label TEXT DEFAULT '',
                    downloaded INTEGER DEFAULT 0,
                    downloaded_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
                )
                """
            )
            # 兼容旧库：补充 downloaded / downloaded_at / aid / translated_title 列，并对 aid 做一次性回填
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(comics)")}
            if "downloaded" not in cols:
                conn.execute(
                    "ALTER TABLE comics ADD COLUMN downloaded INTEGER DEFAULT 0"
                )
            if "downloaded_at" not in cols:
                conn.execute(
                    "ALTER TABLE comics ADD COLUMN downloaded_at TEXT DEFAULT ''"
                )
            if "aid" not in cols:
                conn.execute("ALTER TABLE comics ADD COLUMN aid INTEGER")
            if "translated_title" not in cols:
                conn.execute(
                    "ALTER TABLE comics ADD COLUMN translated_title TEXT DEFAULT ''"
                )
            conn.execute(
                """
                UPDATE comics SET aid = CAST(
                    substr(detail_url, instr(detail_url, 'aid-') + 4) AS INTEGER)
                WHERE aid IS NULL
                """
            )
            # 排序用 aid 索引，避免每次查询都做字符串切片 CAST
            conn.execute("CREATE INDEX IF NOT EXISTS idx_comics_aid ON comics(aid)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def save_comics(self, comics: list[dict]):
        """增量写入漫画记录（按 detail_url 去重 upsert，不清空已有数据）。

        翻页抓取不同页时，所有浏览过的作品都会累积保存到数据库。
        """
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO comics
                    (detail_url, aid, title, translated_title, date,
                     image_count, cover_url, category, category_label,
                     downloaded, downloaded_at)
                VALUES (?, ?, ?, COALESCE(
                    (SELECT translated_title FROM comics WHERE detail_url = ?), ''),
                    ?, ?, ?, ?, ?, COALESCE(
                    (SELECT downloaded FROM comics WHERE detail_url = ?), 0),
                    COALESCE(
                    (SELECT downloaded_at FROM comics WHERE detail_url = ?), ''))
                """,
                [
                    (
                        c.get("detail_url", ""),
                        _extract_aid(c.get("detail_url", "")),
                        c.get("title", ""),
                        c.get("detail_url", ""),
                        c.get("date", ""),
                        int(c.get("image_count") or 0),
                        c.get("cover_url", ""),
                        c.get("category", ""),
                        c.get("category_label", ""),
                        c.get("detail_url", ""),
                        c.get("detail_url", ""),
                    )
                    for c in comics
                ],
            )
        # 增量合并缓存：新抓取的记录直接并入（含 COALESCE 保留字段），
        # 不触发下次访问时的全表重查
        _update_records_cache(comics)

    def save_translated_title(self, detail_url: str, translated_title: str):
        """保存作品名称的翻译结果（不存在该记录时插入一条最小记录）。"""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE comics SET translated_title = ?, updated_at = datetime('now', 'localtime')
                WHERE detail_url = ?
                """,
                (translated_title, detail_url),
            )
            if conn.total_changes == 0:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO comics (detail_url, title, translated_title)
                    VALUES (?, '', ?)
                    """,
                    (detail_url, translated_title),
                )
        _upsert_cached_record(
            {"detail_url": detail_url, "translated_title": translated_title}
        )

    def mark_downloaded(self, detail_url: str):
        """将指定作品标记为已下载，并记录下载完成时间（downloaded_at）。"""
        downloaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                "UPDATE comics SET downloaded = 1, downloaded_at = ?, "
                "updated_at = datetime('now', 'localtime') WHERE detail_url = ?",
                (downloaded_at, detail_url),
            )
        _upsert_cached_record(
            {"detail_url": detail_url, "downloaded": 1, "downloaded_at": downloaded_at}
        )

    def load_comics(self, category: str | None = None, limit: int | None = None) -> list[dict]:
        """读取漫画记录，按作品 ID（aid）倒序（最新上传优先）。

        Args:
            category: 仅返回该分类（如 "cate-22"）的记录；None 返回全部。
            limit: 最多返回条数；None 返回全部。
        """
        sql = "SELECT * FROM comics"
        params: list[str] = []
        if category:
            sql += " WHERE category = ?"
            params.append(category)
        # aid 为 NULL（极端情况）的旧记录排最后，其余按 aid 倒序
        sql += " ORDER BY aid IS NULL, aid DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(str(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        """漫画记录数。"""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM comics").fetchone()
        return int(row["n"]) if row else 0

    def db_size(self) -> int:
        """数据库文件大小（字节）。"""
        return DB_PATH.stat().st_size if DB_PATH.exists() else 0

    def cover_count(self) -> int:
        """封面缓存文件数。"""
        return len(list(COVERS_DIR.glob("*.jpg")))

    def cover_size(self) -> int:
        """封面缓存总大小（字节）。"""
        return sum(p.stat().st_size for p in COVERS_DIR.glob("*.jpg"))

    def clear_comics(self):
        """清空漫画记录。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM comics")
        _invalidate_records_cache()

    def clear_covers(self):
        """清除封面缓存文件。"""
        for path in COVERS_DIR.glob("*.jpg"):
            path.unlink(missing_ok=True)


def format_size(size: int) -> str:
    """将字节数格式化为易读文本。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.2f} MB"
