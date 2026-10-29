"""数据库管理：漫画列表 SQLite 持久化、封面图片本地缓存与重复记录查重。

仅含数据层（无 Qt UI 依赖），对话框见 ui/dialogs/database_dialog.py。
"""
import difflib
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from src.utils import (
    BRACKET_RE,
    clean_title_markup,
    data_root,
    display_title_for_comic,
    safe_filename,
)

ROOT_DIR = data_root()

# 默认存储位置（未自定义路径时使用）
DEFAULT_DB_PATH = ROOT_DIR / "Config" / "comics.db"
DEFAULT_COVERS_DIR = ROOT_DIR / "Config" / "covers"

# 数据库文件与封面缓存目录（模块变量，可按配置切换，见 switch_storage_paths）
DB_PATH = DEFAULT_DB_PATH
COVERS_DIR = DEFAULT_COVERS_DIR

AID_RE = re.compile(r"aid-(\d+)")


def _read_paths_config() -> tuple[str, str]:
    """读取存储路径配置（空字符串表示使用默认位置）。"""
    from ui.settings import AppSettings

    settings = AppSettings()
    return settings.db_path, settings.covers_dir


def _write_paths_config(db_path: str, covers_dir: str):
    """将存储路径写入配置（settings.json），空字符串表示恢复默认位置。"""
    from ui.settings import AppSettings

    settings = AppSettings()
    settings.db_path = db_path
    settings.covers_dir = covers_dir
    settings.save()


def apply_storage_paths():
    """按配置更新数据库与封面缓存路径（模块导入时调用一次）。

    外部（如测试）可通过直接赋值 DB_PATH / COVERS_DIR 覆盖。
    """
    global DB_PATH, COVERS_DIR
    db_path, covers_dir = _read_paths_config()
    DB_PATH = (
        Path(db_path).expanduser()
        if db_path
        else DEFAULT_DB_PATH
    )
    COVERS_DIR = (
        Path(covers_dir).expanduser()
        if covers_dir
        else DEFAULT_COVERS_DIR
    )


def _migrate_storage(new_db: Path, new_covers: Path):
    """将现有数据库与封面缓存复制到新路径（目标已存在则不覆盖，保留旧文件）。

    仅迁移内容文件；迁移完成后旧路径文件保留，由用户决定是否手动清理。
    """
    for suffix in ("", "-wal", "-shm"):
        src = Path(f"{DB_PATH}{suffix}")
        if not src.exists() or src.stat().st_size == 0:
            continue
        dst = Path(f"{new_db}{suffix}")
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    if COVERS_DIR.exists():
        new_covers.mkdir(parents=True, exist_ok=True)
        for src in COVERS_DIR.glob("*.jpg"):
            dst = new_covers / src.name
            if not dst.exists():
                shutil.copy2(src, dst)


def switch_storage_paths(
    db_path: str | Path,
    covers_dir: str | Path,
    migrate: bool = True,
) -> None:
    """切换数据库文件与封面缓存目录，写入配置并立即生效。

    Args:
        db_path: 新的数据库文件路径（如 "D:/data/comics.db"）。
        covers_dir: 新的封面缓存目录。
        migrate: 是否将现有数据库与封面缓存复制到新路径。
            新路径已存在同名文件时不覆盖；旧路径文件保留。
    """
    new_db = Path(db_path).expanduser()
    new_covers = Path(covers_dir).expanduser()
    # 目标数据库已存在但非空且不是 SQLite 文件时拒绝切换（避免后续打开报错）
    if new_db.exists() and not new_db.is_dir() and new_db.stat().st_size > 0:
        with open(new_db, "rb") as f:
            if f.read(16) != _SQLITE_MAGIC:
                raise ValueError(
                    f"目标数据库文件已存在但不是有效的 SQLite 数据库：{new_db}"
                )
    if migrate:
        _migrate_storage(new_db, new_covers)
    _write_paths_config(str(new_db), str(new_covers))
    global DB_PATH, COVERS_DIR
    DB_PATH = new_db
    COVERS_DIR = new_covers
    # 重建单例：在新路径初始化表结构并刷新记录缓存
    DatabaseManager._instance = None
    _invalidate_records_cache()
    DatabaseManager.instance()


# 启动时按配置应用自定义路径（默认位置时保持默认值）
apply_storage_paths()

