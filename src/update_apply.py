"""更新包应用：校验、解压、合并替换、备份回滚、重启主程序。

由独立更新程序 `updater.py` 调用（更新程序自己被复制到 %TEMP% 运行，
因为 Windows 会锁定正在运行的 exe，原地替换必然失败）。纯标准库，
不导入 Qt——更新程序要能独立运行、体积小、启动快。

**安全底线（用户数据不可丢）**：
- 安装目录里的 `Config/`（数据库、封面、设置）、`Download/`（漫画本体）、
  `logs/` 属用户数据，替换时**一律跳过**，绝不能被新包覆盖或删除；
- 替换前把全部旧程序文件整体移入备份目录，新版本启动失败可原样回滚；
- 解压前校验 zip 条目路径（拒绝绝对路径与 `..`，防 zip-slip）。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

# 更新时必须保留的顶层目录（用户数据）；比较时忽略大小写（Windows 语义）
DATA_DIRS = ("config", "download", "logs")
# 备份目录名（就在安装目录内，合并时同样跳过）
BACKUP_DIR_NAME = ".backup-update"
# zip 里必须存在的主程序文件名
REQUIRED_FILES = ("漫画浏览器.exe",)


class UpdateError(RuntimeError):
    """更新应用失败；消息可直接展示给用户。"""


# ---- zip 校验与解压 ----


def _normalize(name: str) -> str:
    return name.replace("\\", "/")


def inspect_zip(zip_path: Path) -> list[str]:
    """校验更新包并返回文件条目名（不含目录项）。

    拒绝绝对路径、`..`、盘符（zip-slip 防御）——更新包会把内容直接写进
    安装目录，一个越界条目就能覆盖系统文件。
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise UpdateError(f"更新包不存在：{zip_path}")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"更新包已损坏：{exc}") from exc
    if not names:
        raise UpdateError("更新包是空的")
    for name in names:
        pure = PurePosixPath(_normalize(name))
        if pure.is_absolute() or ".." in pure.parts or ":" in pure.parts[0]:
            raise UpdateError(f"更新包含非法路径：{name}")
    return names


def payload_prefix(names: list[str]) -> str:
    """确定载荷在 zip 内的前缀（`''` 或 `漫画浏览器-v1.3.14/`）。

    打包时可能把整个版本目录压进 zip（多一层目录），这里按"主程序所在
    层级"自动识别；找不到主程序直接报错，避免解压出一堆无用文件。
    """
    prefixes: list[str] = []
    for name in names:
        posix = _normalize(name)
        base = posix.rsplit("/", 1)[-1]
        if base in REQUIRED_FILES:
            prefixes.append(posix[: len(posix) - len(base)])
    if not prefixes:
        raise UpdateError("更新包里没有主程序：" + "、".join(REQUIRED_FILES))
    return min(prefixes, key=len)


def _is_skipped(relative: str) -> bool:
    """载荷内的相对路径是否要跳过（用户数据目录 / 备份目录）。"""
    first = _normalize(relative).split("/", 1)[0].lower()
    return first in DATA_DIRS or first == BACKUP_DIR_NAME


