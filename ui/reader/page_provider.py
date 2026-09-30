"""阅读器页面获取编排：本地优先 → 直链索引 → 联网解析下载。

三层缓存策略（对应「边看边下边缓存」）：
1. 本地命中：直接返回磁盘路径，零网络请求；
2. 直链索引命中（未过期）：按索引下载该页图片；
3. 全部未命中：解析阅读页图片列表 → 取直链 → 下载并写盘 → 回填索引。

预取：请求第 N 页后，后台按 [N-behind, N+ahead] 窗口补拉缺失页；
窗口外任务取消，避免用户快速翻页时请求堆积。

本模块**不导入任何 Qt**：耗时操作由调用方（reader 窗口）放进线程执行，
通过回调汇报结果，便于离线单测。
"""
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path

from src.config import ProjectConfig
from src.downloader import _download_folder_name, _extract_reading_page_url
from src.net import fetch
from src.utils import safe_filename
from src.wnacg_crawler import (
    G_URL,
    HEADERS,
    download_pictures,
    parse_picture_page,
)
from ui.reader.cache_index import (
    ReaderCacheIndex,
    extract_aid,
    find_local_dir,
    index_local_images,
    scan_local_images,
)

logger = logging.getLogger(__name__)

# 阅读页 URL 中的「总页数-aid」片段
_PAGE_AID_RE = re.compile(r"page-(\d+)-aid-(\d+)")


class PageFetchError(RuntimeError):
    """单页获取失败（网络/解析/落盘），附加页码便于界面定位。"""

    def __init__(self, page_no: int, message: str):
        super().__init__(message)
        self.page_no = page_no


