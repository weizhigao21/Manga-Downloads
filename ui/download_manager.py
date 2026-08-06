"""下载任务管理器：统一管理所有漫画下载任务，展示 ID、进度与状态。"""
import logging
import re
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.models import ComicItem
from ui.settings import ProjectConfig
from ui.styles import PALETTE

# 允许从 src 目录导入下载逻辑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from downloader import download_comic


class DownloadTask:
    """单个下载任务的实时状态（在工作线程中更新，由信号同步回 UI）。"""

    # 状态常量
    WAITING = "等待中"
    DOWNLOADING = "下载中"
    DONE = "已完成"
    FAILED = "失败"

    def __init__(self, task_id: str, title: str, detail_url: str = ""):
        self.task_id = task_id  # wnacg 作品 ID（aid 编号）
        self.title = title      # 作品名称
        self.detail_url = detail_url  # 详情页链接（用于更新数据库下载状态）
        self.status: str = self.WAITING
        self.current: int = 0   # 已下载数量（图片张数 / 压缩包 MB / 已解压文件数）
        self.total: int = 0     # 总数（解析完成后才知道，0 表示未知）
        self.stage: str = ""    # 当前阶段文本（如 下载压缩包 / 解压中）
        self.error: str = ""


class DownloadWorker(QThread):
    """后台下载线程。

    注意：不要为线程设置父对象。卡片在翻页/刷新时会被 deleteLater 销毁，
    若线程是卡片的子对象，销毁卡片会连带销毁仍在运行的线程。
    """

    progress = Signal(object, int, int)   # (task, 当前数量, 总数)
    stage = Signal(object, str)           # (task, 阶段文本)
    finished = Signal(object, bool, str)  # (task, 是否成功, 错误信息)

    def __init__(self, comic: dict, base_dir: Path, task: DownloadTask):
        super().__init__()
        self.comic = comic
        self.base_dir = base_dir
        self.task = task

    def run(self):
        try:
            download_comic(
                self.comic,
                self.base_dir,
                progress_callback=lambda cur, total: self.progress.emit(
                    self.task, cur, total
                ),
                stage_callback=lambda s: self.stage.emit(self.task, s),
            )
            self.finished.emit(self.task, True, "")
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "下载任务失败: title=%s, detail_url=%s", self.task.title, self.task.detail_url
            )
            self.finished.emit(self.task, False, str(exc))


class DownloadManager(QObject):
    """全局下载任务管理器（单例）。

    - 持有所有下载任务与其线程，防止线程对象被回收。
    - 任何状态变化都会发出 task_updated 信号，供界面刷新。
    """

    task_updated = Signal()

    _instance: "DownloadManager | None" = None

    @classmethod
    def instance(cls) -> "DownloadManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self.tasks: list[DownloadTask] = []
        self._workers: dict[DownloadTask, DownloadWorker] = {}

    def start_download(self, comic: ComicItem) -> DownloadTask | None:
        """为一部漫画创建并启动下载任务，返回任务对象；无详情链接时返回 None。"""
        if not comic.detail_url:
            return None

        task = DownloadTask(
            self._extract_id(comic.detail_url), comic.title, comic.detail_url
        )
        self.tasks.append(task)

        comic_dict = {
            "title": comic.title,
            "detail_url": comic.detail_url,
            "image_count": comic.image_count,
            "category_label": comic.display_category_label,
        }
        base_dir = Path(ProjectConfig().download_dir)

        worker = DownloadWorker(comic_dict, base_dir, task)
        self._workers[task] = worker
        worker.progress.connect(self._on_progress)
        worker.stage.connect(self._on_stage)
        worker.finished.connect(self._on_finished)
        worker.finished.connect(worker.deleteLater)
        self.task_updated.emit()
        worker.start()
        return task

    def _on_progress(self, task: DownloadTask, current: int, total: int):
        task.current = current
        task.total = total
        task.status = DownloadTask.DOWNLOADING
        self.task_updated.emit()

    def _on_stage(self, task: DownloadTask, stage: str):
        task.stage = stage
        self.task_updated.emit()

    def _on_finished(self, task: DownloadTask, success: bool, error: str):
        self._workers.pop(task, None)
        if success:
            task.status = DownloadTask.DONE
            task.stage = ""
            # 下载成功后，在数据库中标记该作品为已下载
            if task.detail_url:
                from ui.database import DatabaseManager

                DatabaseManager.instance().mark_downloaded(task.detail_url)
        else:
            task.status = DownloadTask.FAILED
            task.error = error
        self.task_updated.emit()

    def task_for(self, comic: ComicItem) -> DownloadTask | None:
        """返回该漫画最近创建的下载任务（若无则 None）。"""
        task_id = self._extract_id(comic.detail_url)
        if not task_id:
            return None
        for task in reversed(self.tasks):
            if task.task_id == task_id:
                return task
        return None

    @staticmethod
    def _extract_id(detail_url: str) -> str:
        match = re.search(r"aid-(\d+)", detail_url)
        return match.group(1) if match else detail_url


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

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["ID", "名称", "进度", "状态"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
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
        self._manager.task_updated.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        """刷新任务列表：行数变化时重建整行，否则仅更新进度与状态。"""
        tasks = self._manager.tasks
        # 行数变化（任务新增/完成清除）时才重建进度控件
        if self.table.rowCount() != len(tasks):
            self._bars.clear()
            self._labels.clear()
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

    def _set_cell(self, row: int, col: int, text: str, color: str = None):
        item = QTableWidgetItem(text)
        if color:
            from PySide6.QtGui import QBrush, QColor

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
