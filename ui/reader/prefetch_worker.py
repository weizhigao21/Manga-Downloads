"""阅读器后台工作线程：页面获取与相邻页预取。

与项目既有约定保持一致：
- 业务完成信号命名为 `done`（**不要**命名为 finished，否则会遮蔽 QThread
  内置信号，破坏「线程完全停止后才触发」的清理时机，导致
  "QThread: Destroyed while thread is still running" 崩溃）；
- 调用方把 deleteLater 连接**原生 finished**，不要连接 done。

两种线程：
- PageLoadWorker：加载单页（前台请求，优先级高）
- PrefetchWorker：预取窗口（后台补拉 + 解码，可取消）

预取为什么在**线程里**解码：翻页时命中内存缓存才能"秒开"，而把两页
JPEG 在主线程解码会让界面卡顿（每页几十毫秒）。QImage 是纯光栅数据，
可以在后台线程解码；QPixmap 只能在 GUI 线程创建——所以这里交出 QImage，
由窗口在主线程转成 QPixmap 存进缓存。
"""
import logging

from PySide6.QtCore import QThread, Signal

from ui.reader.image_cache import decode_image
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
    """后台预取窗口内的缺失页（可取消：用户翻页后旧窗口任务立即作废）。

    每补拉成功一页就在**本线程内**解码，把 QImage 交回主线程——主线程
    只做 QPixmap 转换与入缓存，翻到该页时无需再读盘解码。
    """

    page_ready = Signal(int, object, str)  # (页码, 解码后的 QImage, 本地路径)
    progress = Signal(int, int)            # (已缓存张数, 期望总数)

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

    def _on_page(self, page_no: int, path):
        """单页补拉成功：在线程内解码，再通知主线程入内存缓存。

        解码失败（文件损坏/格式不支持）不影响磁盘缓存已经写好的事实，
        只是这一页不做内存预取；用户翻到它时会走正常加载路径重试。
        """
        if self.isInterruptionRequested():
            return
        image = decode_image(path)
        if image is None:
            logger.info("预取解码失败（翻页时将重新尝试）: page=%s", page_no)
            return
        self.page_ready.emit(page_no, image, str(path))
        self.progress.emit(self.session.local_count, self.session.resolve_total_pages())
