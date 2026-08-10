"""存储路径设置对话框：切换数据库文件与封面缓存目录（可选迁移现有数据）。"""
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.database import COVERS_DIR, DB_PATH, switch_storage_paths
from ui.styles import PALETTE


class StoragePathsDialog(QDialog):
    """路径设置窗口：设置漫画数据库文件与封面缓存目录的存放位置。

    切换时可将现有数据库与封面缓存复制到新路径（目标已存在同名文件
    不覆盖）；旧路径文件保留，由用户决定是否手动清理。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("存储路径设置")
        self.resize(560, 300)
        self._setup_ui()
        self._load_current()

    def _setup_ui(self):
        self.setStyleSheet(
            f"""
            QDialog {{
                background-color: {PALETTE["background"]};
            }}
            QLabel {{
                color: {PALETTE["text"]};
                font-size: 13px;
            }}
            QLabel#hint {{
                color: {PALETTE["text_secondary"]};
                font-size: 12px;
            }}
            QLineEdit {{
                background-color: {PALETTE["surface"]};
                color: {PALETTE["text"]};
                border: 1px solid {PALETTE["border"]};
                border-radius: 6px;
                padding: 6px 8px;
                font-size: 13px;
            }}
            QPushButton {{
                background-color: {PALETTE["surface"]};
                color: {PALETTE["text"]};
                border: 1px solid {PALETTE["border"]};
                border-radius: 8px;
                padding: 6px 14px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {PALETTE["surface_hover"]};
            }}
            QCheckBox {{
                color: {PALETTE["text"]};
                font-size: 13px;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = QLabel("存储路径设置")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        # 数据库文件路径
        db_row = QHBoxLayout()
        db_label = QLabel("数据库文件：")
        db_label.setFixedWidth(90)
        db_row.addWidget(db_label)
        self.db_edit = QLineEdit()
        self.db_edit.setPlaceholderText("例如 D:/漫画数据/comics.db（留空恢复默认）")
        db_row.addWidget(self.db_edit, 1)
        db_btn = QPushButton("浏览…")
        db_btn.clicked.connect(self._browse_db)
        db_row.addWidget(db_btn)
        layout.addLayout(db_row)

        # 封面缓存目录
        covers_row = QHBoxLayout()
        covers_label = QLabel("封面缓存目录：")
        covers_label.setFixedWidth(90)
        covers_row.addWidget(covers_label)
        self.covers_edit = QLineEdit()
        self.covers_edit.setPlaceholderText("例如 D:/漫画数据/covers（留空恢复默认）")
        covers_row.addWidget(self.covers_edit, 1)
        covers_btn = QPushButton("浏览…")
        covers_btn.clicked.connect(self._browse_covers)
        covers_row.addWidget(covers_btn)
        layout.addLayout(covers_row)

        # 迁移选项
        self.migrate_check = QCheckBox("将现有数据库与封面缓存复制到新路径")
        self.migrate_check.setChecked(True)
        layout.addWidget(self.migrate_check)

        hint = QLabel(
            "切换后立即生效，无需重启；新路径存在同名文件时不会覆盖。\n"
            "旧路径的文件会保留（不迁移时新库从空开始），可自行清理。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_current(self):
        self.db_edit.setText(str(DB_PATH))
        self.covers_edit.setText(str(COVERS_DIR))

    def _browse_db(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择数据库文件位置",
            str(DB_PATH),
            "SQLite 数据库 (*.db);;所有文件 (*)",
        )
        if path:
            self.db_edit.setText(path)

    def _browse_covers(self):
        path = QFileDialog.getExistingDirectory(
            self, "选择封面缓存目录", str(COVERS_DIR)
        )
        if path:
            self.covers_edit.setText(path)

    def _on_apply(self):
        db_text = self.db_edit.text().strip()
        covers_text = self.covers_edit.text().strip()
        if not db_text or not covers_text:
            QMessageBox.warning(
                self, "路径无效", "数据库文件与封面缓存目录都不能为空。"
            )
            return
        new_db = Path(db_text).expanduser()
        new_covers = Path(covers_text).expanduser()
        if new_db.exists() and new_db.is_dir():
            QMessageBox.warning(
                self, "路径无效", "数据库文件路径指向了一个目录，请选择文件路径。"
            )
            return
        if new_covers.exists() and not new_covers.is_dir():
            QMessageBox.warning(
                self, "路径无效", "封面缓存目录路径指向了一个文件，请选择目录。"
            )
            return
        if new_db == DB_PATH and new_covers == COVERS_DIR:
            self.accept()
            return

        try:
            switch_storage_paths(
                new_db, new_covers, migrate=self.migrate_check.isChecked()
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法切换路径", str(exc))
            return
        QMessageBox.information(self, "完成", "存储路径已切换。")
        self.accept()
