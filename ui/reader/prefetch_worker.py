"""阅读器后台工作线程：页面获取与相邻页预取。

与项目既有约定保持一致：
- 业务完成信号命名为 `done`（**不要**命名为 finished，否则会遮蔽 QThread
  内置信号，破坏「线程完全停止后才触发」的清理时机，导致
  "QThread: Destroyed while thread is still running" 崩溃）；
- 调用方把 deleteLater 连接**原生 finished**，不要连接 done。

两种线程：
- PageLoadWorker：加载单页（前台请求，优先级高）
- PrefetchWorker：预取窗口（后台补拉，可取消）
"""
import logging

from PySide6.QtCore import QThread, Signal

from ui.reader.page_provider import PageFetchError, ReaderSession

logger = logging.getLogger(__name__)


class PageLoadWorker(QThread):
    """加载单页：本地优先，未命中时联网补拉并写盘。

    信号都会带上发起请求时的 `seq`（窗口自增序号）。窗口据此丢弃过期
    结果——用户快速翻页时，先发出的旧请求可能后返回，若无序号校验会把
    旧页画面覆盖到新页上（表现为「点了下一页还显示上一页」）。
    """

    loaded = Signal(int, int, object)  # (seq, 页码, 本地图片路径)
    failed = Signal(int, int, str)     # (seq, 页码, 错误信息)

    def __init__(
        self, session: ReaderSession, page_no: int, seq: int = 0, parent=None
    ):
        super().__init__(parent)
        self.session = session
        self.page_no = int(page_no)
        self.seq = int(seq)

    def run(self):
        try:
            path = self.session.fetch_page(self.page_no)
        except PageFetchError as exc:
            # 中断请求时静默退出（用户已翻页/关闭）
            if not self.isInterruptionRequested():
                self.failed.emit(self.seq, self.page_no, str(exc))
            return
        except Exception as exc:  # 兜底：任何异常都不允许逃逸线程
            logger.exception("阅读页加载异常: page=%s", self.page_no)
            if not self.isInterruptionRequested():
                self.failed.emit(self.seq, self.page_no, str(exc))
            return
        if not self.isInterruptionRequested():
            self.loaded.emit(self.seq, self.page_no, path)


class PrefetchWorker(QThread):
    """后台预取窗口内的缺失页（可取消：用户翻页后旧窗口任务立即作废）。"""

    page_ready = Signal(int)       # 某页补拉成功（页码）
    progress = Signal(int, int)    # (已缓存张数, 期望总数)

    def __init__(
        self,
        session: ReaderSession,
        page_no: int,
        ahead: int = 3,
        behind: int = 1,
        parent=None,
    ):
        super().__init__(parent)
        self.session = session
        self.page_no = int(page_no)
        self.ahead = max(0, int(ahead))
        self.behind = max(0, int(behind))

    def run(self):
        total = self.session.resolve_total_pages()

        def _cancelled() -> bool:
            return self.isInterruptionRequested()

        try:
            self.session.prefetch_around(
                self.page_no,
                ahead=self.ahead,
                behind=self.behind,
                cancel_check=_cancelled,
                on_progress=self._on_page,
            )
        except Exception:
            logger.exception("预取线程异常: page=%s", self.page_no)
        finally:
            if not self.isInterruptionRequested():
                self.progress.emit(self.session.local_count, total)

    def _on_page(self, page_no: int):
        """单个页面补拉成功：通知界面刷新进度（取消后不再 emit）。"""
        if self.isInterruptionRequested():
            return
        self.page_ready.emit(page_no)
        self.progress.emit(self.session.local_count, self.session.resolve_total_pages())
