"""后台线程注册表：应用退出前统一收尾非下载类线程。

下载线程由 DownloadManager.shutdown 专门管理；抓取（FetchWorker）、
相似扫描（SimilarWorker）、查重（DedupeWorker）与翻译（TranslateWorker）
等其余 QThread 在此登记，退出时统一收尾，避免 QThread 在底层线程仍在
运行时被销毁而触发 "QThread: Destroyed while thread is still running"
警告甚至崩溃。

收尾策略（保证快速退出）：
1. 请求中断（requestInterruption）并短时优雅等待；
2. 仍未结束的线程强制终止（terminate）。网络 IO 阻塞的线程可安全
   终止；下载线程的写文件为原子替换（tmp + rename），强制终止不会
   留下半截文件。

线程生命周期与注册表：
- register 在 worker 的 run() 中调用；同时连接 finished 信号，线程结束
  （run 返回）后自动从注册表注销，避免已完成线程的 C++ 对象被
  deleteLater 销毁后注册表仍持有其 Python wrapper，退出收尾时访问
  已删除对象而抛 "Internal C++ object already deleted"。
- shutdown_all 仍对已销毁对象做防御（RuntimeError 跳过），覆盖
  finished 信号尚未被事件循环处理（退出瞬间）的窗口期。
"""
import logging
import threading
import time

from PySide6.QtCore import QThread

logger = logging.getLogger(__name__)

_registry: set[QThread] = set()
_registry_lock = threading.Lock()


def _alive(thread: QThread) -> bool:
    """线程的 C++ 对象是否仍有效（已被 deleteLater 销毁时返回 False）。"""
    try:
        thread.isRunning()
        return True
    except RuntimeError:
        return False


def register(thread: QThread):
    """登记一个运行中的后台线程（run 中调用，持锁保证跨线程安全）。

    线程结束后自动注销：finished 信号发出时从注册表移除，确保注册表
    只持有仍有效的线程对象（线程结束后析构是安全的，不会触发
    "Destroyed while thread is still running" 警告）。
    """
    with _registry_lock:
        _registry.add(thread)
    thread.finished.connect(lambda t=thread: _unregister(t))


def _unregister(thread: QThread):
    """线程结束后从注册表移除（finished 信号槽，持锁保证跨线程安全）。"""
    with _registry_lock:
        _registry.discard(thread)


def shutdown_all(timeout_ms: int = 2000, terminate_grace_ms: int = 1000):
    """请求中断并等待所有已登记线程结束；超时后强制终止（应用退出前调用）。

    线程处于网络 IO 时无法立即中断，仅等待限时（默认 2s）即强制终止，
    避免退出卡在慢速网络请求上。重复调用安全：已结束线程的 wait 立即返回。
    已完成（C++ 对象已销毁）的线程自动跳过，不抛 RuntimeError。
    """
    with _registry_lock:
        threads = list(_registry)
    # 只收尾仍然存活（C++ 对象未销毁）的线程
    alive = [t for t in threads if _alive(t)]
    if not alive:
        return
    logger.info("等待 %d 个后台线程结束（最多 %dms）...", len(alive), timeout_ms)
    for thread in alive:
        try:
            thread.requestInterruption()
        except RuntimeError:
            continue
    deadline = time.monotonic() + timeout_ms / 1000
    for thread in alive:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        try:
            thread.wait(remaining_ms)
        except RuntimeError:
            continue
    still = [t for t in alive if _alive(t) and t.isRunning()]
    if still:
        logger.warning(
            "%d 个后台线程等待超时，强制终止", len(still)
        )
        for thread in still:
            try:
                thread.terminate()
                thread.wait(terminate_grace_ms)
            except RuntimeError:
                continue