# 封面查重阈值：64 位均值感知哈希（aHash）的汉明距离上限，越小越严格。
# 实测同一封面重复上传距离为 0（同图重传/不同压缩通常 <= 5），而不同作品
# 封面因色调相似可能低至 9-12，阈值 10 会误判，故收紧到 6。
COVER_HASH_THRESHOLD = 6

# SQLite 文件头魔数，用于校验目标数据库文件
_SQLITE_MAGIC = b"SQLite format 3\x00"

# 标题方括号中常见的非作者块（标签/格式等），提取作者时排除
_NON_AUTHOR_BRACKETS = frozenset(
    {
        "漢化", "汉化", "中文", "日文", "英文", "中文版", "日文版", "英文版",
        "繁體", "简体", "簡體", "無修正", "无修正", "修正", "DL版", "DL",
        "AI", "AI圖集", "AI畫集", "AI绘图", "AI繪圖", "AI绘画",
        "CG", "CG畫集", "差分", "高解像度",
        "全年齢", "成人向け", "例のアレ", "彩頁", "全彩", "單行本", "雜誌",
        "短篇", "連載", "完結", "上卷", "下卷", "第一卷", "第1話",
    }
)

# 展会/活动代号（如 C97 / FF34 / K20），非作者，提取作者时排除
_EXPO_CODE_RE = re.compile(r"^[A-Za-z]{1,4}\s?\d{2,4}$")

# 「作者 - 作品名」格式的分隔符：短横/全角连字符/破折号。
# 要求**至少一侧有空白**（`A - B`、`A- B`、`A -B` 都算），避免把
# `1-12`、`AWE-SOME`、`邂逅-KAIKOU` 这类两侧贴字的连字符误判为分隔。
_AUTHOR_DASH_RE = re.compile(r"[-–—−－](?=\s)|(?<=\s)[-–—−－]")

# 各类括号的配对关系：提取破折号作者时用于判断分隔符是否落在括号内
# （`(2022 - 2025)`、`「A - B」` 内的破折号不是作者分隔）
_BRACKET_PAIRS = {"[": "]", "【": "】", "（": "）", "(": ")", "「": "」", "『": "』"}


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


def _inside_brackets(text: str, pos: int) -> bool:
    """判断 text[pos] 之前是否存在未闭合的括号（即 pos 落在括号内）。"""
    stack: list[str] = []
    for ch in text[:pos]:
        if ch in _BRACKET_PAIRS:
            stack.append(_BRACKET_PAIRS[ch])
        elif stack and stack[-1] == ch:
            stack.pop()
    return bool(stack)


def _author_from_dash_title(title: str) -> str:
    """从「作者 - 作品名」格式的标题中提取破折号前的作者名。

    站点上不少作品没有 `[社团/作者]` 方括号，而是写成
    `Umeko J - Yoruichi Cat Form「Bleach」`。此函数只在方括号路径
    提取不到作者时兜底：

    - 仅把**至少一侧有空白**的短横/破折号（`-` `–` `—` `－`）当分隔符，
      因此 `1-12`、`AWE-SOME-Ryan` 这类两侧贴字的连字符不会被切开；
    - 括号内的破折号跳过（`(2022 - 2025)` 是年份区间，不是作者分隔）；
    - 破折号前必须是合理的名字长度与词数，且不能是标签/展会代号，
      破折号后必须有作品名（标题以 `-` 开头时前段为空，直接放弃）。

    Returns:
        作者名；不符合上述格式时返回空串。
    """
    body = clean_title_markup(title)
    for match in _AUTHOR_DASH_RE.finditer(body):
        if _inside_brackets(body, match.start()):
            continue
        head = body[: match.start()].strip()
        tail = body[match.end():].strip()
        if not head or not tail:
            continue
        # 名字长度与词数：正常作者/社团名 2-40 字、最多 5 个词
        if not 2 <= len(head) <= 40 or len(head.split()) > 5:
            continue
        # 纯数字/日期/序号（如 "8" "2022.05"）不是作者
        if re.fullmatch(r"[\d\s.,/·:：%\-]+", head):
            continue
        # 已有翻译分隔（`日文｜中文`）的段落不是作者名
        if "｜" in head or "|" in head:
            continue
        # 带方括号的段落是标签/作者块本身，不是破折号前的作者名
        if any(ch in head for ch in "[]【】"):
            continue
        if head in _NON_AUTHOR_BRACKETS or _EXPO_CODE_RE.fullmatch(head):
            continue
        return head
    return ""


