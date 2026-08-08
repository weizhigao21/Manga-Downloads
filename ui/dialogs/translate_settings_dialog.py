"""名称翻译设置对话框：配置 AI 翻译接口（链接/模型/Key）。

配置保存在 Config/settings.json（应用本地设置），Key 仅存本地文件。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ui.settings import AppSettings
from ui.styles import PALETTE


class TranslateSettingsDialog(QDialog):
    """名称翻译设置对话框。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("名称翻译设置")
        self.setMinimumWidth(460)
        self._settings = AppSettings()
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("名称翻译设置")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        desc = QLabel(
            "使用 AI 接口将作品名称翻译为简体中文（支持 DeepSeek 等 "
            "OpenAI 兼容接口）。需先安装：pip install openai"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 13px;"
        )
        layout.addWidget(desc)

        config_group = QGroupBox("接口配置")
        form = QFormLayout(config_group)
        form.setSpacing(12)

        self.api_base_edit = QLineEdit(self._settings.translate_api_base)
        self.api_base_edit.setPlaceholderText("https://api.deepseek.com")
        form.addRow("接口链接:", self.api_base_edit)

        self.model_edit = QLineEdit(self._settings.translate_model)
        self.model_edit.setPlaceholderText("deepseek-v4-pro")
        form.addRow("模型:", self.model_edit)

        # Key 默认掩码显示，可勾选查看
        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        self.key_edit = QLineEdit(self._settings.translate_api_key)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-...")
        key_row.addWidget(self.key_edit, stretch=1)
        self.show_key_checkbox = QCheckBox("显示")
        self.show_key_checkbox.toggled.connect(self._on_toggle_key_visible)
        key_row.addWidget(self.show_key_checkbox)
        form.addRow("API Key:", key_row)

        key_hint = QLabel("Key 仅保存在本地 Config/settings.json，不会上传。")
        key_hint.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 12px;"
        )
        form.addRow(key_hint)

        layout.addWidget(config_group)
        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_toggle_key_visible(self, visible: bool):
        self.key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )

    def _on_save(self):
        self._settings.translate_api_base = self.api_base_edit.text()
        self._settings.translate_model = self.model_edit.text()
        self._settings.translate_api_key = self.key_edit.text()
        self._settings.save()
        self.accept()
