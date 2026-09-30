from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ui.settings import AppSettings, ProjectConfig
from ui.styles import PALETTE

_NAV_QSS = f"""
QListWidget#settingsNav {{
    background-color: {PALETTE["surface"]};
    border: 1px solid {PALETTE["border"]};
    border-radius: 8px;
    padding: 4px;
    outline: none;
    font-size: 14px;
}}
QListWidget#settingsNav::item {{
    border-radius: 6px;
    padding: 10px 12px;
    margin: 2px;
    color: {PALETTE["text"]};
}}
QListWidget#settingsNav::item:hover {{
    background-color: {PALETTE["surface_hover"]};
}}
QListWidget#settingsNav::item:selected {{
    background-color: {PALETTE["primary"]};
    color: white;
}}
"""


class SettingsDialog(QDialog):
    """应用设置对话框：左侧设置项导航，右侧对应详情面板。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumSize(700, 520)
        self._settings = AppSettings()
        self._project_config = ProjectConfig()
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("设置")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        # 主体：左导航 + 右面板
        body = QHBoxLayout()
        body.setSpacing(16)

        self.nav_list = QListWidget()
        self.nav_list.setObjectName("settingsNav")
        self.nav_list.setFixedWidth(150)
        self.nav_list.setStyleSheet(_NAV_QSS)
        for name in ("行为偏好", "名称翻译", "项目配置", "数据库管理"):
            item = QListWidgetItem(name)
            item.setSizeHint(QSize(0, 42))
            self.nav_list.addItem(item)
        self.nav_list.setCurrentRow(0)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_behavior_page())
        self.stack.addWidget(self._build_translate_page())
        self.stack.addWidget(self._build_config_page())
        self.stack.addWidget(self._build_database_page())

        body.addWidget(self.nav_list)
        body.addWidget(self.stack, 1)
        layout.addLayout(body, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # 左侧选中项驱动右侧面板切换
        self.nav_list.currentRowChanged.connect(self.stack.setCurrentIndex)

    # ---- 右侧各面板 ----

    def _build_behavior_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(12)

        heading = QLabel("行为偏好")
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        v.addWidget(heading)

        self.convert_checkbox = QCheckBox("将繁体字转换为简体字")
        self.convert_checkbox.setChecked(
            self._settings.convert_traditional_to_simplified
        )
        v.addWidget(self.convert_checkbox)

        convert_hint = QLabel("开启后，漫画标题、分类标签等文本会自动显示为简体。")
        convert_hint.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 12px;"
        )
        convert_hint.setIndent(22)
        v.addWidget(convert_hint)

        self.auto_fetch_checkbox = QCheckBox("启动时自动抓取最新数据")
        self.auto_fetch_checkbox.setChecked(
            self._settings.auto_fetch_on_startup
        )
        v.addWidget(self.auto_fetch_checkbox)

        auto_hint = QLabel("开启后，打开应用会自动从网站拉取最新漫画列表并缓存到本地。")
        auto_hint.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 12px;"
        )
        auto_hint.setIndent(22)
        v.addWidget(auto_hint)

        self.notify_checkbox = QCheckBox("下载完成后显示系统通知")
        self.notify_checkbox.setChecked(
            self._settings.notify_on_download_done
        )
        v.addWidget(self.notify_checkbox)

        notify_hint = QLabel(
            "开启后，下载完成时通过系统托盘气泡提示；关闭窗口后应用驻留"
            "托盘继续后台下载，可在设置中随时关闭此提示。"
        )
        notify_hint.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 12px;"
        )
        notify_hint.setIndent(22)
        notify_hint.setWordWrap(True)
        v.addWidget(notify_hint)

        # ---- 在线阅读器 ----
        reader_heading = QLabel("在线阅读器")
        reader_heading.setStyleSheet("font-size: 15px; font-weight: 600; margin-top: 8px;")
        v.addWidget(reader_heading)

        self.reader_prefetch_checkbox = QCheckBox("阅读时后台预取相邻页（默认关闭）")
        self.reader_prefetch_checkbox.setChecked(self._settings.reader_prefetch_enabled)
        v.addWidget(self.reader_prefetch_checkbox)

        prefetch_hint = QLabel(
            "默认关闭：只在点「下一页/上一页」时获取当前这一页，最省请求。"
            "开启后会提前缓存前后几页，翻页更顺滑，但请求更密。"
        )
        prefetch_hint.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 12px;"
        )
        prefetch_hint.setIndent(22)
        prefetch_hint.setWordWrap(True)
        v.addWidget(prefetch_hint)

        prefetch_row = QHBoxLayout()
        prefetch_row.setContentsMargins(22, 0, 0, 0)
        prefetch_row.setSpacing(8)
        prefetch_row.addWidget(QLabel("向前预取"))
        self.reader_behind_spin = QSpinBox()
        self.reader_behind_spin.setRange(0, 10)
        self.reader_behind_spin.setValue(self._settings.reader_prefetch_behind)
        self.reader_behind_spin.setSuffix(" 页")
        self.reader_behind_spin.setFixedWidth(80)
        prefetch_row.addWidget(self.reader_behind_spin)
        prefetch_row.addSpacing(12)
        prefetch_row.addWidget(QLabel("向后预取"))
        self.reader_ahead_spin = QSpinBox()
        self.reader_ahead_spin.setRange(0, 10)
        self.reader_ahead_spin.setValue(self._settings.reader_prefetch_ahead)
        self.reader_ahead_spin.setSuffix(" 页")
        self.reader_ahead_spin.setFixedWidth(80)
        prefetch_row.addWidget(self.reader_ahead_spin)
        prefetch_row.addStretch()
        v.addLayout(prefetch_row)

        self.reader_resume_checkbox = QCheckBox("打开作品时续读到上次位置")
        self.reader_resume_checkbox.setChecked(self._settings.reader_resume)
        v.addWidget(self.reader_resume_checkbox)

        v.addStretch()
        return page

    def _build_translate_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(12)

        heading = QLabel("名称翻译")
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        v.addWidget(heading)

        desc = QLabel(
            "使用 AI 接口将作品名称翻译为简体中文（支持 DeepSeek 等 "
            "OpenAI 兼容接口）。配置接口链接、模型与 Key 后，卡片右键菜单"
            "即可翻译作品名称。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 13px;"
        )
        v.addWidget(desc)

        self.translate_btn = QPushButton("打开名称翻译设置")
        self.translate_btn.setObjectName("settingsButton")
        self.translate_btn.clicked.connect(self._open_translate_settings)
        v.addWidget(self.translate_btn)

        v.addStretch()
        return page

    def _build_config_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(12)

        heading = QLabel("项目配置")
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        v.addWidget(heading)

        config_hint = QLabel("修改后点击保存会写入 Config/Config.json，供爬虫与下载功能使用。")
        config_hint.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 12px;"
        )
        v.addWidget(config_hint)

        form = QFormLayout()
        form.setSpacing(12)

        self.list_url_edit = QLineEdit(self._project_config.list_url)
        self.list_url_edit.setPlaceholderText("https://www.wnacg.com/albums.html")
        form.addRow("列表页 URL:", self.list_url_edit)

        self.page_number_spin = QSpinBox()
        self.page_number_spin.setRange(1, 999)
        self.page_number_spin.setValue(self._project_config.page_number)
        form.addRow("默认页数:", self.page_number_spin)

        self.download_dir_edit = QLineEdit(self._project_config.download_dir)
        self.download_dir_edit.setPlaceholderText("./Download")
        form.addRow("下载目录:", self.download_dir_edit)

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 10)
        self.workers_spin.setValue(self._project_config.download_workers)
        self.workers_spin.setToolTip("并发过高容易触发站点反爬限制，建议 2~5")
        form.addRow("下载并发数:", self.workers_spin)

        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 10)
        self.retries_spin.setValue(self._project_config.download_retries)
        self.retries_spin.setToolTip("下载失败时的重试次数（含指数退避等待）")
        form.addRow("失败重试次数:", self.retries_spin)

        self.label_checkbox = QCheckBox("文件夹名包含右上角分类标签")
        self.label_checkbox.setChecked(
            self._project_config.folder_use_category_label
        )
        self.label_checkbox.setToolTip("开启后下载文件夹名格式为 [分类标签] 标题")
        form.addRow(self.label_checkbox)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["图片下载（逐张）", "压缩包下载（整包自动解压）"])
        self.mode_combo.setCurrentIndex(
            1 if self._project_config.download_mode == "zip" else 0
        )
        self.mode_combo.setToolTip(
            "图片下载：逐张请求图片直链下载；压缩包下载：下载整包 zip 后自动解压，"
            "文件名为 [分类标签] 标题"
        )
        form.addRow("下载方式:", self.mode_combo)

        v.addLayout(form)
        v.addStretch()
        return page

    def _build_database_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(12)

        heading = QLabel("数据库管理")
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        v.addWidget(heading)

        desc = QLabel(
            "管理本地漫画数据库与封面缓存：查看存储位置、切换存储路径、"
            "清除缓存或清空数据库。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 13px;"
        )
        v.addWidget(desc)

        # 存储路径（当前生效位置，更改后即时刷新）
        path_group = QGroupBox("存储路径")
        path_form = QVBoxLayout(path_group)
        path_form.setSpacing(8)
        self.db_path_label = QLabel()
        self.db_path_label.setWordWrap(True)
        self.db_path_label.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 12px;"
        )
        path_form.addWidget(self.db_path_label)
        self.covers_path_label = QLabel()
        self.covers_path_label.setWordWrap(True)
        self.covers_path_label.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 12px;"
        )
        path_form.addWidget(self.covers_path_label)
        self.storage_btn = QPushButton("更改路径…")
        self.storage_btn.setObjectName("settingsButton")
        self.storage_btn.setFixedWidth(120)
        self.storage_btn.clicked.connect(self._change_storage_paths)
        path_form.addWidget(self.storage_btn, alignment=Qt.AlignLeft)
        v.addWidget(path_group)

        # 清除操作
        clear_group = QGroupBox("清除")
        clear_layout = QHBoxLayout(clear_group)
        clear_layout.setSpacing(12)
        self.clear_covers_btn = QPushButton("清除封面缓存")
        self.clear_covers_btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {PALETTE["surface"]};
                color: #DC2626;
                border: 1px solid #FCA5A5;
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: #FEF2F2;
            }}
            """
        )
        self.clear_covers_btn.clicked.connect(self._clear_cover_cache)
        clear_layout.addWidget(self.clear_covers_btn)
        self.clear_comics_btn = QPushButton("清空数据库")
        self.clear_comics_btn.setStyleSheet(self.clear_covers_btn.styleSheet())
        self.clear_comics_btn.clicked.connect(self._clear_comics)
        clear_layout.addWidget(self.clear_comics_btn)
        clear_layout.addStretch()
        v.addWidget(clear_group)

        # 完整管理入口（表格 / 查重等）
        self.db_btn = QPushButton("打开数据库管理（记录表格 / 查重）")
        self.db_btn.setObjectName("settingsButton")
        self.db_btn.clicked.connect(self._open_database_dialog)
        v.addWidget(self.db_btn)

        v.addStretch()

        self._refresh_db_paths()
        return page

    # ---- 数据库管理面板操作 ----

    def _refresh_db_paths(self):
        """刷新当前生效的数据库与封面缓存路径显示。"""
        from ui.database import COVERS_DIR, DB_PATH

        self.db_path_label.setText(f"数据库：{DB_PATH}")
        self.covers_path_label.setText(f"封面缓存：{COVERS_DIR}")

    def _change_storage_paths(self):
        """打开存储路径设置；切换后刷新路径显示。"""
        from ui.dialogs.storage_dialog import StoragePathsDialog

        if StoragePathsDialog(self).exec() == QDialog.Accepted:
            self._refresh_db_paths()

    def _clear_cover_cache(self):
        """确认后清除所有封面缓存图片。"""
        from ui.database import DatabaseManager

        answer = QMessageBox.question(
            self,
            "确认清除",
            "确定要清除所有封面缓存图片吗？\n下次浏览时将重新从网络加载。",
        )
        if answer == QMessageBox.Yes:
            DatabaseManager.instance().clear_covers()
            self._refresh_db_paths()
            QMessageBox.information(self, "完成", "封面缓存已清除。")

    def _clear_comics(self):
        """确认后清空数据库中的全部漫画记录。"""
        from ui.database import DatabaseManager

        answer = QMessageBox.question(
            self,
            "确认清空",
            "确定要清空数据库中的所有漫画记录吗？\n此操作不可撤销。",
        )
        if answer == QMessageBox.Yes:
            DatabaseManager.instance().clear_comics()
            self._refresh_db_paths()
            QMessageBox.information(self, "完成", "数据库已清空。")

    # ---- 入口与保存 ----

    def _open_database_dialog(self):
        """打开数据库管理窗口。"""
        from ui.dialogs.database_dialog import DatabaseDialog

        DatabaseDialog(self).exec()

    def _open_translate_settings(self):
        """打开名称翻译设置窗口。"""
        from ui.dialogs.translate_settings_dialog import TranslateSettingsDialog

        TranslateSettingsDialog(self).exec()

    def _on_save(self):
        # 保存应用设置
        self._settings.convert_traditional_to_simplified = (
            self.convert_checkbox.isChecked()
        )
        self._settings.auto_fetch_on_startup = self.auto_fetch_checkbox.isChecked()
        self._settings.notify_on_download_done = self.notify_checkbox.isChecked()
        # 在线阅读器设置
        self._settings.reader_prefetch_enabled = (
            self.reader_prefetch_checkbox.isChecked()
        )
        self._settings.reader_prefetch_behind = self.reader_behind_spin.value()
        self._settings.reader_prefetch_ahead = self.reader_ahead_spin.value()
        self._settings.reader_resume = self.reader_resume_checkbox.isChecked()
        self._settings.save()

        # 保存项目配置
        self._project_config.list_url = self.list_url_edit.text()
        self._project_config.page_number = self.page_number_spin.value()
        self._project_config.download_dir = self.download_dir_edit.text()
        self._project_config.download_workers = self.workers_spin.value()
        self._project_config.download_retries = self.retries_spin.value()
        self._project_config.folder_use_category_label = (
            self.label_checkbox.isChecked()
        )
        self._project_config.download_mode = (
            "zip" if self.mode_combo.currentIndex() == 1 else "image"
        )
        self._project_config.save()

        self.accept()
