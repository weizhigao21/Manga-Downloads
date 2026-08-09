"""数据库管理对话框：表格展示漫画记录（ID/名称/日期/张数/是否下载）与统计。"""
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.database import (
    AID_RE,
    DatabaseManager,
    format_size,
    is_comic_downloaded,
)
from ui.styles import PALETTE


class DatabaseDialog(QDialog):
    """数据库管理窗口：表格展示漫画记录（ID/名称/日期/张数/是否下载）与统计。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("数据库管理")
        self.resize(820, 520)
        self._db = DatabaseManager()
        self._setup_ui()
        self._refresh()

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
            QLabel#statValue {{
                font-weight: 600;
                color: {PALETTE["primary"]};
            }}
            QTableWidget {{
                background-color: {PALETTE["surface"]};
                alternate-background-color: {PALETTE["surface_hover"]};
                color: {PALETTE["text"]};
                border: 1px solid {PALETTE["border"]};
                border-radius: 8px;
                gridline-color: {PALETTE["border"]};
                font-size: 13px;
            }}
            QTableWidget::item {{
                padding: 6px;
            }}
            QHeaderView::section {{
                background-color: {PALETTE["surface_hover"]};
                color: {PALETTE["text"]};
                border: none;
                border-bottom: 1px solid {PALETTE["border"]};
                padding: 8px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton {{
                background-color: {PALETTE["surface"]};
                color: {PALETTE["text"]};
                border: 1px solid {PALETTE["border"]};
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {PALETTE["surface_hover"]};
            }}
            QPushButton#dangerButton {{
                color: #DC2626;
                border: 1px solid #FCA5A5;
            }}
            QPushButton#dangerButton:hover {{
                background-color: #FEF2F2;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("数据库管理")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        # 统计信息（单行紧凑展示）
        stats_row = QHBoxLayout()
        stats_row.setSpacing(24)

        self.comics_value = QLabel("-")
        self.comics_value.setObjectName("statValue")
        stats_row.addWidget(self._stat_block("漫画记录", self.comics_value))

        self.db_size_value = QLabel("-")
        self.db_size_value.setObjectName("statValue")
        stats_row.addWidget(self._stat_block("数据库大小", self.db_size_value))

        self.cover_count_value = QLabel("-")
        self.cover_count_value.setObjectName("statValue")
        stats_row.addWidget(self._stat_block("封面缓存", self.cover_count_value))

        stats_row.addStretch()
        layout.addLayout(stats_row)

        # 记录表格
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["ID", "名称", "日期", "张数", "是否下载", "下载完成时间"]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        # 操作按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self._refresh)
        btn_row.addWidget(self.refresh_btn)

        self.clear_comics_btn = QPushButton("清空数据库")
        self.clear_comics_btn.setObjectName("dangerButton")
        self.clear_comics_btn.clicked.connect(self._on_clear_comics)
        btn_row.addWidget(self.clear_comics_btn)

        self.clear_covers_btn = QPushButton("清除封面缓存")
        self.clear_covers_btn.setObjectName("dangerButton")
        self.clear_covers_btn.clicked.connect(self._on_clear_covers)
        btn_row.addWidget(self.clear_covers_btn)

        btn_row.addStretch()

        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("settingsButton")
        self.close_btn.setFixedWidth(100)
        self.close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.close_btn)
        layout.addLayout(btn_row)

    @staticmethod
    def _stat_block(label_text: str, value_label: QLabel) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(4)
        label = QLabel(label_text)
        label.setStyleSheet(f"color: {PALETTE['text_secondary']}; font-size: 12px;")
        form.addRow(label)
        form.addRow(value_label)
        return box

    def _refresh(self):
        self.comics_value.setText(f"{self._db.count()} 部")
        self.db_size_value.setText(format_size(self._db.db_size()))
        self.cover_count_value.setText(f"{self._db.cover_count()} 张 / {format_size(self._db.cover_size())}")

        records = self._db.load_comics()
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            detail_url = record.get("detail_url", "")
            match = AID_RE.search(detail_url)
            self._set_cell(row, 0, match.group(1) if match else "-")
            self._set_cell(row, 1, record.get("title", ""))
            self._set_cell(row, 2, record.get("date", ""))
            self._set_cell(row, 3, str(record.get("image_count", 0)))

            downloaded = is_comic_downloaded(record)
            self._set_cell(
                row,
                4,
                "已下载" if downloaded else "未下载",
                color="#16A34A" if downloaded else PALETTE["text_secondary"],
            )

            downloaded_at = record.get("downloaded_at", "")
            self._set_cell(
                row,
                5,
                downloaded_at if downloaded and downloaded_at else "—",
                color=PALETTE["text_secondary"],
            )
        self.table.resizeRowsToContents()

    def _set_cell(self, row: int, col: int, text: str, color: str = None):
        item = QTableWidgetItem(text)
        if color:
            item.setForeground(QBrush(QColor(color)))
        self.table.setItem(row, col, item)

    def _on_clear_comics(self):
        answer = QMessageBox.question(
            self,
            "确认清空",
            "确定要清空数据库中的所有漫画记录吗？\n此操作不可撤销。",
        )
        if answer == QMessageBox.Yes:
            self._db.clear_comics()
            self._refresh()
            QMessageBox.information(self, "完成", "数据库已清空。")

    def _on_clear_covers(self):
        answer = QMessageBox.question(
            self,
            "确认清除",
            "确定要清除所有封面缓存图片吗？\n下次浏览时将重新从网络加载。",
        )
        if answer == QMessageBox.Yes:
            self._db.clear_covers()
            self._refresh()
            QMessageBox.information(self, "完成", "封面缓存已清除。")
