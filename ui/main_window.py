"""漫画浏览器主窗口。

负责顶部工具栏（搜索/分类/按钮）、内容区与翻页栏的组装，
以及列表浏览与关键词搜索两种模式的切换调度。
"""
import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.data_store import load_cache, save_cache
from ui.dialogs import SettingsDialog
from ui.download_manager import DownloadManager
from ui.fetch_worker import FetchWorker
from ui.models import ComicItem
from ui.settings import BASE_URL, CATEGORIES, AppSettings, ProjectConfig
from ui.styles import LIGHT_THEME_QSS
from ui.version import __version__
from ui.widgets import ComicsGrid, PageBar


class MainWindow(QMainWindow):
    """漫画浏览器主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"漫画浏览器 v{__version__}")
        self.setMinimumSize(1080, 720)
        self._comics: list[ComicItem] = []
        self._worker: FetchWorker | None = None
        self._silent_fetch: bool = False
        self._current_page: int = 1
        self._search_keyword: str = ""  # 非空时处于"关键词搜索"模式
        # 请求序号：每次发起新抓取递增；worker 完成回调时校验序号，
        # 过期结果（期间已发起新请求）直接丢弃，防止旧结果覆盖/污染数据。
        self._fetch_seq: int = 0
        # 待执行请求：worker 繁忙时记录 (silent, page)，当前 worker 结束后立即执行，
        # 避免连点翻页/切换分类时请求被静默丢弃。
        self._pending_request: tuple[bool, int] | None = None
        # 相似作品模式：列表切换为"某作品 + 本地库相似作品"的过滤视图
        self._similar_mode: bool = False
        self._similar_prev_comics: list[ComicItem] | None = None
        self._similar_worker = None  # 后台相似扫描线程引用
        self._settings = AppSettings()
        self._project_config = ProjectConfig()
        self._setup_ui()
        self._load_initial_data()

    # ---- UI 构建 ----

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet(LIGHT_THEME_QSS)

        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 顶部工具栏
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(24, 16, 24, 16)
        toolbar_layout.setSpacing(16)

        title = QLabel("漫画浏览器")
        title.setObjectName("titleLabel")
        toolbar_layout.addWidget(title)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索漫画名称，回车搜索")
        self.search_edit.setMinimumWidth(280)
        self.search_edit.returnPressed.connect(self._on_search_submit)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        toolbar_layout.addWidget(self.search_edit)

        # 分类选择下拉框：切换后自动抓取该分类内容（同时退出搜索模式）
        self.category_combo = QComboBox()
        self.category_combo.addItems(CATEGORIES.keys())
        self._select_current_category()
        self.category_combo.currentTextChanged.connect(self._on_category_changed)
        self.category_combo.setToolTip("切换浏览分类，选择后自动抓取该分类内容")
        toolbar_layout.addWidget(self.category_combo)

        toolbar_layout.addStretch()

        download_mgr_btn = QPushButton("下载管理")
        download_mgr_btn.setObjectName("downloadManagerButton")
        download_mgr_btn.clicked.connect(self._on_download_manager)
        toolbar_layout.addWidget(download_mgr_btn)

        refresh_btn = QPushButton("刷新")
        refresh_btn.setObjectName("refreshButton")
        refresh_btn.clicked.connect(self._on_refresh)
        toolbar_layout.addWidget(refresh_btn)

        settings_btn = QPushButton("设置")
        settings_btn.setObjectName("settingsButton")
        settings_btn.clicked.connect(self._on_settings)
        toolbar_layout.addWidget(settings_btn)

        root_layout.addWidget(toolbar)

        # 相似模式提示条（查看"某作品 + 相似作品"时显示，默认隐藏）
        self.similar_banner = QFrame()
        self.similar_banner.setObjectName("similarBanner")
        self.similar_banner.setStyleSheet(
            """
            QFrame#similarBanner {
                background-color: #FFF7ED;
                border-bottom: 1px solid #FDBA74;
                padding: 8px 24px;
            }
            QLabel#similarBannerLabel {
                color: #9A3412;
                font-size: 13px;
            }
            QPushButton {
                background-color: #F97316;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 5px 16px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #EA580C;
            }
            """
        )
        banner_layout = QHBoxLayout(self.similar_banner)
        banner_layout.setContentsMargins(0, 0, 0, 0)
        banner_layout.setSpacing(16)
        self.similar_banner_label = QLabel()
        self.similar_banner_label.setObjectName("similarBannerLabel")
        banner_layout.addWidget(self.similar_banner_label)
        banner_layout.addStretch()
        exit_similar_btn = QPushButton("返回列表")
        exit_similar_btn.setCursor(Qt.PointingHandCursor)
        # 用 lambda 包裹：clicked 信号携带的 bool 参数不能误传为 restore
        exit_similar_btn.clicked.connect(lambda: self._exit_similar_mode())
        banner_layout.addWidget(exit_similar_btn)
        self.similar_banner.setVisible(False)
        root_layout.addWidget(self.similar_banner)

        # 内容区（漫画网格）
        self.comics_grid = ComicsGrid()
        self.comics_grid.similar_requested.connect(self._on_view_similar)
        root_layout.addWidget(self.comics_grid)

        # 底部栏：统一底色（QFrame#pageBar 背景+顶边框）
        bottom_bar = QFrame()
        bottom_bar.setObjectName("pageBar")
        bottom_layout = QGridLayout(bottom_bar)
        bottom_layout.setContentsMargins(24, 0, 24, 0)
        bottom_layout.setSpacing(0)

        # 翻页栏铺满整行，按钮组以整个窗口中心为基准居中
        self.page_bar = PageBar()
        self.page_bar.page_requested.connect(self._on_page_requested)
        bottom_layout.setColumnStretch(0, 1)
        bottom_layout.addWidget(self.page_bar, 0, 0)

        # 左下角覆盖层：状态信息 + 进度条（透明，叠在翻页栏上层）
        left_host = QWidget()
        left_host.setStyleSheet("background-color: transparent;")
        # 鼠标事件穿透到下层翻页栏：状态区只是浮层，不拦截按钮点击
        left_host.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        left_layout = QHBoxLayout(left_host)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(16)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("statusLabel")
        # 限制状态文本宽度：覆盖层不会向右延伸遮挡居中的翻页按钮
        self.status_label.setMaximumWidth(260)
        left_layout.addWidget(self.status_label)

        # 加载进度条：放入固定宽度容器，显隐切换不会引起布局跳动
        progress_host = QWidget()
        progress_host.setFixedWidth(130)
        progress_layout = QVBoxLayout(progress_host)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setAlignment(Qt.AlignVCenter)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        progress_layout.addWidget(self.progress_bar)
        left_layout.addWidget(progress_host)

        bottom_layout.addWidget(left_host, 0, 0, Qt.AlignLeft | Qt.AlignVCenter)

        root_layout.addWidget(bottom_bar)

    # ---- 数据加载 ----

    def _current_category_filter(self) -> str | None:
        """从当前列表 URL 提取分类（cate-N），无分类（全部更新）时返回 None。"""
        match = re.search(r"cate-(\d+)\.html", self._project_config.list_url)
        return f"cate-{match.group(1)}" if match else None

    def _load_initial_data(self):
        """启动时加载数据：优先数据库，其次 JSON 缓存，否则示例数据。

        数据库与缓存均为本地数据，无需网络请求；若开启自动抓取则在后台刷新。
        只加载当前分类的作品，并按作品 ID 倒序（与网站最新顺序一致）。
        """
        from ui.database import DatabaseManager

        records = DatabaseManager.instance().load_comics(
            category=self._current_category_filter(),
            limit=self._project_config.page_number,
        )
        if records:
            self.set_comics_from_raw(records)
            self.set_status(f"已从数据库加载，共 {len(records)} 部漫画")
        else:
            cached = load_cache()
            if cached:
                # 数据库为空时，将旧 JSON 缓存迁移到数据库
                DatabaseManager.instance().save_comics(cached)
                self.set_comics_from_raw(cached)
                self.set_status(f"已加载本地缓存，共 {len(cached)} 部漫画")
            else:
                self._load_sample_data()

        if self._settings.auto_fetch_on_startup:
            # 延迟到事件循环中触发，避免阻塞启动流程
            QTimer.singleShot(0, lambda: self._start_fetch(silent=True))

    def _load_sample_data(self):
        """加载示例数据；实际可替换为爬虫或本地 JSON。"""
        sample = [
            ComicItem(
                title="示例漫画 01",
                image_count=24,
                upload_date="2026-08-01",
                detail_url="/photos-index-page-1-aid-10001.html",
                category="cate-5",
                category_label="同人誌",
            ),
            ComicItem(
                title="示例漫画 02",
                image_count=36,
                upload_date="2026-08-02",
                detail_url="/photos-index-page-1-aid-10002.html",
                category="cate-9",
                category_label="單行本／漢化",
            ),
            ComicItem(
                title="示例漫画 03",
                image_count=18,
                upload_date="2026-08-03",
                detail_url="/photos-index-page-1-aid-10003.html",
                category="cate-19",
                category_label="韓漫",
            ),
            ComicItem(
                title="示例漫画 04",
                image_count=42,
                upload_date="2026-08-04",
                detail_url="/photos-index-page-1-aid-10004.html",
                category="cate-22",
                category_label="3D&漫畫",
            ),
            ComicItem(
                title="示例漫画 05",
                image_count=28,
                upload_date="2026-08-05",
                detail_url="/photos-index-page-1-aid-10005.html",
                category="cate-37",
                category_label="AI&圖集",
            ),
        ]
        self.set_comics(sample)

    def set_comics(self, comics: list[ComicItem]):
        """设置漫画列表并刷新界面。"""
        self._comics = comics
        self.comics_grid.render(comics)
        self.set_status(f"共 {len(comics)} 部漫画")

    def set_status(self, text: str):
        """在左下角状态栏显示提示（超长自动省略，悬停显示全文）。

        状态文本统一经此入口设置：超过状态区宽度（maximumWidth）时
        显示省略号，不会撑宽覆盖层遮挡居中的翻页按钮；完整文本
        通过悬停 tooltip 查看。卡片等子组件可经 window() 调用。
        """
        metrics = QFontMetrics(self.status_label.font())
        elided = metrics.elidedText(
            text, Qt.ElideRight, self.status_label.maximumWidth()
        )
        self.status_label.setText(elided)
        self.status_label.setToolTip(text if elided != text else "")

    def set_comics_from_raw(self, raw_comics: list[dict]):
        """将爬虫原始字典转换为 ComicItem 并展示（自动关联本地封面缓存）。

        抓取/搜索结果不含 translated_title 字段，但该作品可能已在数据库中
        保存过翻译名（重启后仍生效），此处从数据库缓存合并回填。
        """
        from ui.database import _cached_records, cover_cache_path

        # 数据库缓存：detail_url -> translated_title（含增量缓存，无需重复全表查询）
        translations = {
            r.get("detail_url", ""): r.get("translated_title", "")
            for r in _cached_records()
        }
        items = []
        for c in raw_comics:
            detail_url = c.get("detail_url", "")
            items.append(
                ComicItem(
                    title=c.get("title", "未知"),
                    image_count=c.get("image_count", 0),
                    upload_date=c.get("date", ""),
                    detail_url=detail_url,
                    cover_url=c.get("cover_url", ""),
                    cover_path=cover_cache_path(detail_url),
                    category=c.get("category", ""),
                    category_label=c.get("category_label", ""),
                    translated_title=(
                        c.get("translated_title") or translations.get(detail_url, "")
                    ),
                )
            )
        self.set_comics(items)

    # ---- 搜索 ----

    def _on_search_submit(self):
        """回车触发网站搜索；输入为空时若处于搜索模式则返回列表。"""
        keyword = self.search_edit.text().strip()
        if keyword:
            self._search_keyword = keyword
            self._start_fetch(page=1)
        elif self._search_keyword:
            self._exit_search()

    def _on_search_text_changed(self, text: str):
        """清空搜索框时退出搜索模式，回到列表第 1 页。"""
        if not text.strip() and self._search_keyword:
            self._exit_search()

    def _exit_search(self):
        """退出搜索模式：清空关键词并抓取列表第 1 页。"""
        self._search_keyword = ""
        self._start_fetch(page=1)

    # ---- 分类 ----

    def _select_current_category(self):
        """根据当前 list_url 匹配分类下拉的初始选项。"""
        current = self._project_config.list_url
        match = re.search(r"cate-(\d+)\.html", current)
        if match:
            target = f"cate-{match.group(1)}.html"
            for name, suffix in CATEGORIES.items():
                if suffix.endswith(target):
                    self.category_combo.setCurrentText(name)
                    return
        self.category_combo.setCurrentText("全部更新")

    def _on_category_changed(self, name: str):
        """切换分类：退出搜索模式，更新列表 URL 并抓取该分类第 1 页。"""
        self._search_keyword = ""
        self.search_edit.clear()  # 关键词已清空，textChanged 不会重复触发请求
        suffix = CATEGORIES.get(name, "albums.html")
        self._project_config.list_url = f"{BASE_URL}/{suffix}"
        self._project_config.save()
        self._start_fetch(silent=False, page=1)

    # ---- 抓取调度 ----

    def _start_fetch(self, silent: bool = False, page: int | None = None):
        """启动后台抓取/搜索线程。

        worker 繁忙（信号槽为异步排队，期间状态可能已变化）时不再静默丢弃
        新请求，而是记录为待执行请求，当前 worker 结束后立即执行。

        Args:
            silent: 为 True 时出错不弹窗，仅在状态栏提示（用于启动自动刷新）。
            page: 要抓取的页码，默认使用当前页码。
        """
        if self._worker and self._worker.isRunning():
            # 记录最新请求（连点翻页/切分类时只保留最后一次），完成后立即执行
            self._pending_request = (silent, page if page is not None else self._current_page)
            return
        self._launch_fetch(silent, page)

    def _launch_fetch(self, silent: bool, page: int | None):
        """实际创建并启动抓取线程，绑定本次请求序号。"""
        # 翻页/刷新/搜索/切分类都会离开相似过滤视图（列表将重新抓取）
        self._exit_similar_mode(restore=False)
        self._fetch_seq += 1
        seq = self._fetch_seq

        if page is not None and page >= 1:
            self._current_page = page
            self.page_bar.set_page(page)

        self._silent_fetch = silent
        if self._search_keyword:
            self.set_status(
                f"正在搜索“{self._search_keyword}”第 {self._current_page} 页..."
            )
        else:
            self.set_status(f"正在抓取第 {self._current_page} 页...")
        self.progress_bar.setVisible(True)

        self._worker = FetchWorker(
            url=self._project_config.list_url,
            page=self._current_page,
            keyword=self._search_keyword,
        )
        self._worker.finished.connect(
            lambda raw, s=seq: self._on_fetch_finished(raw, s)
        )
        self._worker.error.connect(
            lambda msg, s=seq: self._on_fetch_error(msg, s)
        )
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_page_requested(self, page: int):
        """翻页栏请求跳转回调。"""
        if page >= 1:
            self._start_fetch(page=page)

    def _on_fetch_finished(self, raw_comics: list[dict], seq: int):
        """抓取线程完成回调；seq 与当前请求不符时视为过期结果，直接丢弃。"""
        if seq != self._fetch_seq:
            # 期间已发起新请求（搜索/切分类/翻页），旧结果不再使用：
            # 避免搜索结果被当成列表写入数据库/缓存（数据污染）
            return
        self._worker = None
        self.progress_bar.setVisible(False)
        if self._search_keyword:
            # 搜索结果仅展示，不写入缓存与数据库（避免污染下次启动的本地数据）
            self.set_comics_from_raw(raw_comics)
            self.set_status(
                f"搜索“{self._search_keyword}”：第 {self._current_page} 页，"
                f"共 {len(raw_comics)} 部漫画"
            )
        else:
            save_cache(raw_comics)
            # 抓取结果写入数据库，供下次启动直接加载
            from ui.database import DatabaseManager

            DatabaseManager.instance().save_comics(raw_comics)
            self.set_comics_from_raw(raw_comics)
            if not raw_comics:
                # HTML 正常但解析 0 条：极可能是站点改版导致 XPath 失效
                self.set_status(
                    "未解析到漫画数据（站点结构可能已改版，请更新程序或检查网络）"
                )
            else:
                self.set_status(
                    f"第 {self._current_page} 页：共 {len(raw_comics)} 部漫画"
                )
        # 页面数据就绪后自动查询本地库相似作品（后台，不阻塞界面）
        if raw_comics:
            self._start_similar_scan(raw_comics)
        self._run_pending_request()

    def _on_fetch_error(self, message: str, seq: int):
        """抓取线程错误回调；过期请求的错误同样丢弃，不打扰用户。"""
        if seq != self._fetch_seq:
            return
        self._worker = None
        self.progress_bar.setVisible(False)
        if self._silent_fetch:
            self.set_status(f"自动抓取失败: {message}")
        else:
            QMessageBox.critical(self, "抓取失败", f"抓取过程中出现错误:\n{message}")
            self.set_status("抓取失败")
        self._run_pending_request()

    # ---- 相似作品 ----

    def _start_similar_scan(self, raw_comics: list[dict]):
        """页面抓取/搜索完成后：后台扫描本页作品与本地库的相似关系。

        只查询本地数据库（名称/作者/封面三信号）；封面缺失时后台
        下载并计算感知哈希存入缓存表，随后一次性更新卡片角标。
        """
        from ui.similar_worker import SimilarWorker

        items = [
            (
                c.get("detail_url", ""),
                c.get("title", ""),
                c.get("translated_title", ""),
                c.get("cover_url", ""),
            )
            for c in raw_comics
            if c.get("detail_url")
        ]
        if not items:
            return
        worker = SimilarWorker(items)
        self._similar_worker = worker
        worker.finished_ok.connect(self._on_similar_done)
        worker.error.connect(self._on_similar_error)
        worker.finished_ok.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        worker.start()

    def _on_similar_done(self, similar_map: dict):
        """后台相似扫描完成：批量更新当前卡片左下角 ≈N 角标。"""
        self._similar_worker = None
        self.comics_grid.apply_similar_map(similar_map)

    def _on_similar_error(self, message: str):
        """后台相似扫描失败：清理引用并在状态栏提示。"""
        self._similar_worker = None
        self.set_status(f"相似查询失败: {message}")

    def _on_view_similar(self, comic: ComicItem):
        """右键「查看类似作品」：列表切换为只显示该作品与本地库相似作品。"""
        from ui.database import AID_RE, _cover_hash, find_similar_records

        # 封面哈希（已下载时）参与封面信号，与卡片角标口径一致
        match = AID_RE.search(comic.detail_url or "")
        cover_hash = _cover_hash(match.group(1)) if match else None
        records = find_similar_records(
            comic.detail_url,
            comic.title,
            comic.translated_title,
            cover_hash,
        )
        if not records:
            self.set_status("本地数据库没有找到相似作品")
            return
        self._similar_prev_comics = self._comics
        self.set_comics(self._comic_items_for_similar(comic, records))
        self._similar_mode = True
        self.similar_banner_label.setText(
            f"正在查看「{comic.display_title}」及 {len(records)} 部相似作品"
        )
        self.similar_banner.setVisible(True)
        self.set_status(
            f"相似作品：{comic.display_title}（含其本身共 {len(records) + 1} 部）"
        )

    def _comic_items_for_similar(
        self, source: ComicItem, records: list[dict]
    ) -> list[ComicItem]:
        """构造相似视图列表：源作品置顶，库内相似记录按 aid 倒序排列。

        源作品角标与相似记录保持一致（count=len(records)，其中有已下载
        时显示红色），避免进入相似视图后源卡片角标与右键菜单口径不一致。
        """
        from ui.database import cover_cache_path

        others = []
        for rec in records:
            detail_url = rec.get("detail_url", "")
            others.append(
                ComicItem(
                    title=rec.get("title", "未知"),
                    image_count=rec.get("image_count", 0),
                    upload_date=rec.get("date", ""),
                    detail_url=detail_url,
                    cover_url=rec.get("cover_url", ""),
                    cover_path=cover_cache_path(detail_url),
                    category=rec.get("category", ""),
                    category_label=rec.get("category_label", ""),
                    translated_title=rec.get("translated_title", ""),
                )
            )

        def _aid(item: ComicItem) -> int:
            match = re.search(r"aid-(\d+)", item.detail_url or "")
            return int(match.group(1)) if match else 0

        others.sort(key=_aid, reverse=True)
        source.similar_count = len(records)
        source.similar_downloaded = any(r.get("downloaded") for r in records)
        return [source, *others]

    def _exit_similar_mode(self, restore: bool = True):
        """退出相似模式。

        Args:
            restore: 为 True 时恢复进入前的作品列表（banner「返回列表」）；
                为 False 时仅隐藏 banner（抓取/翻页等即将产生新列表）。
        """
        if not self._similar_mode:
            return
        self._similar_mode = False
        self.similar_banner.setVisible(False)
        if restore and self._similar_prev_comics is not None:
            prev = self._similar_prev_comics
            self._similar_prev_comics = None
            self.set_comics(prev)

    def _run_pending_request(self):
        """当前 worker 结束后，若期间有排队请求则立即执行。"""
        if self._pending_request is not None:
            silent, page = self._pending_request
            self._pending_request = None
            self._start_fetch(silent=silent, page=page)

    # ---- 工具栏回调 ----

    def _on_refresh(self):
        """刷新按钮回调：重新抓取当前模式的当前页数据。"""
        self._start_fetch(silent=False)

    def _on_download_manager(self):
        """打开下载管理对话框（显示任务 ID、进度与状态）。"""
        from ui.dialogs.download_manager_dialog import DownloadManagerDialog

        dialog = DownloadManagerDialog(self)
        dialog.exec()

    def _on_settings(self):
        """打开设置对话框，保存后刷新界面以应用繁简转换。"""
        dialog = SettingsDialog(self)
        if dialog.exec() == SettingsDialog.Accepted:
            self.comics_grid.render(self._comics)
            self.set_status("设置已保存")

    # ---- 窗口事件 ----

    def showEvent(self, event):
        """窗口显示时按真实宽度重排（不重建卡片）。

        首次数据加载发生在窗口显示前，viewport 宽度尚小，
        列数会被算成 1 导致卡片单列排列；显示后再重排恢复多列。
        """
        super().showEvent(event)
        self.comics_grid.reflow()

    def resizeEvent(self, event):
        """窗口大小变化时重排卡片列数（仅移动卡片，不重建）。"""
        super().resizeEvent(event)
        self.comics_grid.reflow()

    def closeEvent(self, event):
        """关闭窗口（即退出程序）时快速收尾后台线程：

        1. 抓取线程：引用由全局线程注册表持有（ui/threads.py），
           应用退出时统一中断并限时强制终止，此处不阻塞；
        2. 下载线程：此处仅请求中断（不等待），让线程在下一个检查点
           退出；最终限时收尾由应用退出流程（aboutToQuit → shutdown）
           完成，关闭窗口不再同步阻塞 2.5s（写文件为原子替换，
           中断/强制终止也不会损坏数据）。
        """
        self._worker = None
        DownloadManager.instance().request_interruption()
        super().closeEvent(event)
