"""漫画浏览器主窗口。

负责顶部工具栏（搜索/分类/按钮）、内容区与翻页栏的组装，
以及列表浏览与关键词搜索两种模式的切换调度。
"""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
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
from ui.fetch_worker import FetchWorker
from ui.models import ComicItem
from ui.settings import AppSettings, BASE_URL, CATEGORIES, ProjectConfig
from ui.styles import LIGHT_THEME_QSS
from ui.widgets import ComicsGrid, PageBar
from ui import __version__


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

        # 加载进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumWidth(120)
        self.progress_bar.setMaximumHeight(6)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        toolbar_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("statusLabel")
        toolbar_layout.addWidget(self.status_label)

        root_layout.addWidget(toolbar)

        # 内容区（漫画网格）与底部翻页栏
        self.comics_grid = ComicsGrid()
        root_layout.addWidget(self.comics_grid)

        self.page_bar = PageBar()
        self.page_bar.page_requested.connect(self._on_page_requested)
        root_layout.addWidget(self.page_bar)

    # ---- 数据加载 ----

    def _load_initial_data(self):
        """启动时加载数据：优先数据库，其次 JSON 缓存，否则示例数据。

        数据库与缓存均为本地数据，无需网络请求；若开启自动抓取则在后台刷新。
        """
        from ui.database import DatabaseManager

        records = DatabaseManager.instance().load_comics()
        if records:
            self.set_comics_from_raw(records)
            self.status_label.setText(f"已从数据库加载，共 {len(records)} 部漫画")
        else:
            cached = load_cache()
            if cached:
                # 数据库为空时，将旧 JSON 缓存迁移到数据库
                DatabaseManager.instance().save_comics(cached)
                self.set_comics_from_raw(cached)
                self.status_label.setText(f"已加载本地缓存，共 {len(cached)} 部漫画")
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
        self.status_label.setText(f"共 {len(comics)} 部漫画")

    def set_comics_from_raw(self, raw_comics: list[dict]):
        """将爬虫原始字典转换为 ComicItem 并展示（自动关联本地封面缓存）。"""
        from ui.database import cover_cache_path

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
        import re

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

        Args:
            silent: 为 True 时出错不弹窗，仅在状态栏提示（用于启动自动刷新）。
            page: 要抓取的页码，默认使用当前页码。
        """
        if self._worker and self._worker.isRunning():
            return

        if page is not None and page >= 1:
            self._current_page = page
            self.page_bar.set_page(page)

        self._silent_fetch = silent
        if self._search_keyword:
            self.status_label.setText(
                f"正在搜索“{self._search_keyword}”第 {self._current_page} 页..."
            )
        else:
            self.status_label.setText(f"正在抓取第 {self._current_page} 页...")
        self.progress_bar.setVisible(True)

        self._worker = FetchWorker(
            url=self._project_config.list_url,
            page=self._current_page,
            keyword=self._search_keyword,
        )
        self._worker.finished.connect(self._on_fetch_finished)
        self._worker.error.connect(self._on_fetch_error)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_page_requested(self, page: int):
        """翻页栏请求跳转回调。"""
        if page >= 1:
            self._start_fetch(page=page)

    def _on_fetch_finished(self, raw_comics: list[dict]):
        self.progress_bar.setVisible(False)
        if self._search_keyword:
            # 搜索结果仅展示，不写入缓存与数据库（避免污染下次启动的本地数据）
            self.set_comics_from_raw(raw_comics)
            self.status_label.setText(
                f"搜索“{self._search_keyword}”：第 {self._current_page} 页，"
                f"共 {len(raw_comics)} 部漫画"
            )
        else:
            save_cache(raw_comics)
            # 抓取结果写入数据库，供下次启动直接加载
            from ui.database import DatabaseManager

            DatabaseManager.instance().save_comics(raw_comics)
            self.set_comics_from_raw(raw_comics)
            self.status_label.setText(
                f"第 {self._current_page} 页：共 {len(raw_comics)} 部漫画"
            )
        self._worker = None

    def _on_fetch_error(self, message: str):
        self.progress_bar.setVisible(False)
        if self._silent_fetch:
            self.status_label.setText(f"自动抓取失败: {message}")
        else:
            QMessageBox.critical(self, "抓取失败", f"抓取过程中出现错误:\n{message}")
            self.status_label.setText("抓取失败")
        self._worker = None

    # ---- 工具栏回调 ----

    def _on_refresh(self):
        """刷新按钮回调：重新抓取当前模式的当前页数据。"""
        self._start_fetch(silent=False)

    def _on_download_manager(self):
        """打开下载管理对话框（显示任务 ID、进度与状态）。"""
        from ui.download_manager import DownloadManagerDialog

        dialog = DownloadManagerDialog(self)
        dialog.exec()

    def _on_settings(self):
        """打开设置对话框，保存后刷新界面以应用繁简转换。"""
        dialog = SettingsDialog(self)
        if dialog.exec() == SettingsDialog.Accepted:
            self.comics_grid.render(self._comics)
            self.status_label.setText("设置已保存")

    # ---- 窗口事件 ----

    def resizeEvent(self, event):
        """窗口大小变化时重新布局卡片列数。"""
        super().resizeEvent(event)
        if self._comics:
            self.comics_grid.render(self._comics)

    def closeEvent(self, event):
        """关闭窗口时等待后台抓取线程结束，避免销毁仍在运行的线程。"""
        if self._worker and self._worker.isRunning():
            self._worker.wait()
        super().closeEvent(event)
