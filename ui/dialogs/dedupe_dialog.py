"""查重对话框：扫描数据库重复记录（封面/名称/作者），分组展示并支持清理。

入口：数据库管理对话框 → 「查重」按钮。计算在后台线程执行（封面哈希
计算可能较慢），带进度条；结果按重复组树形展示，勾选记录后可批量删除
（仅删数据库记录，可选同时删除封面缓存）。
"""
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.database import AID_RE, DatabaseManager, find_duplicates
from ui.styles import PALETTE
from ui.threads import register


class DedupeWorker(QThread):
    """后台查重线程：扫描数据库并计算封面哈希，带进度回调。"""

    progress = Signal(int, int)
    finished_ok = Signal(list)
    error = Signal(str)

    def run(self):
        register(self)
        try:
            groups = find_duplicates(self.progress.emit)
            self.finished_ok.emit(groups)
        except Exception as exc:  # noqa: BLE001 - 查重失败不应导致对话框崩溃
            self.error.emit(str(exc))


class DedupeDialog(QDialog):
    """查重结果窗口：树形分组展示重复记录，支持勾选删除。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("查重")
        self.resize(860, 560)
        self._db = DatabaseManager()
        self._groups: list[dict] = []
        self._setup_ui()
        self._start_scan()

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
            QLabel#summary {{
                font-size: 14px;
                font-weight: 600;
                color: {PALETTE["primary"]};
            }}
            QProgressBar {{
                background-color: {PALETTE["surface"]};
                border: 1px solid {PALETTE["border"]};
                border-radius: 8px;
                height: 10px;
                text-align: center;
            }}
            QProgressBar::chunk {{
                background-color: {PALETTE["primary"]};
                border-radius: 7px;
            }}
            QTreeWidget {{
                background-color: {PALETTE["surface"]};
                alternate-background-color: {PALETTE["surface_hover"]};
                color: {PALETTE["text"]};
                border: 1px solid {PALETTE["border"]};
                border-radius: 8px;
                font-size: 13px;
            }}
            QTreeWidget::item {{
                padding: 4px;
            }}
            QHeaderView::section {{
                background-color: {PALETTE["surface_hover"]};
                color: {PALETTE["text"]};
                border: none;
                border-bottom: 1px solid {PALETTE["border"]};
                padding: 6px;
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
            QCheckBox {{
                color: {PALETTE["text"]};
                font-size: 13px;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = QLabel("数据库查重")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        self.summary = QLabel("正在扫描…")
        self.summary.setObjectName("summary")
        layout.addWidget(self.summary)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)

        # 结果树：顶层为重复组，子项为组内记录
        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["ID", "名称", "作者", "日期", "张数", "已下载"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)

        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        layout.addWidget(self.tree, 1)

        # 勾选联动：父项（组）勾选状态同步到组内所有子项
        self.tree.itemChanged.connect(self._on_item_changed)

        # 操作按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)

        self.rescan_btn = QPushButton("重新扫描")
        self.rescan_btn.clicked.connect(self._start_scan)
        btn_row.addWidget(self.rescan_btn)

        self.select_all_btn = QPushButton("全部勾选")
        self.select_all_btn.clicked.connect(lambda: self._set_all_checks(Qt.Checked))
        btn_row.addWidget(self.select_all_btn)

        self.select_none_btn = QPushButton("取消勾选")
        self.select_none_btn.clicked.connect(lambda: self._set_all_checks(Qt.Unchecked))
        btn_row.addWidget(self.select_none_btn)

        self.del_covers_check = QCheckBox("删除时同时清除封面缓存")
        btn_row.addWidget(self.del_covers_check)

        btn_row.addStretch()

        self.delete_btn = QPushButton("删除选中")
        self.delete_btn.setObjectName("dangerButton")
        self.delete_btn.clicked.connect(self._on_delete_selected)
        btn_row.addWidget(self.delete_btn)

        close_btn = QPushButton("关闭")
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    # ---- 扫描 ----

    def _start_scan(self):
        self._groups = []
        self.tree.clear()
        self.summary.setText("正在扫描…")
        self.progress_bar.setValue(0)
        self._set_busy(True)
        # 扫描可能在对话框关闭后继续收尾，线程不能作为对话框的子对象。
        self._worker = DedupeWorker()
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_scan_done)
        self._worker.error.connect(self._on_scan_error)
        self._worker.finished.connect(lambda: self._set_busy(False))
        self._worker.finished.connect(self._worker.deleteLater)
        register(self._worker)
        self._worker.start()

    def _set_busy(self, busy: bool):
        self.rescan_btn.setEnabled(not busy)
        self.select_all_btn.setEnabled(not busy)
        self.select_none_btn.setEnabled(not busy)
        self.delete_btn.setEnabled(not busy)
        self.progress_bar.setVisible(busy)

    def _on_progress(self, done: int, total: int):
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(done)

    def _on_scan_done(self, groups: list[dict]):
        self._groups = groups
        self._populate(groups)

    def _on_scan_error(self, message: str):
        self.summary.setText("扫描失败")
        QMessageBox.warning(self, "查重失败", f"扫描数据库时出错：\n{message}")

    # ---- 结果展示 ----

    def _populate(self, groups: list[dict]):
        total_items = sum(len(g["items"]) for g in groups)
        if not groups:
            self.summary.setText("未发现重复记录，数据库是干净的。")
            return
        self.summary.setText(f"发现 {len(groups)} 组重复，涉及 {total_items} 条记录")
        for idx, group in enumerate(groups, 1):
            top = QTreeWidgetItem(
                [f"第 {idx} 组 · {len(group['items'])} 条 · 原因：{group['reason']}"]
            )
            top.setFlags(top.flags() & ~Qt.ItemIsSelectable)
            top.setFirstColumnSpanned(True)
            top.setCheckState(0, Qt.Unchecked)
            self.tree.addTopLevelItem(top)
            for rec in group["items"]:
                match = AID_RE.search(rec.get("detail_url", ""))
                aid = match.group(1) if match else ""
                downloaded = bool(rec.get("downloaded"))
                child = QTreeWidgetItem(
                    [
                        aid,
                        rec.get("title", ""),
                        rec.get("author", "") or "",
                        rec.get("date", ""),
                        str(rec.get("image_count", 0)),
                        "已下载" if downloaded else "未下载",
                    ]
                )
                child.setData(0, Qt.UserRole, rec.get("detail_url", ""))
                child.setData(0, Qt.UserRole + 1, aid)
                child.setCheckState(0, Qt.Unchecked)
                if downloaded:
                    child.setForeground(5, QBrush(QColor("#16A34A")))
                else:
                    child.setForeground(5, QBrush(QColor(PALETTE["text_secondary"])))
                top.addChild(child)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        """组父项勾选时联动组内子项（子项勾选不回写父项）。"""
        if column != 0 or item.childCount() == 0:
            return
        state = item.checkState(0)
        for i in range(item.childCount()):
            item.child(i).setCheckState(0, state)

    def _set_all_checks(self, state: Qt.CheckState):
        for top_idx in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(top_idx)
            for i in range(top.childCount()):
                top.child(i).setCheckState(0, state)
            top.setCheckState(0, state)

    # ---- 删除 ----

    def _on_delete_selected(self):
        urls: list[str] = []
        aids: list[str] = []
        for top_idx in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(top_idx)
            for i in range(top.childCount()):
                child = top.child(i)
                if child.checkState(0) == Qt.Checked:
                    urls.append(child.data(0, Qt.UserRole))
                    aid = child.data(0, Qt.UserRole + 1)
                    if aid:
                        aids.append(aid)
        if not urls:
            QMessageBox.information(self, "提示", "请先勾选要删除的记录。")
            return

        message = (
            f"确定删除选中的 {len(urls)} 条记录吗？\n"
            "仅删除数据库记录，不影响已下载的文件。"
        )
        if self.del_covers_check.isChecked():
            message += "\n同时删除对应的封面缓存图片（下次浏览需重新下载）。"
        if QMessageBox.question(self, "确认删除", message) != QMessageBox.Yes:
            return

        self._db.delete_comics(urls)
        if self.del_covers_check.isChecked():
            self._db.delete_covers(aids)
        self._start_scan()
