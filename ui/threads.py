"""后台线程注册表：应用退出前统一收尾非下载类线程。

下载线程由 DownloadManager.shutdown 专门管理；抓取（FetchWorker）与
翻译（TranslateWorker）等其余 QThread 在此登记，退出时统一收尾，
避免 QThread 在底层线程仍在运行时被销毁而触发
"QThread: Destroyed while thread is still running" 警告甚至崩溃。

收尾策略（保证快速退出）：
1. 请求中断（requestInterruption）并短时优雅等待；
2. 仍未结束的线程强制终止（terminate）。网络 IO 阻塞的线程可安全
   终止；下载线程的写文件为原子替换（tmp + rename），强制终止不会
   留下半截文件。

注意：注册表只登记不注销。线程完成后其 C++ 对象经 deleteLater 正常
销毁，Python wrapper 残留到进程退出（线程已结束，析构安全）；若在线程
仍在运行时移除引用，CPython 引用计数会立即回收对象导致析构警告。
"""
import logging
import threading
import time

from PySide6.QtCore import QThread

logger = logging.getLogger(__name__)

_registry: set[QThread] = set()
_registry_lock = threading.Lock()


def register(thread: QThread):
    """登记一个运行中的后台线程（run 中调用，持锁保证跨线程安全）。"""
    with _registry_lock:
        _registry.add(thread)


def shutdown_all(timeout_ms: int = 2000, terminate_grace_ms: int = 1000):
    """请求中断并等待所有已登记线程结束；超时后强制终止（应用退出前调用）。

    线程处于网络 IO 时无法立即中断，仅等待限时（默认 2s）即强制终止，
    避免退出卡在慢速网络请求上。重复调用安全：已结束线程的 wait 立即返回。
    """
    with _registry_lock:
        threads = list(_registry)
    if not threads:
        return
    logger.info("等待 %d 个后台线程结束（最多 %dms）...", len(threads), timeout_ms)
    for thread in threads:
        thread.requestInterruption()
    deadline = time.monotonic() + timeout_ms / 1000
    for thread in threads:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        thread.wait(remaining_ms)
    still = [t for t in threads if t.isRunning()]
    if still:
        logger.warning(
            "%d 个后台线程等待超时，强制终止", len(still)
        )
        for thread in still:
            thread.terminate()
            thread.wait(terminate_grace_ms)