def extract_author(title: str) -> str:
    """从标题中提取作者/社团名。

    wnacg 标题惯例为 `[社团/作者] 作品名`（其后可能还有标签/展会等
    方括号块）。策略：收集全部方括号块，过滤掉常见标签/格式与展会
    代号，取第一个剩余块作为作者。

    没有方括号时，兼容 `作者 - 作品名` 格式（见 `_author_from_dash_title`）。

    Returns:
        作者/社团名（已去除方括号）；无法识别时返回空串。
    """
    for block in BRACKET_RE.findall(clean_title_markup(title)):
        name = block[1:-1].strip()
        if not name or name in _NON_AUTHOR_BRACKETS:
            continue
        if _EXPO_CODE_RE.fullmatch(name):
            continue
        return name
    return _author_from_dash_title(title)


def normalize_for_dedup(text: str) -> str:
    """查重归一化：去方括号块、去空白/标点/符号、繁转简、统一小写。

    仅用于比较，不修改数据库中的原始标题。繁简转换复用 AppSettings
    （跟随用户"繁转简"开关，与界面显示保持一致）。
    """
    if not text:
        return ""
    text = BRACKET_RE.sub("", text)
    from ui.settings import AppSettings

    text = AppSettings().convert(text)
    return re.sub(r"\W+", "", text).lower()


def _body_similar(a: str, b: str) -> bool:
    """判断两个归一化后的主体名称是否相似（用于作者信号补充判定）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    # 一方包含另一方（如「作品名」与「作品名 後編」的归一化结果）
    if len(a) >= 3 and len(b) >= 3 and (a in b or b in a):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _image_ahash(path: Path) -> str | None:
    """计算图片的均值感知哈希（aHash：8x8 灰度，64 位）。

    同一封面被不同尺寸/压缩率重新上传时哈希高度相近；无法解析或
    Pillow 缺失时返回 None。
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            img = img.convert("L").resize((8, 8), Image.LANCZOS)
            pixels = list(img.getdata())
    except Exception:
        return None
    avg = sum(pixels) / len(pixels)
    return "".join("1" if p >= avg else "0" for p in pixels)


def hamming_distance(a: str, b: str) -> int:
    """两个等长 0/1 字符串的汉明距离。"""
    return sum(x != y for x, y in zip(a, b, strict=True))


def _collect_cover_hashes(
    records: list[dict], progress_callback=None
) -> dict[str, str]:
    """批量加载全部记录的封面哈希：优先读缓存表，缺失的封面文件计算并写回。

    相比逐条查询，只开一次数据库连接，避免记录量大时的重复连接开销。

    Returns:
        {aid(str): ahash}，仅含封面存在且可解析的记录。
    """
    aids = [str(r.get("aid")) for r in records if r.get("aid")]
    result: dict[str, str] = {}
    if not aids:
        return result
    total = len(aids)
    placeholders = ",".join("?" * total)
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            f"SELECT aid, ahash FROM cover_hashes WHERE aid IN ({placeholders})",
            aids,
        ).fetchall()
    finally:
        conn.close()
    cached = {str(row[0]): row[1] for row in rows}

    computed: dict[str, str] = {}
    done = 0
    for aid in aids:
        if aid in cached:
            result[aid] = cached[aid]
        else:
            path = COVERS_DIR / f"{aid}.jpg"
            hash_value = _image_ahash(path) if path.exists() else None
            if hash_value:
                result[aid] = hash_value
                computed[aid] = hash_value
        done += 1
        if progress_callback:
            progress_callback(done, total)
    if computed:
        with sqlite3.connect(DB_PATH) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO cover_hashes (aid, ahash) VALUES (?, ?)",
                [(aid, h) for aid, h in computed.items()],
            )
    return result


def _cover_hash(aid: str | int | None) -> str | None:
    """返回作品封面的感知哈希：优先读缓存表，未命中时计算并写入。

    封面文件缺失或无法解析时返回 None。缓存避免重复查重时重算封面。
    """
    if not aid:
        return None
    DatabaseManager.instance()  # 确保表结构就绪（单例幂等）
    aid = str(aid)
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT ahash FROM cover_hashes WHERE aid = ?", (aid,)
        ).fetchone()
    finally:
        conn.close()
    if row:
        return row[0]
    path = COVERS_DIR / f"{aid}.jpg"
    ahash = _image_ahash(path) if path.exists() else None
    if ahash:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cover_hashes (aid, ahash) VALUES (?, ?)",
                (aid, ahash),
            )
    return ahash


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
    global _comics_records, _similar_index_cache, _similar_index_state
    _comics_records = None
    # 相似查询索引依赖记录缓存：一并失效，避免对象 id 复用导致旧索引残留
    _similar_index_cache = None
    _similar_index_state = None