class ReaderSession:
    """一部作品的阅读会话：管理本地目录、直链索引与按页获取。

    典型用法（在后台线程中调用 fetch_page 即可，见 prefetch_worker）：

        session = ReaderSession(comic)
        session.ensure_ready()            # 定位/创建本地目录，读索引
        path = session.fetch_page(3)      # 本地优先，缺则联网补拉
        session.prefetch_around(3)        # 后台预取窗口

    线程安全：fetch_page / prefetch 内部加锁，允许预取线程与前台并发调用。
    """

    def __init__(self, comic: dict, base_dir: Path | None = None, index: ReaderCacheIndex | None = None):
        self.comic = dict(comic or {})
        self.aid = extract_aid(self.comic.get("detail_url", ""))
        self.title = self.comic.get("title", "")
        self.index = index or ReaderCacheIndex()
        self._base_dir = Path(base_dir) if base_dir else self._resolve_base_dir()
        self._lock = threading.RLock()
        # 本地目录（延迟确定；ensure_ready 后可用）
        self._local_dir: Path | None = None
        # 内存中的页 → 磁盘路径映射（避免每次都扫盘）
        self._local_files: list[Path] = []
        # {页码: 磁盘路径}：按文件名里的数字对齐页码（修复缺页错位）
        self._local_by_number: dict[int, Path] = {}
        # 直链索引（内存镜像，减少 SQLite 查询）：{页码: (直链, 真实页名)}
        self._page_urls: dict[int, tuple[str, str]] = {}
        # 阅读页每页图片数缓存：{reading_url: 张数}（避免反复探测）
        self._per_page_cache: dict[str, int] = {}
        # 列表页条目缓存：{列表页码: 条目列表}（相邻页同列表页时复用，免重复请求）
        self._list_pages_cache: dict[int, list[dict]] = {}
        # 阅读页 URL 缓存（详情页解析结果）：避免每页都联网拉详情页
        self._reading_url_cache: str | None = None
        # 本地目录的轻量变更指示（目录项数 + 最新 mtime）：翻页是高频操作，
        # 全目录扫描要 10ms+，因此用便宜得多的判据决定"是否需要真正重扫"。
        self._dir_revision_cache: tuple[int, float] | None = None

    # ---- 初始化 ----

    @staticmethod
    def _resolve_base_dir() -> Path:
        return Path(ProjectConfig().download_dir)

    def ensure_ready(self) -> None:
        """定位本地目录并加载直链索引（打开阅读器时调用一次）。"""
        with self._lock:
            self._local_dir = self._locate_or_plan_dir()
            self._refresh_local_files(force=True)
            self._page_urls = self.index.load_page_urls(self.aid)

    def refresh(self) -> None:
        """显式刷新本地索引（外部写入文件后调用，如下载完成）。

        翻页路径不会自动重扫（性能考虑），需要感知外部新增文件时
        由调用方显式触发。
        """
        with self._lock:
            self._refresh_local_files(force=True)

    def _locate_or_plan_dir(self) -> Path:
        """定位现有本地目录；不存在时规划（但不创建）将使用的目录。"""
        existing = find_local_dir(self.comic, self._base_dir)
        if existing is not None:
            return existing
        use_label = ProjectConfig().folder_use_category_label
        name = _download_folder_name(self.comic, use_label)
        return self._base_dir / name

    def _refresh_local_files(self, force: bool = False) -> None:
        """重新扫描本地图片（下载完成后调用可感知新文件）。

        同时维护两套视图：
        - `_local_files`：自然排序列表（用于计数与卡片进度）；
        - `_local_by_number`：{页码: 路径}（用于按页取图，缺页不错位）。

        **增量判定**：翻页是高频操作，而 `rglob("*")` 在图片多的作品上
        要 10ms+。先用轻量的目录项快照（`iterdir` 一层 + `stat`）比对
        指纹，未变化时直接复用缓存结果，避免每次翻页都全盘扫描。

        Args:
            force: True 时无条件重扫（如刚下载完新文件）。
        """
        if self._local_dir is None:
            self._local_files = []
            self._local_by_number = {}
            return
        revision = self._dir_revision()
        if not force and revision == self._dir_revision_cache:
            return
        self._dir_revision_cache = revision
        self._local_files = scan_local_images(self._local_dir)
        self._local_by_number = index_local_images(self._local_dir)

    @staticmethod
    def _scan_fingerprint(folder: Path) -> dict[str, tuple[float, int]]:
        """生成目录指纹：{相对路径: (mtime, size)}。

        只遍历目录树收集条目元信息（不读文件内容、不做后缀过滤、不排序），
        比完整的 scan_local_images（含过滤 + 排序 + 建索引）轻，用于判断
        "是否需要真正重扫"。
        """
        result: dict[str, tuple[float, int]] = {}
        try:
            for path in folder.rglob("*"):
                try:
                    if not path.is_file():
                        continue
                    st = path.stat()
                    result[path.relative_to(folder).as_posix()] = (
                        st.st_mtime,
                        st.st_size,
                    )
                except OSError:
                    # 单个文件 stat 失败（权限/被删除）不影响整体判定
                    continue
        except OSError:
            return {}
        return result

    def _dir_revision(self) -> tuple[int, float]:
        """轻量目录变更指示：目录项数量 + 最新修改时间。

        比逐文件 stat 的完整指纹便宜得多（一次 scandir 即可），足以捕捉
        "新增/删除文件"这一最常见的变更；内容替换（同名改写）由写盘路径
        显式 `refresh()` 覆盖。
        """
        if self._local_dir is None or not self._local_dir.is_dir():
            return (0, 0.0)
        count = 0
        newest = 0.0
        try:
            for entry in self._local_dir.rglob("*"):
                try:
                    if not entry.is_file():
                        continue
                    count += 1
                    newest = max(newest, entry.stat().st_mtime)
                except OSError:
                    continue
        except OSError:
            return (0, 0.0)
        return (count, newest)

    @property
    def local_dir(self) -> Path | None:
        return self._local_dir

    @property
    def local_count(self) -> int:
        with self._lock:
            return len(self._local_files)

    def is_offline_ready(self, expected_total: int = 0) -> bool:
        """本地图片是否已满足阅读（用于决定是否完全离线）。"""
        with self._lock:
            if not self._local_files:
                return False
            total = int(expected_total or self.comic.get("image_count") or 0)
            return total <= 0 or len(self._local_files) >= total

    # ---- 页数 ----

    def resolve_total_pages(self) -> int:
        """确定作品总页数：优先 image_count，其次本地文件数，最后读阅读页。

        wnacg 列表页的「N 張」即图片总数，直接可用；缺失时才联网，
        避免打开阅读器就产生额外请求。
        """
        count = int(self.comic.get("image_count") or 0)
        if count > 0:
            return count
        with self._lock:
            if self._local_files:
                return len(self._local_files)
        reading_url = self._reading_url()
        if not reading_url:
            return 1
        match = _PAGE_AID_RE.search(reading_url)
        return int(match.group(1)) if match else 1

    def _reading_url(self) -> str | None:
        """从详情页提取阅读页 URL（含总页数）；失败返回 None。

        结果**进程内缓存**：原先每页都重新 fetch 详情页，翻页时平白多出
        一次网络往返（实测 8s+）。同一会话内详情页不会变，缓存即可。
        """
        if self._reading_url_cache is not None:
            return self._reading_url_cache or None
        detail_url = self.comic.get("detail_url", "")
        if not detail_url:
            self._reading_url_cache = ""
            return None
        try:
            from lxml import html as lxml_html

            detail_html = fetch(G_URL + detail_url, HEADERS)
            url = _extract_reading_page_url(lxml_html.fromstring(detail_html))
            self._reading_url_cache = url or ""
            return url
        except Exception as exc:  # 网络/解析失败：交由上层降级
            logger.warning("提取阅读页失败: %s (%s)", detail_url, exc)
            # 失败不写入缓存：下次仍可重试（网络抖动是暂时的）
            return None

    # ---- 单页获取（核心） ----

    def fetch_page(self, page_no: int) -> Path:
        """获取第 page_no 页的本地图片路径（1-based）。

        顺序：内存/磁盘本地命中 → 直链索引下载 → 联网解析后下载。
        任何阶段失败抛 PageFetchError，由界面显示「重试」。

        Returns:
            该页图片的磁盘路径。
        """
        page_no = max(1, int(page_no))
        with self._lock:
            local = self._local_by_page(page_no)
            if local is not None:
                return local
            # 本地已覆盖全部页：缺页说明确实没有该页（而非"需要联网"），
            # 直接失败，避免为一部已下载完整的作品平白发起网络请求。
            # 注意判据是 local_count >= 总页数，且总页数可信（来自
            # image_count 或本地推断），否则会把"本地不全"误判成"已完整"。
            if self._local_is_complete():
                raise PageFetchError(
                    page_no, f"第 {page_no} 页本地缺失（该作品已缓存完整）"
                )

        # 直链索引命中：直接下载该页图片。
        # 文件名必须沿用真实「页名」（网页 <span class="name"> / 压缩包内名），
        # 不能用 page_no 顶替：否则「第 2 页」会被写成 2.jpg，与本地既有的
        # 0002.jpg 并存，排序后页码整体错位（图片顺序不对）。
        indexed = self._page_urls.get(page_no)
        if indexed:
            url, page_name = indexed
            path = self._download_one(
                page_no, {"link": url, "number": page_name}
            )
            if path is not None:
                return path

        # 全部未命中：联网解析该页图片列表
        pages = self._parse_page(page_no)
        if not pages:
            raise PageFetchError(page_no, f"第 {page_no} 页未解析到图片")
        # 该页解析出的条目依次对应 page_no, page_no+1, ...（用于相邻页预填）
        # 同时记录网页给出的真实页名，供索引路径下载时沿用
        discovered: dict[int, tuple[str, str]] = {}
        for i, entry in enumerate(pages):
            no = page_no + i
            name = str(entry.get("number") or "").strip() or str(no)
            discovered[no] = (entry["link"], name)
        with self._lock:
            self._page_urls.update(discovered)
        self.index.save_page_urls(self.aid, discovered)

        path = self._download_one(page_no, pages[0])
        if path is None:
            raise PageFetchError(page_no, f"第 {page_no} 页图片下载失败")
        return path

    def _local_by_page(self, page_no: int) -> Path | None:
        """按页码取本地图片。

        **按文件名里的页码对齐**，而不是用排序下标——这样只缓存了
        第 1、2、3、10 页时，第 10 页就是第 10 页，不会被错当成第 4 页。
        文件名无法解析出页码时（罕见），回退为排序下标取。

        注意：本方法**不做**目录刷新——刷新由 ensure_ready / 写盘后
        显式调用，避免每次取页都触发全盘扫描（见 `_refresh_local_files`）。
        """
        path = self._local_by_number.get(page_no)
        if path is not None:
            return path
        # 回退：全部文件名都无页码可用时，按排序下标对上（保证兼容）
        if not self._local_by_number and 1 <= page_no <= len(self._local_files):
            return self._local_files[page_no - 1]
        return None

    def _local_is_complete(self) -> bool:
        """本地是否已覆盖作品全部页（用于短路网络请求）。

        判据：本地图片数 >= 作品总页数。总页数优先取 `image_count`
        （列表页的「N 張」，可信）；为 0 时退而用本地文件数——此时
        "本地有文件"即视为完整（否则无从判断完整性，宁可让它联网）。

        **二次校验**：站点的 `image_count` 可能与实际张数不符（重传/
        合并/计数偏差），若本地张数 >= 声明总数的一半以上，再检查本地
        页码是否**连续无缺口**（最大页码 == 本地张数，即 1..N 全覆盖）。
        连续则视为完整——宁可相信落盘的连续页码，也不为站点的错误计数
        平白发起一轮列表页解析 + 逐张直链抓取。
        """
        total = int(self.comic.get("image_count") or 0)
        count = self.local_count
        if total <= 0:
            # 总数未知：只要有本地文件就认为够用，避免无谓联网
            return count > 0
        if count >= total:
            return True
        # 本地明显不够，且差距过大（< 一半）：很可能确实不全，放行联网
        if count * 2 < total:
            return False
        # 本地接近声明总数：用页码连续性判断（站点计数可能虚高）
        if not self._local_by_number:
            return False
        return max(self._local_by_number) == count

    def _parse_page(self, page_no: int) -> list[dict]:
        """解析出**第 page_no 张图片**的条目（含直链）。

        关键概念区分（曾在此处踩坑）：
        - `page_no` 是**图片序号**（第几张图，1-based，来自列表页的「N 張」）；
        - `parse_picture_page(start, end)` 的 start/end 是**阅读页的列表页码**
          （站点每页放若干张图，如每页 12 张）。

        直接令 start=end=page_no 是错的：看第 13 张图时 page_count 只有 2，
        会构造出 `range(12, 2)` 空区间 → 「未解析到任何图片链接」。
        正确做法是先确定每页张数，再把图片序号换算成列表页码。
        """
        reading_url = self._reading_url()
        if not reading_url:
            raise PageFetchError(page_no, "无法获取阅读页链接（详情页解析失败）")

        total_pages = self._reading_page_count(reading_url)
        per_page = self._images_per_reading_page(reading_url, total_pages)
        if per_page <= 0:
            raise PageFetchError(page_no, "无法确定阅读页每页图片数量")

        # 图片序号 → 列表页码（第 1..per_page 张在第 1 个列表页，依此类推）
        list_page = (page_no - 1) // per_page + 1
        offset = (page_no - 1) % per_page
        if list_page > total_pages:
            raise PageFetchError(
                page_no,
                f"第 {page_no} 张超出作品范围（共 {total_pages * per_page} 张）",
            )
        entries = self._list_page_entries(reading_url, list_page)
        if not entries:
            raise PageFetchError(page_no, f"第 {page_no} 张所在列表页未解析到图片")
        if offset >= len(entries):
            raise PageFetchError(
                page_no,
                f"第 {page_no} 张超出列表页范围（该页仅 {len(entries)} 张）",
            )
        # 返回从该张开始的条目，便于上层把相邻张一并回填索引
        return entries[offset:]

    def _list_page_entries(self, reading_url: str, list_page: int) -> list[dict]:
        """取某个列表页的图片条目，**按列表页缓存**避免重复请求。

        同一部作品的相邻若干张往往落在同一个列表页（站点每页约 12 张），
        连续翻页时若不缓存，每翻一页都要重新解析该列表页——每张图的直链
        解析本身又是并发网络请求的集合，重复代价很高。
        """
        cached = self._list_pages_cache.get(list_page)
        if cached is not None:
            return cached
        try:
            entries = parse_picture_page(
                reading_url, start=list_page, end=list_page
            )
        except Exception as exc:
            raise PageFetchError(
                list_page, f"解析列表页 {list_page} 失败: {exc}"
            ) from exc
        self._list_pages_cache[list_page] = entries
        return entries

    @staticmethod
    def _reading_page_count(reading_url: str) -> int:
        """阅读页的列表页总数（URL 里的 page-N-aid-X）。"""
        match = _PAGE_AID_RE.search(reading_url)
        return int(match.group(1)) if match else 1

    def _images_per_reading_page(self, reading_url: str, total_pages: int) -> int:
        """确定站点每个阅读页放多少张图（优先用总数推导，避免多余请求）。

        **优先用已知的总张数推导**：`每页张数 = ceil(总张数 / 列表页数)`，
        这类信息通常已由列表页的「N 張」（`image_count`）给出，无需联网。
        只有在总张数未知时才回退到「探测第 1 个列表页」——那是一次真实
        网络请求，翻页路径上应尽量避免。

        结果按 URL 缓存（`_per_page_cache`），同会话内不再重复计算。
        """
        cached = self._per_page_cache.get(reading_url)
        if cached:
            return cached

        # 路径 1：用已知总数推导（零网络请求）
        total_images = int(self.comic.get("image_count") or 0)
        if total_images > 0 and total_pages > 0:
            # 向上取整：最后一页可能不满
            count = -(-total_images // total_pages)
            if count > 0:
                self._per_page_cache[reading_url] = count
                return count

        # 路径 2：总数未知，退回探测列表页 1（一次网络请求）
        try:
            entries = parse_picture_page(reading_url, start=1, end=1)
        except Exception as exc:
            logger.warning("探测每页张数失败: %s", exc)
            return 0
        count = len(entries)
        if count <= 0:
            return 0
        self._per_page_cache[reading_url] = count
        return count

    def _download_one(self, page_no: int, picture: dict) -> Path | None:
        """下载单张图片到本地目录，返回落盘路径（失败返回 None）。

        复用 src 的 download_pictures：已存在非空文件自动跳过（断点续传），
        并由其内部完成并发控制、节流与原子写盘。

        落盘文件名取自 picture["number"]（真实页名），与下载侧完全一致；
        返回值直接按该名字定位，**不靠排序下标反推**——当文件名与页码不
        一致（如 0001.jpg / 2.jpg 混排）时，下标反推会取到错误的页。
        """
        if self._local_dir is None:
            return None
        config = ProjectConfig()
        self._local_dir.mkdir(parents=True, exist_ok=True)
        try:
            failed = download_pictures(
                [picture],
                self._local_dir,
                max_workers=1,
                max_retries=config.download_retries,
            )
        except Exception as exc:
            logger.warning("下载第 %d 页失败: %s", page_no, exc)
            return None
        if failed:
            return None
        # 按真实页名定位刚下载的文件
        name = safe_filename(str(picture.get("number") or "")).strip()
        if name:
            target = self._local_dir / f"{name}.jpg"
            if target.is_file() and target.stat().st_size > 0:
                # 强制重扫：新文件已落盘，必须让索引感知到
                self._refresh_local_files(force=True)
                return target
        # 页名缺失或落盘名不符时，回退按排序下标取
        self._refresh_local_files(force=True)
        return self._local_by_page(page_no)

    # ---- 预取 ----

    def prefetch_around(
        self,
        page_no: int,
        ahead: int = 3,
        behind: int = 1,
        cancel_check: Callable[[], bool] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        """预取 [page_no-behind, page_no+ahead] 窗口内缺失的页。

        Args:
            page_no: 当前页（1-based）。
            ahead / behind: 前后预取页数。
            cancel_check: 返回 True 时中止（用户已翻页/关闭窗口）。
            on_progress: 每成功补拉一页回调一次（参数为该页码）。
        """
        total = self.resolve_total_pages()
        start = max(1, page_no - max(0, behind))
        end = min(total, page_no + max(0, ahead))
        for target in range(start, end + 1):
            if cancel_check and cancel_check():
                return
            with self._lock:
                if self._local_by_page(target) is not None:
                    continue
            try:
                self.fetch_page(target)
                if on_progress:
                    on_progress(target)
            except PageFetchError as exc:
                logger.info("预取跳过第 %d 页: %s", exc.page_no, exc)

    # ---- 离线快照（供测试与界面展示） ----

    def local_pages(self) -> list[Path]:
        """返回当前本地已缓存图片的有序列表（快照）。"""
        with self._lock:
            self._refresh_local_files()
            return list(self._local_files)
