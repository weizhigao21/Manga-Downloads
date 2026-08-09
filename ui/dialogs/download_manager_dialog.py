"""下载管理对话框：表格展示所有下载任务（ID/名称/进度条/状态），支持右键删除任务。"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.download_manager import DownloadManager, DownloadTask
from ui.styles import PALETTE


class DownloadManagerDialog(QDialog):
    """下载管理对话框：表格展示所有任务，含 ID、名称、进度条、状态。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("下载管理")
        self.resize(760, 420)
        self.setStyleSheet(
            f"""
            QDialog {{
                background-color: {PALETTE["background"]};
            }}
            QLabel {{
                color: {PALETTE["text"]};
                font-size: 13px;
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
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        hint = QLabel("下载任务实时进度（关闭对话框不影响后台下载）")
        hint.setStyleSheet(f"color: {PALETTE['text_secondary']};")
        layout.addWidget(hint)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["ID", "名称", "进度", "状态", "操作"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        # 右键菜单：删除所选任务
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("settingsButton")
        self.close_btn.setFixedWidth(100)
        self.close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.close_btn)
        layout.addLayout(btn_row)

        self._manager = DownloadManager.instance()
        # 保存每行进度控件引用，刷新时只更新数值、不重建控件（避免进度条闪烁）
        self._bars: dict[int, QProgressBar] = {}
        self._labels: dict[int, QLabel] = {}
        self._retry_buttons: dict[int, QPushButton] = {}
        self._manager.task_updated.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        """刷新任务列表：行数变化时重建整行，否则仅更新进度与状态。"""
        tasks = self._manager.tasks
        # 行数变化（任务新增/完成清除）时才重建进度控件
        if self.table.rowCount() != len(tasks):
            self._bars.clear()
            self._labels.clear()
            self._retry_buttons.clear()
            self.table.setRowCount(len(tasks))
            for row, task in enumerate(tasks):
                self._set_cell(row, 0, task.task_id)
                self._set_cell(row, 1, task.title)

                # 进度列：进度条 + 文本
                progress_widget = QWidget()
                progress_layout = QHBoxLayout(progress_widget)
                progress_layout.setContentsMargins(4, 2, 4, 2)
                progress_layout.setSpacing(8)
                bar = QProgressBar()
                bar.setFixedWidth(180)
                bar.setTextVisible(False)
                progress_layout.addWidget(bar)
                pct_label = QLabel()
                pct_label.setStyleSheet(f"color: {PALETTE['text_secondary']};")
                progress_layout.addWidget(pct_label)
                progress_layout.addStretch()
                self.table.setCellWidget(row, 2, progress_widget)
                self._bars[row] = bar
                self._labels[row] = pct_label

                self._set_cell(row, 3, task.status, color=self._status_color(task.status))

                # 操作列：失败任务显示重试按钮
                retry_btn = QPushButton("重试")
                retry_btn.setObjectName("retryButton")
                retry_btn.setFixedWidth(60)
                retry_btn.clicked.connect(
                    lambda checked=False, t=task: self._manager.retry_task(t)
                )
                retry_host = QWidget()
                retry_layout = QHBoxLayout(retry_host)
                retry_layout.setContentsMargins(4, 2, 4, 2)
                retry_layout.addWidget(retry_btn)
                retry_layout.addStretch()
                self.table.setCellWidget(row, 4, retry_host)
                self._retry_buttons[row] = retry_btn
            self.table.resizeRowsToContents()
            return

        # 行数不变：仅更新已有进度条与文本，不重建控件
        for row, task in enumerate(tasks):
            bar = self._bars.get(row)
            label = self._labels.get(row)
            if bar is not None:
                if task.status == DownloadTask.DOWNLOADING and task.total == 0:
                    bar.setRange(0, 0)  # 总数未知：不定态
                else:
                    bar.setRange(0, max(task.total, 1))
                    value = (
                        max(task.total, 1)
                        if task.status == DownloadTask.DONE
                        else min(task.current, task.total) if task.total else 0
                    )
                    bar.setValue(value)
            if label is not None:
                label.setText(self._progress_text(task))
            self._set_cell(row, 3, task.status, color=self._status_color(task.status))
            # 重试按钮仅对失败任务可见（重试后自动隐藏）
            retry_btn = self._retry_buttons.get(row)
            if retry_btn is not None:
                retry_btn.setVisible(task.status == DownloadTask.FAILED)

    def _on_context_menu(self, pos):
        """表格右键：弹出删除菜单（支持多选批量删除）。

        QMenu 为独立弹出窗口，不继承项目浅色主题；系统深色模式下默认
        黑底，与浅色文字叠加不可见，因此显式设置浅色样式（与卡片菜单一致）。
        """
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows:
            # 未选中任何行时，按右键位置定位所在行
            row = self.table.rowAt(pos.y())
            if row < 0:
                return
            rows = [row]

        menu = QMenu(self)
        menu.setStyleSheet(
            f"""
            QMenu {{
                background-color: {PALETTE["surface"]};
                color: {PALETTE["text"]};
                border: 1px solid {PALETTE["border"]};
                border-radius: 8px;
                padding: 6px;
            }}
            QMenu::item {{
                background-color: transparent;
                color: {PALETTE["text"]};
                padding: 6px 24px;
                border-radius: 6px;
                font-size: 13px;
            }}
            QMenu::item:selected {{
                background-color: {PALETTE["primary"]};
                color: white;
            }}
            """
        )
        label = f"删除所选 {len(rows)} 个任务" if len(rows) > 1 else "删除任务"
        action = menu.addAction(label)
        if menu.exec(self.table.viewport().mapToGlobal(pos)) == action:
            self._delete_selected_tasks(rows)

    def _delete_selected_tasks(self, rows: list[int]):
        """删除选中行对应的任务（仅移除任务记录，不影响已下载的磁盘文件）。"""
        tasks = [self._manager.tasks[r] for r in rows if 0 <= r < len(self._manager.tasks)]
        if not tasks:
            return
        answer = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除 {len(tasks)} 个下载任务吗？\n已下载的文件不会删除。",
        )
        if answer == QMessageBox.Yes:
            for task in tasks:
                self._manager.delete_task(task)
            # delete_task 已触发 task_updated → 表格自动刷新

    def _set_cell(self, row: int, col: int, text: str, color: str = None):
        item = QTableWidgetItem(text)
        if color:
            item.setForeground(QBrush(QColor(color)))
        self.table.setItem(row, col, item)

    # 压缩包模式下进度为 0-100 百分比，阶段文本按百分比显示
    _PERCENT_STAGES = ("下载压缩包", "解压中")

    @staticmethod
    def _progress_text(task: DownloadTask) -> str:
        if task.status == DownloadTask.DONE:
            return "100%"
        if task.status == DownloadTask.WAITING:
            return "等待中"
        if task.status == DownloadTask.FAILED and task.stage:
            return task.stage
        if task.stage in DownloadManagerDialog._PERCENT_STAGES and task.total:
            return f"{task.stage} {min(task.current, task.total)}%"
        if task.stage:
            return f"{task.stage} {task.current}/{task.total}" if task.total else task.stage
        if task.total:
            return f"{task.current}/{task.total}"
        return f"{task.current}"

    @staticmethod
    def _status_color(status: str) -> str:
        return {
            DownloadTask.DOWNLOADING: PALETTE["primary"],
            DownloadTask.DONE: "#16A34A",
            DownloadTask.FAILED: "#DC2626",
            DownloadTask.WAITING: PALETTE["text_secondary"],
        }.get(status, PALETTE["text"])
