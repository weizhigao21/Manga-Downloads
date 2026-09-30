"""阅读器本地缓存索引：定位作品目录、扫描已缓存图片、维护直链索引表。

设计要点（与下载目录合流）：
- 阅读缓存与「下载」写同一个目录（`Download/[分类标签] 显示名/`），
  因此看完的作品天然等同于下载完成，断点续传直接生效；
- 压缩包模式解压后目录结构与图片模式一致，无需区分来源即可阅读；
- 图片文件名不可控（取自阅读页 `<span class="name">`），因此**按自然
  排序扫描目录**，不依赖文件名语义。

本模块为纯 Python（仅标准库 + src 层），不导入任何 Qt，便于离线测试。
"""
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from src.downloader import _download_folder_name
from src.utils import safe_filename

logger = logging.getLogger(__name__)

# 视为漫画图片的扩展名（小写，含点）
IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".avif"}
)

# 阅读器元数据文件名（与 title_info.json 并列存放，用于断点续读）
READER_STATE_FILE = "reader_state.json"

# 直链签名有效期（保守取 12 小时；超期后重看会重新解析，避免 403/404）
URL_TTL = timedelta(hours=12)

_DIGITS_RE = re.compile(r"(\d+)")


def natural_sort_key(value) -> tuple:
    """自然排序键：`2.jpg` 排在 `10.jpg` 之前。

    将字符串按「连续数字段」切分，数字段转 int 参与比较，非数字段转小写
    字符串。返回元组可直接用于 sorted(key=...)。

    Examples:
        sorted(["10.jpg", "2.jpg"], key=natural_sort_key) -> ["2.jpg", "10.jpg"]
    """
    text = str(value)
    parts: list[tuple[int, object]] = []
    for segment in _DIGITS_RE.split(text):
        if not segment:
            continue
        if segment.isdigit():
            parts.append((0, int(segment)))
        else:
            parts.append((1, segment.lower()))
    return tuple(parts)


def page_number_from_name(name: str) -> int | None:
    """从文件名中提取页码（数字段），无法判定时返回 None。

    下载侧与压缩包内的图片名普遍以页码/序号命名（`1.jpg`、`0001.jpg`、
    `page_10.jpg`），因此**文件名里的数字即页码**。取**最后一个**数字段：
    前缀常含作品 ID 等噪声（如 `aid-99-3.jpg` 的第 3 页），页码总在末尾。

    Returns:
        解析出的页码（>0）；无数字段或数字为 0 时返回 None。
    """
    matches = _DIGITS_RE.findall(str(name))
    if not matches:
        return None
    try:
        number = int(matches[-1])
    except ValueError:  # 数字段过长/异常：视为无法判定
        return None
    return number if number > 0 else None


def index_local_images(folder: Path) -> dict[int, Path]:
    """扫描目录并建立 {页码: 文件路径} 映射。

    这是修复「图片顺序不对」的核心：**按文件名里的页码对齐**，而不是用
    排序下标当页码。若只缓存了第 1、2、3、10 页，下标方案会把第 10 页
    错当成第 4 页；页码映射则如实返回，缺口由上层联网补拉。

    冲突处理：同一页码出现多个文件时，保留自然排序靠前的那个（多为
    无后缀重复下载的历史产物）。

    Args:
        folder: 作品目录。

    Returns:
        {页码: 路径}；目录不存在或无法解析页码的文件会被忽略。
    """
    result: dict[int, Path] = {}
    for path in scan_local_images(folder):
        number = page_number_from_name(path.name)
        if number is None:
            continue
        result.setdefault(number, path)
    return result


