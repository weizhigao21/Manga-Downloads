"""数据库管理对话框：表格分页展示漫画记录（ID/名称/日期/张数/是否下载）与统计。

支持每页 5 条分页浏览、右上角按标题/ID 搜索、按下载状态筛选。
"""
import math

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
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

# 每页显示的记录条数
PAGE_SIZE = 5


class DatabaseDialog(QDialog):
    """数据库管理窗口：表格分页展示漫画记录与统计，支持搜索与状态筛选。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("数据库管理")
        self.resize(860, 520)
        self._db = DatabaseManager()
        self._records: list[dict] = []  # 搜索/筛选后的全量记录
        self._page = 1  # 当前页码（从 1 开始）
        self._pages = 1
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
            QPushButton:disabled {{
                color: {PALETTE["text_secondary"]};
                background-color: {PALETTE["surface"]};
                border: 1px solid {PALETTE["border"]};
            }}
            QPushButton#pageButton {{
                padding: 4px 14px;
            }}
            QLineEdit, QComboBox {{
                background-color: {PALETTE["surface"]};
                border: 1px solid {PALETTE["border"]};
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 13px;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("数据库管理")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        # 顶行：左侧统计 + 右上角搜索/筛选
        top_row = QHBoxLayout()
        top_row.setSpacing(24)

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

        top_row.addLayout(stats_row)
        top_row.addStretch()

        # 右上角：搜索框 + 状态筛选下拉
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索标题或 ID")
        self.search_edit.setFixedWidth(200)
        self.search_edit.textChanged.connect(self._on_filter_changed)
        top_row.addWidget(self.search_edit)

        self.filter_combo = QComboBox()
        self.filter_combo.addItem("全部状态", None)
        self.filter_combo.addItem("已下载", True)
        self.filter_combo.addItem("未下载", False)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        top_row.addWidget(self.filter_combo)

        layout.addLayout(top_row)

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
        layout.addWidget(self.table, 1)

        # 底行：分页控件 + 操作按钮
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(12)

        self.prev_btn = QPushButton("上一页")
        self.prev_btn.setObjectName("pageButton")
        self.prev_btn.clicked.connect(self._prev_page)
        bottom_row.addWidget(self.prev_btn)

        self.page_label = QLabel("第 1 / 1 页")
        self.page_label.setStyleSheet(
            f"color: {PALETTE['text_secondary']}; font-size: 13px;"
        )
        bottom_row.addWidget(self.page_label)

        self.next_btn = QPushButton("下一页")
        self.next_btn.setObjectName("pageButton")
        self.next_btn.clicked.connect(self._next_page)
        bottom_row.addWidget(self.next_btn)

        bottom_row.addStretch()

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self._refresh)
        bottom_row.addWidget(self.refresh_btn)

        self.dedupe_btn = QPushButton("查重")
        self.dedupe_btn.clicked.connect(self._open_dedupe)
        bottom_row.addWidget(self.dedupe_btn)

        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("settingsButton")
        self.close_btn.setFixedWidth(100)
        self.close_btn.clicked.connect(self.accept)
        bottom_row.addWidget(self.close_btn)
        layout.addLayout(bottom_row)

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

    # ---- 数据加载与过滤 ----

    def _refresh(self):
        """重新加载数据库并应用搜索/筛选，回到第 1 页。"""
        self.comics_value.setText(f"{self._db.count()} 部")
        self.db_size_value.setText(format_size(self._db.db_size()))
        self.cover_count_value.setText(
            f"{self._db.cover_count()} 张 / {format_size(self._db.cover_size())}"
        )

        records = self._db.load_comics()
        keyword = self.search_edit.text().strip().lower()
        status = self.filter_combo.currentData()
        if status is not None:
            records = [r for r in records if is_comic_downloaded(r) == status]
        if keyword:
            keyword_records = []
            for r in records:
                match = AID_RE.search(r.get("detail_url", ""))
                record_id = match.group(1) if match else ""
                if keyword in r.get("title", "").lower() or keyword in record_id:
                    keyword_records.append(r)
            records = keyword_records

        self._records = records
        self._page = 1
        self._pages = max(1, math.ceil(len(records) / PAGE_SIZE))
        self._render_page()

    def _render_page(self):
        """按当前页渲染表格切片，并同步分页控件状态。"""
        start = (self._page - 1) * PAGE_SIZE
        page_records = self._records[start : start + PAGE_SIZE]

        self.table.setRowCount(len(page_records))
        for row, record in enumerate(page_records):
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

        self.page_label.setText(f"第 {self._page} / {self._pages} 页")
        self.prev_btn.setEnabled(self._page > 1)
        self.next_btn.setEnabled(self._page < self._pages)

    # ---- 搜索/筛选/分页回调 ----

    def _on_filter_changed(self, *args):
        """搜索框或筛选下拉变化：重新过滤并回到第 1 页。"""
        self._refresh()

    def _prev_page(self):
        if self._page > 1:
            self._page -= 1
            self._render_page()

    def _next_page(self):
        if self._page < self._pages:
            self._page += 1
            self._render_page()

    def _open_dedupe(self):
        """打开查重对话框（封面/名称/作者维度扫描重复记录）。"""
        from ui.dialogs.dedupe_dialog import DedupeDialog

        DedupeDialog(self).exec()

    def _set_cell(self, row: int, col: int, text: str, color: str = None):
        item = QTableWidgetItem(text)
        if color:
            item.setForeground(QBrush(QColor(color)))
        self.table.setItem(row, col, item)
