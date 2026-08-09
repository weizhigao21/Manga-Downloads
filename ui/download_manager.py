"""下载任务管理器：统一管理所有漫画下载任务（无 UI 依赖）。

状态变化通过 task_updated 信号广播，界面（下载管理对话框等）自行订阅；
下载线程生命周期与退出收尾（shutdown）也在此管理。
"""
import json
import logging
import re
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from src.downloader import download_comic
from src.utils import data_root
from ui.models import ComicItem
from ui.settings import ProjectConfig

# 下载任务持久化文件（重启后恢复历史任务列表）
TASKS_PATH = data_root() / "Config" / "tasks.json"


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
        # 下载所需的作品数据（供失败后原地重试复用，随任务持久化）
        self.comic: dict = {}


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
        def _guard():
            """中断检查点：应用退出时请求中断，从回调抛出取消下载任务。"""
            if self.isInterruptionRequested():
                raise InterruptedError("下载任务已中断")

        # 进度信号限频：压缩包模式每 256KB 触发一次回调，若每次都 emit，
        # 会驱动 UI 线程刷新所有卡片（task_updated 有 N 个连接者）造成卡顿。
        # 最多每 100ms 发一次，cur == total 时强制发出保证最终进度准确。
        last_progress = [0.0]

        def _progress(cur: int, total: int):
            _guard()
            now = time.monotonic()
            if cur == total or now - last_progress[0] >= 0.1:
                last_progress[0] = now
                self.progress.emit(self.task, cur, total)

        try:
            download_comic(
                self.comic,
                self.base_dir,
                progress_callback=_progress,
                stage_callback=lambda s: (
                    _guard(),
                    self.stage.emit(self.task, s),
                ),
            )
            self.finished.emit(self.task, True, "")
        except InterruptedError:
            logging.getLogger(__name__).info(
                "下载任务被中断: title=%s", self.task.title
            )
            self.finished.emit(self.task, False, "下载已中断")
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

    task_updated = Signal()        # 无参广播（下载管理对话框等全量刷新）
    task_changed = Signal(object)  # 携带任务（漫画卡片等按任务定向响应，避免各自遍历）
    task_deleted = Signal(object)  # 携带被删除的任务（卡片收到后回到未下载状态）

    _instance: "DownloadManager | None" = None

    @classmethod
    def instance(cls) -> "DownloadManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self.tasks: list[DownloadTask] = self._load_tasks()
        self._workers: dict[DownloadTask, DownloadWorker] = {}
        # 进度更新时持久化节流（避免高频磁盘写入卡顿 UI）
        self._last_save = 0.0

    # ---- 任务持久化 ----

    def _load_tasks(self) -> list[DownloadTask]:
        """从本地文件恢复历史任务。

        - 已完成（DONE）任务重启后自动隐藏：不再恢复显示（已下载状态
          由数据库 downloaded 字段与磁盘目录检查负责，不依赖任务记录）；
        - 中断中的任务（等待/下载中）标记为失败，说明退出原因。
        """
        if not TASKS_PATH.exists():
            return []
        try:
            data = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        tasks = []
        for item in data:
            task = DownloadTask(
                item.get("task_id", ""),
                item.get("title", ""),
                item.get("detail_url", ""),
            )
            task.status = item.get("status", DownloadTask.FAILED)
            task.current = int(item.get("current", 0) or 0)
            task.total = int(item.get("total", 0) or 0)
            task.stage = item.get("stage", "")
            task.error = item.get("error", "")
            task.comic = item.get("comic", {}) or {}
            if task.status == DownloadTask.DONE:
                # 已完成任务重启后自动隐藏（不恢复显示）
                continue
            if task.status in (DownloadTask.WAITING, DownloadTask.DOWNLOADING):
                # 重启后这些任务已无法续跑，标记为失败并说明原因
                task.status = DownloadTask.FAILED
                task.error = "程序已退出，任务中断"
            tasks.append(task)
        return tasks

    def _save_tasks(self):
        """将任务列表写入本地文件（任意状态变化后调用）。"""
        try:
            TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {
                    "task_id": t.task_id,
                    "title": t.title,
                    "detail_url": t.detail_url,
                    "status": t.status,
                    "current": t.current,
                    "total": t.total,
                    "stage": t.stage,
                    "error": t.error,
                    "comic": t.comic,
                }
                for t in self.tasks
            ]
            TASKS_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logging.getLogger(__name__).warning("保存下载任务列表失败: %s", exc)

    def start_download(self, comic: ComicItem) -> DownloadTask | None:
        """为一部漫画创建并启动下载任务，返回任务对象；无详情链接时返回 None。

        同一作品存在**失败**任务时，复用该任务原地重试（不新建重复任务）；
        已下载/进行中的任务不受影响。下载前会从数据库补全最新翻译名，
        保证文件夹命名使用翻译后的名称。
        """
        if not comic.detail_url:
            return None

        existing = self.task_for(comic)
        if existing and existing.status == DownloadTask.FAILED:
            # 失败后再次点击下载：复用原任务重试（用最新作品数据刷新）
            existing.comic = self._fill_translation(self._comic_dict(comic))
            return self.retry_task(existing)

        task = DownloadTask(
            self._extract_id(comic.detail_url), comic.title, comic.detail_url
        )
        task.comic = self._fill_translation(self._comic_dict(comic))
        self.tasks.append(task)
        self._launch_worker(task)
        return task

    @staticmethod
    def _comic_dict(comic: ComicItem) -> dict:
        """将 ComicItem 转换为下载所需的作品数据字典。"""
        return {
            "title": comic.title,
            "detail_url": comic.detail_url,
            "image_count": comic.image_count,
            "category_label": comic.display_category_label,
            "translated_title": comic.translated_title,
        }

    @staticmethod
    def _fill_translation(comic_dict: dict) -> dict:
        """若作品数据缺翻译名，从数据库补全最新翻译结果。

        卡片数据可能早于翻译操作（或重试时沿用任务旧快照），
        这里统一兜底，确保下载文件夹使用翻译后的名称。
        """
        if comic_dict.get("translated_title"):
            return comic_dict
        detail_url = comic_dict.get("detail_url", "")
        if not detail_url:
            return comic_dict
        from ui.database import _cached_records

        for record in _cached_records():
            if record.get("detail_url") == detail_url and record.get("translated_title"):
                comic_dict["translated_title"] = record["translated_title"]
                break
        return comic_dict

    def retry_task(self, task: DownloadTask) -> DownloadTask | None:
        """原地重试一个失败任务：重置状态并重新启动下载线程。

        使用任务自带的作品数据（task.comic，随任务持久化），
        因此下载管理对话框/重启后的任务也能直接重试。
        重试前会从数据库补全最新翻译名，避免使用旧快照导致文件夹
        未用翻译后的名称。数据不足（无详情链接）时返回 None。
        """
        if not task.comic:
            # 兼容旧版持久化数据：用已有字段构建最小作品数据
            task.comic = {"title": task.title, "detail_url": task.detail_url}
        if not task.comic.get("detail_url"):
            return None
        # 清理可能残留的旧线程关联
        self._workers.pop(task, None)
        # 补全最新翻译名（覆盖旧快照/持久化时尚未翻译的情况）
        task.comic = self._fill_translation(task.comic)
        task.status = DownloadTask.WAITING
        task.current = 0
        task.total = 0
        task.stage = ""
        task.error = ""
        self._launch_worker(task)
        return task

    def delete_task(self, task: DownloadTask):
        """删除任务记录（不影响已下载的磁盘文件）。

        运行中的任务先请求中断并短时等待（写文件为原子替换，
        中断不会留下半截文件）；删除后广播 task_deleted，相关卡片
        重新自查并回到未下载状态。
        """
        worker = self._workers.pop(task, None)
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            worker.wait(1500)
            if worker.isRunning():
                worker.terminate()
                worker.wait(500)
        if task in self.tasks:
            self.tasks.remove(task)
        self._save_tasks()
        self.task_deleted.emit(task)
        self.task_updated.emit()

    def _launch_worker(self, task: DownloadTask):
        """创建并启动下载线程（供新建任务与重试复用）。"""
        base_dir = Path(ProjectConfig().download_dir)
        worker = DownloadWorker(task.comic, base_dir, task)
        self._workers[task] = worker
        worker.progress.connect(self._on_progress)
        worker.stage.connect(self._on_stage)
        worker.finished.connect(self._on_finished)
        worker.finished.connect(worker.deleteLater)
        self.task_changed.emit(task)
        self.task_updated.emit()
        self._save_tasks()
        worker.start()

    def _on_progress(self, task: DownloadTask, current: int, total: int):
        task.current = current
        task.total = total
        task.status = DownloadTask.DOWNLOADING
        self.task_changed.emit(task)
        self.task_updated.emit()
        # 进度高频到达：持久化最多每 500ms 一次，状态/结果变化仍即时保存
        now = time.monotonic()
        if now - self._last_save >= 0.5:
            self._last_save = now
            self._save_tasks()

    def _on_stage(self, task: DownloadTask, stage: str):
        task.stage = stage
        self.task_changed.emit(task)
        self.task_updated.emit()
        self._save_tasks()

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
        self.task_changed.emit(task)
        self.task_updated.emit()
        self._save_tasks()

    def task_for(self, comic: ComicItem) -> DownloadTask | None:
        """返回该漫画最近创建的下载任务（若无则 None）。"""
        task_id = self._extract_id(comic.detail_url)
        if not task_id:
            return None
        for task in reversed(self.tasks):
            if task.task_id == task_id:
                return task
        return None

    def request_interruption(self):
        """请求中断所有下载线程（不等待）。

        窗口关闭时先快速触发中断，让线程在下一个检查点退出，
        实际收尾等待由应用退出流程（aboutToQuit → shutdown）统一完成，
        避免关闭窗口时同步阻塞等待。
        """
        for worker in self._workers.values():
            if worker.isRunning():
                worker.requestInterruption()

    def shutdown(self, timeout_ms: int = 2500, terminate_grace_ms: int = 1000):
        """请求中断所有下载线程并等待结束；超时后强制终止（应用退出前调用）。

        下载线程处于网络 IO 时无法立即终止，中断粒度约为一小段数据/一张图片；
        写文件为原子替换（tmp + rename），强制终止不会留下半截文件。
        限时等待后仍未结束的线程强制终止，保证退出不卡顿。
        重复调用安全：已结束的线程 wait 立即返回。
        """
        workers = [w for w in self._workers.values() if w.isRunning()]
        if not workers:
            return
        logging.getLogger(__name__).info(
            "等待 %d 个下载线程结束（最多 %dms）...", len(workers), timeout_ms
        )
        for worker in workers:
            worker.requestInterruption()
        deadline = time.monotonic() + timeout_ms / 1000
        for worker in workers:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            worker.wait(remaining_ms)
        still = [w for w in workers if w.isRunning()]
        if still:
            logging.getLogger(__name__).warning(
                "%d 个下载线程等待超时，强制终止", len(still)
            )
            for worker in still:
                worker.terminate()
                worker.wait(terminate_grace_ms)

    @staticmethod
    def _extract_id(detail_url: str) -> str:
        match = re.search(r"aid-(\d+)", detail_url)
        return match.group(1) if match else detail_url