def _update_records_cache(records: list[dict]):
    """将新抓取的记录增量合并进缓存（按 detail_url upsert）。

    相比直接失效缓存（下次访问全表重查），翻页抓取时缓存持续累积，
    避免每次翻页都触发一次 SQLite 全表查询。
    """
    global _comics_records, _similar_index_cache, _similar_index_state
    # 记录发生变化：相似查询索引随之失效（下次查询时按新数据重建）
    _similar_index_cache = None
    _similar_index_state = None
    if _comics_records is None:
        # 缓存未初始化：从 SQL 全表读取，保证缓存与数据库一致
        # （save_translated_title / mark_downloaded 等可能在库中留有记录）
        with DatabaseManager.instance()._connect() as conn:
            db_rows = conn.execute("SELECT * FROM comics").fetchall()
        db_records = {r["detail_url"]: dict(r) for r in db_rows}
        _comics_records = []
        for rec in records:
            url = rec.get("detail_url", "")
            prev = db_records.get(url)
            merged = dict(rec)
            merged.setdefault("aid", _extract_aid(url))
            merged.setdefault(
                "translated_title", prev["translated_title"] if prev else ""
            )
            merged.setdefault("downloaded", prev["downloaded"] if prev else 0)
            merged.setdefault("downloaded_at", prev["downloaded_at"] if prev else "")
            merged.setdefault("favorite", prev["favorite"] if prev else 0)
            _merge_count_fields(merged, prev)
            _comics_records.append(merged)
        # SQL 中其余记录（本次未抓取的，如仅翻译过的最小记录）一并纳入
        known = {r["detail_url"] for r in _comics_records}
        for url, rec in db_records.items():
            if url not in known:
                _comics_records.append(dict(rec))
        return
    # 增量合并：缓存中已存在的直接保留字段；新出现的从 SQL 读取保留字段
    merged = {r["detail_url"]: r for r in _comics_records}
    missing_urls = [
        rec.get("detail_url", "")
        for rec in records
        if rec.get("detail_url") and rec.get("detail_url") not in merged
    ]
    db_records: dict[str, dict] = {}
    if missing_urls:
        with DatabaseManager.instance()._connect() as conn:
            placeholders = ",".join("?" * len(missing_urls))
            rows = conn.execute(
                f"SELECT * FROM comics WHERE detail_url IN ({placeholders})",
                missing_urls,
            ).fetchall()
        db_records = {r["detail_url"]: dict(r) for r in rows}
    for rec in records:
        url = rec.get("detail_url", "")
        prev = merged.get(url) or db_records.get(url)
        # 统一补齐 SQL 保留、但爬虫数据里没有的字段（翻译名/下载标记/完成时间）
        merged_rec = dict(rec)
        merged_rec.setdefault("aid", _extract_aid(url))
        merged_rec.setdefault(
            "translated_title", prev.get("translated_title", "") if prev else ""
        )
        merged_rec.setdefault("downloaded", prev.get("downloaded", 0) if prev else 0)
        merged_rec.setdefault(
            "downloaded_at", prev.get("downloaded_at", "") if prev else ""
        )
        merged_rec.setdefault("favorite", prev.get("favorite", 0) if prev else 0)
        _merge_count_fields(merged_rec, prev)
        merged[url] = merged_rec
    _comics_records = list(merged.values())


def _merge_count_fields(record: dict, previous: dict | None):
    """未知数量不覆盖已知值；合集话数与单作品图片数互斥。"""
    images = int(record.get("image_count") or 0)
    chapters = int(record.get("chapter_count") or 0)
    if images > 0:
        record["chapter_count"] = 0
    elif chapters > 0:
        record["image_count"] = 0
    else:
        record["image_count"] = int(previous.get("image_count") or 0) if previous else 0
        record["chapter_count"] = int(previous.get("chapter_count") or 0) if previous else 0


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
    merged = dict(record)
    merged.setdefault("aid", _extract_aid(url))
    merged.setdefault("translated_title", "")
    merged.setdefault("downloaded", 0)
    merged.setdefault("downloaded_at", "")
    merged.setdefault("favorite", 0)
    merged.setdefault("chapter_count", 0)
    _comics_records.append(merged)


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