def scan_local_images(folder: Path) -> list[Path]:
    """扫描目录下（含子目录）的图片文件，按自然排序返回。

    递归一层子目录是刻意为之：部分压缩包解压后会多出一层同名目录，
    若不递归会漏图。同时忽略隐藏文件与其他非图片文件。

    Args:
        folder: 作品目录。

    Returns:
        排序后的图片文件路径列表；目录不存在时返回空列表。
    """
    if not folder or not folder.is_dir():
        return []
    files = [
        p
        for p in folder.rglob("*")
        if p.is_file()
        and p.suffix.lower() in IMAGE_SUFFIXES
        and not p.name.startswith(".")
    ]
    files.sort(key=lambda p: natural_sort_key(p.relative_to(folder).as_posix()))
    return files


def count_local_images(folder: Path) -> int:
    """统计目录下已缓存的图片数量（供卡片进度圈计算）。"""
    return len(scan_local_images(folder))


def cache_progress(folder: Path, expected_total: int) -> tuple[int, int, int]:
    """计算缓存进度，返回 (已缓存, 期望总数, 百分比整数 0-100)。

    Args:
        folder: 作品目录。
        expected_total: 期望图片总数（通常来自 ComicItem.image_count）。
            为 0 或未知时，百分比按「有图片即视为未知进度」处理：
            有图返回 100（无法判断完整性，避免误报"部分缓存"），无图返回 0。

    Returns:
        (cached, total, percent)。cached 可能大于 total（站点计数与实际
        不符），此时 percent 夹取到 100。
    """
    cached = count_local_images(folder) if folder else 0
    total = int(expected_total or 0)
    if total <= 0:
        return cached, total, (100 if cached else 0)
    if cached <= 0:
        return cached, total, 0
    percent = min(100, int(cached * 100 / total))
    return cached, total, percent


def find_local_dir(comic: dict, base_dir: Path | None = None) -> Path | None:
    """定位作品在下载目录中的本地目录；不存在时返回 None。

    复用下载侧的命名规则（`_download_folder_name`）生成候选目录名，
    并兼容历史/冲突命名（追加 ` (aid)`），逐个检查是否存在且含图片。

    Args:
        comic: 作品数据字典，需含 title/detail_url，可选
            category_label / translated_title。
        base_dir: 下载根目录；None 时读取 Config.json 的 Download 配置。

    Returns:
        命中的作品目录（含图片）；无命中返回 None。
    """
    from src.config import ProjectConfig

    base = Path(base_dir) if base_dir else Path(ProjectConfig().download_dir)
    if not base.is_dir():
        return None

    aid = extract_aid(comic.get("detail_url", ""))

    # 候选目录名：带标签 / 不带标签。**带 (aid) 后缀的优先**——同名冲突时
    # 它是下载侧追加的作品 ID 目录，通常内容更完整（后下载的全量版本）。
    # 若先命中不带后缀的目录，可能拿到一份更早下载的部分缓存。
    candidates: list[str] = []
    for use_label in (True, False):
        name = _download_folder_name(comic, use_label)
        if aid:
            candidates.append(f"{name} ({aid})")
        candidates.append(name)

    seen: set[str] = set()
    for name in candidates:
        for variant in (name, safe_filename(name)):
            if variant in seen:
                continue
            seen.add(variant)
            path = base / variant
            if path.is_dir() and scan_local_images(path):
                return path

    # 兜底：目录名里含 aid 的（用户重命名/旧版命名差异）
    if aid:
        for path in base.iterdir():
            if path.is_dir() and f"({aid})" in path.name and scan_local_images(path):
                return path
    return None


def extract_aid(detail_url: str) -> str:
    """从详情/阅读链接提取作品 ID（aid）；失败返回空串。"""
    match = re.search(r"aid-(\d+)", detail_url or "")
    return match.group(1) if match else ""


def _resolve_reader_db_path() -> Path:
    """阅读器元数据数据库路径：与 comics.db 同目录（跟随自定义路径配置）。"""
    from ui.database import DB_PATH

    return Path(DB_PATH).with_name("reader.db")