def extract_payload(
    zip_path: Path,
    staging_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Path:
    """把载荷解压到暂存目录（跳过用户数据目录），返回载荷根目录。

    Args:
        on_progress: 回调 (已解压文件数, 总文件数)。
    """
    names = inspect_zip(zip_path)
    prefix = payload_prefix(names)
    members = [n for n in names if _normalize(n).startswith(prefix)]
    if not members:
        raise UpdateError("更新包内没有可用的程序文件")
    staging_dir = Path(staging_dir)
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    done = 0
    with zipfile.ZipFile(zip_path) as zf:
        for name in members:
            if cancel_check and cancel_check():
                raise UpdateError("已取消更新")
            relative = _normalize(name)[len(prefix):]
            if not relative or _is_skipped(relative):
                continue
            target = staging_dir / PurePosixPath(relative).as_posix()
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            done += 1
            if on_progress:
                on_progress(done, len(members))
    return staging_dir


# ---- 合并替换与回滚 ----


def _rm(entry: Path) -> None:
    if entry.is_dir() and not entry.is_symlink():
        shutil.rmtree(entry, ignore_errors=True)
    else:
        entry.unlink(missing_ok=True)


def _transfer(src: Path, dst: Path, on_status: Callable[[str], None] | None) -> None:
    """移动/复制一个顶层条目；跨盘 move 失败时退回复制。"""
    try:
        shutil.move(str(src), str(dst))
        return
    except OSError:
        pass
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    _rm(src)
    if on_status:
        on_status(f"已复制 {src.name}")


def replace_program_files(
    payload_root: Path,
    install_dir: Path,
    on_status: Callable[[str], None] | None = None,
) -> Path:
    """把载荷覆盖进安装目录，返回备份目录（失败时用 `rollback` 还原）。

    步骤：旧程序文件（exe、`_internal/` 及旧版残留）整体移入备份目录 →
    新载荷逐项搬进来。`Config/`、`Download/`、`logs/` 与备份目录本身
    全程跳过。
    """
    payload_root = Path(payload_root)
    install_dir = Path(install_dir)
    if not payload_root.is_dir():
        raise UpdateError(f"载荷目录不存在：{payload_root}")
    if not install_dir.is_dir():
        raise UpdateError(f"安装目录不存在：{install_dir}")

    backup_dir = install_dir / BACKUP_DIR_NAME
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    backup_dir.mkdir(parents=True)

    # 1) 旧程序文件先整体移走（数据目录留在原地）
    for entry in list(install_dir.iterdir()):
        if entry.name.lower() in DATA_DIRS or entry.name == BACKUP_DIR_NAME:
            continue
        if on_status:
            on_status(f"备份旧版 {entry.name}")
        _transfer(entry, backup_dir / entry.name, on_status=None)

    # 2) 新载荷搬进安装目录
    for entry in sorted(payload_root.iterdir()):
        if entry.name.lower() in DATA_DIRS or entry.name == BACKUP_DIR_NAME:
            continue
        if on_status:
            on_status(f"安装 {entry.name}")
        _transfer(entry, install_dir / entry.name, on_status)
    return backup_dir


def rollback(install_dir: Path, backup_dir: Path) -> None:
    """回滚：删掉新版程序文件，把备份原样搬回。"""
    install_dir = Path(install_dir)
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        raise UpdateError("备份目录不存在，无法回滚")
    for entry in list(install_dir.iterdir()):
        if entry.name.lower() in DATA_DIRS or entry.name == BACKUP_DIR_NAME:
            continue
        _rm(entry)
    for entry in list(backup_dir.iterdir()):
        _transfer(entry, install_dir / entry.name, on_status=None)
    shutil.rmtree(backup_dir, ignore_errors=True)


def finish_update(backup_dir: Path) -> None:
    """更新成功后的收尾：删除备份目录。"""
    shutil.rmtree(Path(backup_dir), ignore_errors=True)


# ---- 进程协作 ----


def _detached_flags() -> int:
    """分离进程标志（Windows）；非 Windows 平台返回 0。"""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return flags


def wait_for_process_exit(pid: int, timeout: float = 60.0) -> bool:
    """等待指定进程退出，返回是否已退出（超时返回 False）。

    Windows 上用 `OpenProcess + WaitForSingleObject`（不引入 psutil）；
    其他平台退回轮询"进程是否存在"。pid 为 0 或进程不存在视为已退出。
    """
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            return True  # 进程已不在
        try:
            result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            return result == 0  # WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.3)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def start_app(install_dir: Path, exe_name: str = REQUIRED_FILES[0]) -> subprocess.Popen:
    """启动主程序（分离进程：更新程序退出后它继续运行）。"""
    exe = Path(install_dir) / exe_name
    if not exe.is_file():
        raise UpdateError(f"找不到主程序：{exe}")
    return subprocess.Popen(
        [str(exe)],
        cwd=str(install_dir),
        creationflags=_detached_flags(),
        close_fds=True,
    )


def launch_updater(
    install_dir: Path,
    zip_path: Path,
    exe_name: str = REQUIRED_FILES[0],
    pid: int | None = None,
    updater_name: str = "更新程序.exe",
    work_dir: Path | None = None,
) -> subprocess.Popen:
    """把更新程序复制到缓存目录后启动（主程序下载完成后调用）。

    **必须是副本**：更新程序自己也在安装目录里，原地替换时会被 Windows
    锁定；复制到 %TEMP% 再运行才能顺利替换整个目录。
    """
    install_dir = Path(install_dir)
    source = install_dir / updater_name
    if not source.is_file():
        raise UpdateError(f"安装目录里没有 {updater_name}，请到发布页手动下载完整包")
    work_dir = Path(work_dir) if work_dir else Path(zip_path).parent
    work_dir.mkdir(parents=True, exist_ok=True)
    runner = work_dir / updater_name
    if runner.exists():
        runner.unlink(missing_ok=True)
    shutil.copy2(source, runner)

    args = [
        str(runner),
        "--pid", str(pid if pid is not None else os.getpid()),
        "--zip", str(Path(zip_path)),
        "--dir", str(install_dir),
        "--exe", exe_name,
    ]
    return subprocess.Popen(args, cwd=str(work_dir), creationflags=_detached_flags(),
                            close_fds=True)
