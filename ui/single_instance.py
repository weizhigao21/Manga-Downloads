"""单实例守护：防止重复启动，二次启动时唤起已运行实例的主窗口。

实现要点：
- 单实例判定用 QLockFile（本机文件锁，跨平台可靠）：主实例持有锁，
  二次启动 tryLock 失败即判定为"非主实例"。
- 唤起信号用 QLocalServer / QLocalSocket 命名管道：主实例监听固定名字，
  二次启动连接该名字发送 "show" 消息后立即退出，主实例收到后由上层
  唤起主窗口（取消最小化/托盘隐藏）。
- 注意：不能只用 QLocalServer 判单例——在 Windows/PySide6 上同名二次
  listen 可能不报冲突（实测两个都能 listen 成功），必须靠 QLockFile。
"""
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QStandardPaths, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

# 固定名字：所有实例共用，跨版本保持一致（保持 ASCII，避免管道名编码问题）
SINGLE_INSTANCE_KEY = "ManHuaBrowser.SingleInstance"

# 二次启动时发送给主实例的消息内容
_SHOW_MESSAGE = b"show"


class SingleInstanceGuard(QObject):
    """单实例守护。

    用法：在 QApplication 创建后构造；若 is_primary 为 False，说明已有
    实例在运行（本构造过程已通知其显示窗口），调用方应立即退出本进程。
    """

    showRequested = Signal()  # 主实例收到其它实例的唤起请求

    def __init__(self, key: str = SINGLE_INSTANCE_KEY, parent: QObject | None = None):
        super().__init__(parent)
        self._key = key
        self._server: QLocalServer | None = None
        self._lock = self._create_lock(key)

        if not self._lock.tryLock(0):
            # 锁被其它实例持有 → 本进程是二次启动，通知主实例后退出
            self._is_primary = False
            self._notify_primary()
            return

        self._is_primary = True
        # 清理上次异常退出可能遗留的监听（此时我们持有锁，不会误删存活实例）
        QLocalServer.removeServer(key)
        self._server = QLocalServer(self)
        # 仅允许当前用户连接，避免其它用户/进程伪装唤起
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_new_connection)
        self._server.listen(key)

    @staticmethod
    def _create_lock(key: str) -> QLockFile:
        """创建位于系统临时目录的锁文件（QLockFile 自动做陈旧锁清理）。"""
        temp = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.TempLocation
        )
        lock = QLockFile(str(Path(temp) / f"{key}.lock"))
        return lock

    @property
    def is_primary(self) -> bool:
        """本进程是否是唯一运行的主实例。"""
        return self._is_primary

    # ---- 主实例侧：接收二次启动的唤起请求 ----

    def _on_new_connection(self):
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        conn.readyRead.connect(lambda: self._on_ready_read(conn))
        conn.disconnected.connect(conn.deleteLater)
        # 消息可能已在等待队列中，主动读一次
        self._on_ready_read(conn)

    def _on_ready_read(self, conn: QLocalSocket):
        if conn.bytesAvailable() <= 0:
            return
        if bytes(conn.readAll()) == _SHOW_MESSAGE:
            self.showRequested.emit()

    # ---- 二次实例侧：通知主实例后退出 ----

    def _notify_primary(self):
        socket = QLocalSocket(self)
        socket.connectToServer(self._key)
        if socket.waitForConnected(1000):
            socket.write(_SHOW_MESSAGE)
            socket.flush()
            socket.waitForBytesWritten(1000)
        socket.abort()