# ---- 相似作品查询（页面自动查重） ----

# 相似查询索引缓存：键为 (记录缓存对象 id, 条数)，记录缓存变化时自动重建
_similar_index_cache: tuple[dict[str, list[dict]], dict[str, list[dict]]] | None = None
_similar_index_state: tuple[int, int] | None = None


def _similar_index() -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """构建/复用相似查询索引：(名称索引, 作者索引)。

    - 名称索引：normalize_for_dedup(title / translated_title) -> 记录列表
    - 作者索引：normalize_for_dedup(extract_author(title)) -> 记录列表

    索引随 _cached_records 缓存对象变化自动重建（翻页保存/清空后），
    避免每次查询都全表归一化。
    """
    global _similar_index_cache, _similar_index_state
    records = _cached_records()
    state = (id(records), len(records))
    if _similar_index_cache is not None and _similar_index_state == state:
        return _similar_index_cache
    title_index: dict[str, list[dict]] = {}
    author_index: dict[str, list[dict]] = {}
    for rec in records:
        for name in (rec.get("title", ""), rec.get("translated_title", "")):
            key = normalize_for_dedup(name)
            if len(key) >= 2:
                title_index.setdefault(key, []).append(rec)
        author = normalize_for_dedup(extract_author(rec.get("title", "")))
        if len(author) >= 2:
            author_index.setdefault(author, []).append(rec)
    _similar_index_cache = (title_index, author_index)
    _similar_index_state = state
    return _similar_index_cache