class ReaderCacheIndex:
    """阅读器元数据存储（SQLite）：直链索引 + 阅读进度。

    独立于 comics.db，避免污染主库；文件与 comics.db 同目录，
    因此「更改存储路径」后自动跟随（每次实例化时重新解析路径）。

    两张表：
    - reader_pages(aid, page_no)：图片直链缓存（含抓取时间，用于 TTL 判断）
    - reader_state(aid)：上次阅读到的页码等断点信息
    """

    def __init__(self, db_path: Path | None = None):
        self._db_path = Path(db_path) if db_path else _resolve_reader_db_path()
        self._ensure_schema()

    # ---- 基础设施 ----

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reader_pages (
                    aid        TEXT NOT NULL,
                    page_no    INTEGER NOT NULL,
                    image_url  TEXT NOT NULL,
                    local_name TEXT DEFAULT '',
                    fetched_at TEXT DEFAULT (datetime('now', 'localtime')),
                    PRIMARY KEY (aid, page_no)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reader_state (
                    aid         TEXT PRIMARY KEY,
                    last_page   INTEGER DEFAULT 1,
                    total_pages INTEGER DEFAULT 0,
                    updated_at  TEXT DEFAULT (datetime('now', 'localtime'))
                )
                """
            )

    # ---- 直链索引 ----

    def load_page_urls(
        self, aid: str, ttl: timedelta = URL_TTL
    ) -> dict[int, tuple[str, str]]:
        """读取该作品的直链索引，跳过已过期的记录。

        Args:
            aid: 作品 ID。
            ttl: 有效期；None 表示不过滤过期（全部返回）。

        Returns:
            {页码: (图片直链, 真实页名)}。页名用于落盘时保持与下载侧一致，
            避免用页码顶替造成重复文件与顺序错位。
        """
        if not aid:
            return {}
        cutoff = None
        if ttl is not None:
            cutoff = (datetime.now() - ttl).strftime("%Y-%m-%d %H:%M:%S")
        sql = (
            "SELECT page_no, image_url, local_name, fetched_at "
            "FROM reader_pages WHERE aid = ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (aid,)).fetchall()
        result: dict[int, tuple[str, str]] = {}
        for row in rows:
            if cutoff and (row["fetched_at"] or "") < cutoff:
                continue
            page_no = int(row["page_no"])
            # 兼容旧记录：无页名时回退为页码字符串
            name = (row["local_name"] or "").strip() or str(page_no)
            result[page_no] = (row["image_url"], name)
        return result

    def save_page_urls(
        self,
        aid: str,
        pages: dict[int, tuple[str, str] | str],
        local_name: str = "",
    ) -> None:
        """写入/更新直链索引（按 aid+page_no upsert，刷新抓取时间）。

        Args:
            pages: {页码: (直链, 页名)}；也兼容 {页码: 直链} 的旧格式
                （此时页名回退为页码字符串）。
            local_name: 兼容参数，未使用时留空。
        """
        if not aid or not pages:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for page_no, value in pages.items():
            if isinstance(value, tuple):
                url, name = value[0], value[1]
            else:
                url, name = value, str(page_no)
            rows.append((aid, int(page_no), url, str(name or ""), now))
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO reader_pages (aid, page_no, image_url, local_name, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(aid, page_no) DO UPDATE SET
                    image_url = excluded.image_url,
                    local_name = excluded.local_name,
                    fetched_at = excluded.fetched_at
                """,
                rows,
            )

    # ---- 阅读进度（断点续读） ----

    def load_last_page(self, aid: str) -> int:
        """读取上次阅读到的页码（未见记录时返回 1）。"""
        if not aid:
            return 1
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_page FROM reader_state WHERE aid = ?", (aid,)
            ).fetchone()
        return int(row["last_page"]) if row else 1

    def save_last_page(self, aid: str, page: int, total_pages: int = 0) -> None:
        """记录阅读进度（供下次打开时续读）。"""
        if not aid:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reader_state (aid, last_page, total_pages, updated_at)
                VALUES (?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(aid) DO UPDATE SET
                    last_page = excluded.last_page,
                    total_pages = excluded.total_pages,
                    updated_at = excluded.updated_at
                """,
                (aid, int(page), int(total_pages or 0)),
            )
