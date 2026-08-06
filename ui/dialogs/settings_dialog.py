from PySide6.QtCore import Qt
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
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.settings import AppSettings, ProjectConfig
from ui.styles import PALETTE


class SettingsDialog(QDialog):
    """应用设置对话框。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(420)
        self._settings = AppSettings()
        self._project_config = ProjectConfig()
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("设置")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        desc = QLabel("调整界面显示、行为偏好与项目配置。")
        desc.setStyleSheet(f"color: {PALETTE['text_secondary']}; font-size: 13px;")
        layout.addWidget(desc)

        # 行为偏好分组
        behavior_group = QGroupBox("行为偏好")
        behavior_layout = QVBoxLayout(behavior_group)
        behavior_layout.setSpacing(12)

        self.convert_checkbox = QCheckBox("将繁体字转换为简体字")
        self.convert_checkbox.setChecked(
            self._settings.convert_traditional_to_simplified
        )
        self.convert_checkbox.setStyleSheet("font-size: 14px;")
        behavior_layout.addWidget(self.convert_checkbox)

        convert_hint = QLabel("开启后，漫画标题、分类标签等文本会自动显示为简体。")
        convert_hint.setStyleSheet(f"color: {PALETTE['text_secondary']}; font-size: 12px; padding-left: 22px;")
        behavior_layout.addWidget(convert_hint)

        self.auto_fetch_checkbox = QCheckBox("启动时自动抓取最新数据")
        self.auto_fetch_checkbox.setChecked(
            self._settings.auto_fetch_on_startup
        )
        self.auto_fetch_checkbox.setStyleSheet("font-size: 14px;")
        behavior_layout.addWidget(self.auto_fetch_checkbox)

        auto_hint = QLabel("开启后，打开应用会自动从网站拉取最新漫画列表并缓存到本地。")
        auto_hint.setStyleSheet(f"color: {PALETTE['text_secondary']}; font-size: 12px; padding-left: 22px;")
        behavior_layout.addWidget(auto_hint)

        layout.addWidget(behavior_group)

        # 项目配置分组
        config_group = QGroupBox("项目配置 (Config/Config.json)")
        config_layout = QFormLayout(config_group)
        config_layout.setSpacing(12)

        self.list_url_edit = QLineEdit(self._project_config.list_url)
        self.list_url_edit.setPlaceholderText("https://www.wnacg.com/albums.html")
        config_layout.addRow("列表页 URL:", self.list_url_edit)

        self.page_number_spin = QSpinBox()
        self.page_number_spin.setRange(1, 999)
        self.page_number_spin.setValue(self._project_config.page_number)
        config_layout.addRow("默认页数:", self.page_number_spin)

        self.download_dir_edit = QLineEdit(self._project_config.download_dir)
        self.download_dir_edit.setPlaceholderText("./Download")
        config_layout.addRow("下载目录:", self.download_dir_edit)

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 10)
        self.workers_spin.setValue(self._project_config.download_workers)
        self.workers_spin.setToolTip("并发过高容易触发站点反爬限制，建议 2~5")
        config_layout.addRow("下载并发数:", self.workers_spin)

        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 10)
        self.retries_spin.setValue(self._project_config.download_retries)
        self.retries_spin.setToolTip("下载失败时的重试次数（含指数退避等待）")
        config_layout.addRow("失败重试次数:", self.retries_spin)

        self.label_checkbox = QCheckBox("文件夹名包含右上角分类标签")
        self.label_checkbox.setChecked(self._project_config.folder_use_category_label)
        self.label_checkbox.setToolTip("开启后下载文件夹名格式为 [分类标签] 标题")
        config_layout.addRow(self.label_checkbox)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["图片下载（逐张）", "压缩包下载（整包自动解压）"])
        self.mode_combo.setCurrentIndex(
            1 if self._project_config.download_mode == "zip" else 0
        )
        self.mode_combo.setToolTip(
            "图片下载：逐张请求图片直链下载；压缩包下载：下载整包 zip 后自动解压，"
            "文件名为 [分类标签] 标题"
        )
        config_layout.addRow("下载方式:", self.mode_combo)

        config_hint = QLabel("修改后点击保存会写入 Config/Config.json，供爬虫与下载功能使用。")
        config_hint.setStyleSheet(f"color: {PALETTE['text_secondary']}; font-size: 12px;")
        config_layout.addRow(config_hint)

        layout.addWidget(config_group)
        layout.addStretch()

        # 数据库管理按钮
        db_row = QHBoxLayout()
        self.db_btn = QPushButton("数据库管理")
        self.db_btn.setObjectName("settingsButton")
        self.db_btn.clicked.connect(self._open_database_dialog)
        db_row.addWidget(self.db_btn)
        db_row.addStretch()
        layout.addLayout(db_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _open_database_dialog(self):
        """打开数据库管理窗口。"""
        from ui.database import DatabaseDialog

        DatabaseDialog(self).exec()

    def _on_save(self):
        # 保存应用设置
        self._settings.convert_traditional_to_simplified = self.convert_checkbox.isChecked()
        self._settings.auto_fetch_on_startup = self.auto_fetch_checkbox.isChecked()
        self._settings.save()

        # 保存项目配置
        self._project_config.list_url = self.list_url_edit.text()
        self._project_config.page_number = self.page_number_spin.value()
        self._project_config.download_dir = self.download_dir_edit.text()
        self._project_config.download_workers = self.workers_spin.value()
        self._project_config.download_retries = self.retries_spin.value()
        self._project_config.folder_use_category_label = self.label_checkbox.isChecked()
        self._project_config.download_mode = (
            "zip" if self.mode_combo.currentIndex() == 1 else "image"
        )
        self._project_config.save()

        self.accept()