def _load_all_cover_hashes() -> dict[str, str]:
    """一次性加载封面哈希缓存表（仅读缓存，不触发封面文件计算）。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT aid, ahash FROM cover_hashes").fetchall()
    finally:
        conn.close()
    return {str(row[0]): row[1] for row in rows}


def find_similar_records(
    detail_url: str,
    title: str,
    translated_title: str = "",
    cover_hash: str | None = None,
) -> list[dict]:
    """在本地数据库中查找与指定作品相似的记录（同作品重复上传）。

    Args:
        detail_url: 作品详情链接（用于排除自身）。
        title: 作品标题。
        translated_title: 作品翻译名（已有时传入，参与名称信号）。
        cover_hash: 作品封面感知哈希（可选，已下载封面时传入）。

    Returns:
        相似记录列表（按 aid 倒序）；同 aid 的自身记录不会返回。
    """
    title_index, author_index = _similar_index()
    body = normalize_for_dedup(title)
    name_keys = {key for key in (body, normalize_for_dedup(translated_title)) if key}
    author = normalize_for_dedup(extract_author(title))

    candidates: dict[str, dict] = {}
    # 名称信号：归一化后完全一致
    for key in name_keys:
        for rec in title_index.get(key, ()):
            candidates[rec.get("detail_url", "")] = rec
    # 作者信号：作者相同且主体名称相似
    if author:
        for rec in author_index.get(author, ()):
            url = rec.get("detail_url", "")
            if url not in candidates and _body_similar(
                body, normalize_for_dedup(rec.get("title", ""))
            ):
                candidates[url] = rec
    # 封面信号：封面哈希与库内记录比对（独立判定，名称不同也能命中）
    if cover_hash:
        library_hashes = _load_all_cover_hashes()
        for rec in _cached_records():
            url = rec.get("detail_url", "")
            if not url or url in candidates:
                continue
            h = library_hashes.get(str(rec.get("aid") or ""))
            if h and hamming_distance(cover_hash, h) <= COVER_HASH_THRESHOLD:
                candidates[url] = rec

    own_aid = _extract_aid(detail_url)
    results = [
        rec
        for url, rec in candidates.items()
        if url
        and url != detail_url
        and (not own_aid or rec.get("aid") != own_aid)
    ]
    results.sort(key=lambda r: -(r.get("aid") or 0))
    return results


def find_similar_map(
    items: list[tuple[str, str, str]],
    page_cover_hashes: dict[str, str] | None = None,
) -> dict[str, list[dict]]:
    """批量查询一批作品的相似记录（名称/作者/封面三信号，纯本地）。

    Args:
        items: [(detail_url, title, translated_title), ...]，页面抓取所得。
        page_cover_hashes: {detail_url: 页面作品封面哈希}（可选）。
            提供时参与封面信号；未提供的作品跳过封面信号。

    Returns:
        {detail_url: 相似记录列表}，仅含命中至少一个信号的作品。
    """
    title_index, author_index = _similar_index()
    library_records = _cached_records()
    library_hashes = (
        _load_all_cover_hashes() if page_cover_hashes else None
    )

    result: dict[str, list[dict]] = {}
    for detail_url, title, translated_title in items:
        if not detail_url or not title:
            continue
        body = normalize_for_dedup(title)
        name_keys = {
            key for key in (body, normalize_for_dedup(translated_title)) if key
        }
        author = normalize_for_dedup(extract_author(title))
        cover_hash = (page_cover_hashes or {}).get(detail_url)

        candidates: dict[str, dict] = {}
        for key in name_keys:
            for rec in title_index.get(key, ()):
                candidates[rec.get("detail_url", "")] = rec
        if author:
            for rec in author_index.get(author, ()):
                url = rec.get("detail_url", "")
                if url not in candidates and _body_similar(
                    body, normalize_for_dedup(rec.get("title", ""))
                ):
                    candidates[url] = rec
        if cover_hash and library_hashes is not None:
            for rec in library_records:
                url = rec.get("detail_url", "")
                if not url or url in candidates:
                    continue
                h = library_hashes.get(str(rec.get("aid") or ""))
                if h and hamming_distance(cover_hash, h) <= COVER_HASH_THRESHOLD:
                    candidates[url] = rec

        own_aid = _extract_aid(detail_url)
        matches = [
            rec
            for url, rec in candidates.items()
            if url
            and url != detail_url
            and (not own_aid or rec.get("aid") != own_aid)
        ]
        if matches:
            matches.sort(key=lambda r: -(r.get("aid") or 0))
            result[detail_url] = matches
    return result


def find_duplicates(progress_callback=None) -> list[dict]:
    """扫描数据库记录，按封面/名称/作者三信号聚类，返回重复组列表。

    Args:
        progress_callback: 可选回调 (done, total)，封面哈希计算阶段逐条调用。

    Returns:
        重复组列表，每组包含：
            - reason: 命中的查重信号说明（如「封面相似、名称相同」）
            - items: 组内记录（含 aid / author 等字段），按 aid 倒序

    信号说明：
        1. 封面相似：64 位 aHash 汉明距离 <= COVER_HASH_THRESHOLD
        2. 名称相同：归一化（去方括号+繁转简+去符号）后完全一致
        3. 作者+名称相似：提取作者相同，且主体名称相似（_body_similar）

    封面哈希有增量缓存（cover_hashes 表），重复查重不会重算已缓存封面。
    单条孤立记录（无任何匹配）不返回。
    """
    records = DatabaseManager.instance().load_comics()
    n = len(records)
    if n < 2:
        return []

    # 封面哈希批量加载（缓存命中不重算；缺封面/无法解析的跳过）
    cover_hashes = _collect_cover_hashes(records, progress_callback)

    # 并查集：把命中共识信号的记录合并到同一组
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 1. 封面聚类：64bit 哈希按 4×16bit 分块建索引，只查邻近桶避免 O(n^2)。
    #    汉明距离 <= 阈值时必有一个 16bit 块距离 <= 2，搜索该块 ±2 不漏判。
    block_size = 16
    reps: list[tuple[str, int]] = []  # (hash, record_index)，每组的代表
    block_index: dict[int, dict[int, list[int]]] = {b: {} for b in range(4)}

    def _find_cover_rep(hash_value: str) -> int | None:
        for block in range(4):
            start = block * block_size
            value = int(hash_value[start : start + block_size], 2)
            for delta in (-2, -1, 0, 1, 2):
                for rep_idx in block_index[block].get(value + delta, ()):
                    if hamming_distance(hash_value, reps[rep_idx][0]) <= COVER_HASH_THRESHOLD:
                        return reps[rep_idx][1]
        return None

    for i, rec in enumerate(records):
        hash_value = cover_hashes.get(str(rec.get("aid") or ""))
        if not hash_value:
            continue
        rep_idx = _find_cover_rep(hash_value)
        if rep_idx is not None:
            union(i, rep_idx)
        else:
            group_idx = len(reps)
            reps.append((hash_value, i))
            for block in range(4):
                start = block * block_size
                value = int(hash_value[start : start + block_size], 2)
                block_index[block].setdefault(value, []).append(group_idx)

    # 2. 名称聚类：归一化后完全相同
    name_groups: dict[str, int] = {}
    for i, rec in enumerate(records):
        key = normalize_for_dedup(rec.get("title", ""))
        if len(key) < 2:
            continue
        if key in name_groups:
            union(i, name_groups[key])
        else:
            name_groups[key] = i

    # 3. 作者+名称相似聚类：作者相同且主体名称相似
    author_reps: dict[str, list[int]] = {}
    for i, rec in enumerate(records):
        author = normalize_for_dedup(extract_author(rec.get("title", "")))
        body = normalize_for_dedup(rec.get("title", ""))
        if len(author) < 2 or len(body) < 3:
            continue
        for rep_idx in author_reps.get(author, []):
            if _body_similar(body, normalize_for_dedup(records[rep_idx].get("title", ""))):
                union(i, rep_idx)
                break
        author_reps.setdefault(author, []).append(i)

    # 汇总：每个连通分量（>= 2 条）为一个重复组，并标注命中信号
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    result: list[dict] = []
    for idxs in groups.values():
        if len(idxs) < 2:
            continue

        def _cover_match(a: dict, b: dict) -> bool:
            ha = cover_hashes.get(str(a.get("aid") or ""))
            hb = cover_hashes.get(str(b.get("aid") or ""))
            return bool(ha and hb and hamming_distance(ha, hb) <= COVER_HASH_THRESHOLD)

        def _title_match(a: dict, b: dict) -> bool:
            ka = normalize_for_dedup(a.get("title", ""))
            kb = normalize_for_dedup(b.get("title", ""))
            return bool(ka and ka == kb)

        def _author_match(a: dict, b: dict) -> bool:
            aa = normalize_for_dedup(extract_author(a.get("title", "")))
            ab = normalize_for_dedup(extract_author(b.get("title", "")))
            if not aa or not ab or aa != ab:
                return False
            return _body_similar(
                normalize_for_dedup(a.get("title", "")),
                normalize_for_dedup(b.get("title", "")),
            )

        reasons: set[str] = set()
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                a, b = records[idxs[x]], records[idxs[y]]
                if _cover_match(a, b):
                    reasons.add("封面相似")
                if _title_match(a, b):
                    reasons.add("名称相同")
                if _author_match(a, b):
                    reasons.add("作者+名称相似")
        items = sorted(
            (records[i] for i in idxs), key=lambda r: -(r.get("aid") or 0)
        )
        result.append(
            {
                "reason": "、".join(sorted(reasons)) or "疑似重复",
                "items": items,
            }
        )
    result.sort(key=lambda g: -(g["items"][0].get("aid") or 0))
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
                    chapter_count INTEGER DEFAULT 0,
                    cover_url TEXT DEFAULT '',
                category TEXT DEFAULT '',
                category_label TEXT DEFAULT '',
                author TEXT DEFAULT '',
                downloaded INTEGER DEFAULT 0,
                downloaded_at TEXT DEFAULT '',
                favorite INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
                )
                """
            )
            # 封面感知哈希缓存（aid 唯一，供查重复用）
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cover_hashes (
                    aid INTEGER PRIMARY KEY,
                    ahash TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
                )
                """
            )
            # 兼容旧库：补充 downloaded / downloaded_at / aid / translated_title / author 列
            # ，并对 aid 与 author 做一次性回填
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
            if "author" not in cols:
                conn.execute("ALTER TABLE comics ADD COLUMN author TEXT DEFAULT ''")
            if "favorite" not in cols:
                conn.execute("ALTER TABLE comics ADD COLUMN favorite INTEGER DEFAULT 0")
            if "chapter_count" not in cols:
                conn.execute("ALTER TABLE comics ADD COLUMN chapter_count INTEGER DEFAULT 0")
            conn.execute(
                """
                UPDATE comics SET aid = CAST(
                    substr(detail_url, instr(detail_url, 'aid-') + 4) AS INTEGER)
                WHERE aid IS NULL
                """
            )
            # 一次性回填 author：从标题方括号块提取（已有值跳过）
            for row in conn.execute("SELECT detail_url, title FROM comics WHERE author = ''"):
                author = extract_author(row["title"])
                if author:
                    conn.execute(
                        "UPDATE comics SET author = ? WHERE detail_url = ?",
                        (author, row["detail_url"]),
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
        使用 UPSERT（ON CONFLICT DO UPDATE）而非 INSERT OR REPLACE：
        后者先删后插，引用旧值的 COALESCE 子查询行为不可靠；UPSERT
        明确保留数据库中的翻译名/下载标记/完成时间（新抓取数据不携带）。
        """
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO comics
                    (detail_url, aid, title, translated_title, date,
                     image_count, chapter_count, cover_url, category, category_label,
                     downloaded, downloaded_at, author, favorite)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(detail_url) DO UPDATE SET
                    aid = excluded.aid,
                    title = excluded.title,
                    translated_title = CASE
                        WHEN excluded.translated_title = '' THEN comics.translated_title
                        ELSE excluded.translated_title END,
                    date = excluded.date,
                    image_count = CASE
                        WHEN excluded.image_count > 0 THEN excluded.image_count
                        WHEN excluded.chapter_count > 0 THEN 0
                        ELSE comics.image_count END,
                    chapter_count = CASE
                        WHEN excluded.chapter_count > 0 THEN excluded.chapter_count
                        WHEN excluded.image_count > 0 THEN 0
                        ELSE comics.chapter_count END,
                    cover_url = excluded.cover_url,
                    category = excluded.category,
                    category_label = excluded.category_label,
                    downloaded = comics.downloaded,
                    downloaded_at = comics.downloaded_at,
                    favorite = comics.favorite,
                    author = CASE
                        WHEN excluded.author = '' THEN comics.author
                        ELSE excluded.author END
                """,
                [
                    (
                        c.get("detail_url", ""),
                        _extract_aid(c.get("detail_url", "")),
                        c.get("title", ""),
                        c.get("translated_title", ""),
                        c.get("date", ""),
                        int(c.get("image_count") or 0),
                        int(c.get("chapter_count") or 0),
                        c.get("cover_url", ""),
                        c.get("category", ""),
                        c.get("category_label", ""),
                        0,
                        "",
                        extract_author(c.get("title", "")),
                        0,
                    )
                    for c in comics
                ],
            )
        # 增量合并缓存：新抓取的记录直接并入（保留 COALESCE 字段），
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

    def set_favorite(self, comic: dict, favorite: bool):
        """保存收藏状态；搜索结果尚未入库时同时保存作品资料。"""
        detail_url = comic.get("detail_url", "")
        if not detail_url:
            raise ValueError("作品缺少详情链接，无法保存收藏")
        self.save_comics([comic])
        with self._connect() as conn:
            conn.execute(
                "UPDATE comics SET favorite = ?, updated_at = datetime('now', 'localtime') "
                "WHERE detail_url = ?",
                (int(favorite), detail_url),
            )
        _upsert_cached_record({"detail_url": detail_url, "favorite": int(favorite)})

    def load_favorites(self) -> list[dict]:
        """返回所有收藏作品，按作品 ID 从新到旧排列。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM comics WHERE favorite = 1 "
                "ORDER BY aid IS NULL, aid DESC"
            ).fetchall()
        return [dict(row) for row in rows]

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

    def delete_comics(self, detail_urls: list[str]):
        """按详情链接批量删除记录（仅删数据库记录，不动封面缓存与下载文件）。"""
        if not detail_urls:
            return
        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM comics WHERE detail_url = ?",
                [(url,) for url in detail_urls],
            )
        _invalidate_records_cache()

    def delete_covers(self, aids: list[str]):
        """删除指定作品的封面缓存文件与对应哈希记录（aids 为作品 ID 字符串）。"""
        if not aids:
            return
        for aid in aids:
            (COVERS_DIR / f"{aid}.jpg").unlink(missing_ok=True)
        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM cover_hashes WHERE aid = ?", [(a,) for a in aids]
            )

    def clear_comics(self):
        """清空漫画记录。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM comics")
        _invalidate_records_cache()

    def clear_covers(self):
        """清除封面缓存文件与对应的哈希缓存记录。"""
        for path in COVERS_DIR.glob("*.jpg"):
            path.unlink(missing_ok=True)
        with self._connect() as conn:
            conn.execute("DELETE FROM cover_hashes")


def format_size(size: int) -> str:
    """将字节数格式化为易读文本。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.2f} MB"
