"""独立更新程序：等待主程序退出 → 校验解压更新包 → 备份替换 → 重启主程序。

由主程序在下载完成后启动（见 `src/update_apply.py::launch_updater`），参数：

    --pid <主程序 PID> --zip <更新包 zip> --dir <安装目录> --exe 漫画浏览器.exe

为什么要独立进程：Windows 会锁定正在运行的 exe，主程序无法覆盖自己。
为什么必须复制到 %TEMP% 运行：更新程序自己也在安装目录里，原地替换时
同样被锁定（`launch_updater` 已处理复制）。

**只依赖标准库**（进度窗用 tkinter 画，失败则退化为静默运行）：更新程序
要能独立运行、体积小、启动快，不能依赖 PySide6，也不能依赖 src 里的
其他模块（除了同样纯标准库的 `src.update_apply`）。
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

# 允许以脚本方式直接运行（打包后同目录即为程序根）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.update_apply import (  # noqa: E402  （需先补 sys.path）
    BACKUP_DIR_NAME,
    REQUIRED_FILES,
    UpdateError,
    extract_payload,
    finish_update,
    inspect_zip,
    replace_program_files,
    rollback,
    start_app,
    wait_for_process_exit,
)

logger = logging.getLogger("updater")

# 启动新版本后观察多久判断"是否启动成功"（进程立刻退出视为失败）
HEALTH_CHECK_SECONDS = 6.0
# 等待主程序退出的上限
EXIT_WAIT_SECONDS = 60.0


# ---- 进度窗（tkinter，可选） ----


class ProgressWindow:
    """最小进度窗；`enabled=False` 或 tkinter 不可用时所有方法退化为空操作。"""

    def __init__(self, title: str = "正在更新漫画浏览器", enabled: bool = True):
        self._root = None
        self._status = None
        self._bar = None
        if not enabled:
            return
        try:
            import tkinter as tk
            from tkinter import ttk

            self._root = tk.Tk()
            self._root.title(title)
            self._root.geometry("420x130")
            self._root.resizable(False, False)
            self._root.attributes("-topmost", True)
            tk.Label(self._root, text="漫画浏览器 更新程序", font=("Microsoft YaHei UI", 12, "bold")).pack(pady=(14, 4))
            self._status = tk.Label(self._root, text="准备中…", font=("Microsoft YaHei UI", 9))
            self._status.pack()
            self._bar = ttk.Progressbar(self._root, length=360, mode="determinate", maximum=100)
            self._bar.pack(pady=10)
            self._pump()
        except Exception as exc:  # 无图形环境 / tkinter 缺失：静默运行
            logger.info("进度窗不可用（%s），改为静默更新", exc)
            self._root = None

    def _pump(self) -> None:
        if self._root is not None:
            try:
                self._root.update_idletasks()
                self._root.update()
            except Exception:
                self._root = None

    def status(self, text: str) -> None:
        logger.info("%s", text)
        if self._status is not None and self._root is not None:
            try:
                self._status.config(text=text)
            except Exception:
                pass
        self._pump()

    def progress(self, done: int, total: int) -> None:
        if self._bar is None or not total:
            return
        try:
            self._bar["value"] = min(100, int(done * 100 / total))
        except Exception:
            return
        self._pump()

    def error(self, message: str) -> None:
        logger.error("%s", message)
        if self._root is None:
            return
        try:
            from tkinter import messagebox

            messagebox.showerror("更新失败", message)
        except Exception:
            pass
        self.close()

    def info(self, message: str) -> None:
        if self._root is None:
            return
        try:
            from tkinter import messagebox

            messagebox.showinfo("更新完成", message)
        except Exception:
            pass

    def close(self) -> None:
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None


# ---- 主流程 ----


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="漫画浏览器独立更新程序")
    parser.add_argument("--pid", type=int, default=0, help="等待退出的主程序 PID")
    parser.add_argument("--zip", required=True, help="更新包 zip 路径")
    parser.add_argument("--dir", required=True, help="安装目录（主程序所在目录）")
    parser.add_argument("--exe", default=REQUIRED_FILES[0], help="主程序文件名")
    parser.add_argument("--no-ui", action="store_true", help="不显示进度窗（调试用）")
    return parser.parse_args(argv)


def setup_logging(install_dir: Path) -> None:
    handlers: list[logging.Handler] = []
    try:
        log_dir = Path(install_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "updater.log", encoding="utf-8"))
    except OSError:
        pass
    handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def run_update(args: argparse.Namespace, ui: ProgressWindow) -> None:
    """执行更新；任一步失败抛 UpdateError（调用方负责回滚与提示）。"""
    install_dir = Path(args.dir)
    zip_path = Path(args.zip)

    ui.status("等待主程序退出…")
    if not wait_for_process_exit(args.pid, EXIT_WAIT_SECONDS):
        raise UpdateError("主程序仍在运行，更新已取消。请关闭程序后重试。")

    ui.status("校验更新包…")
    inspect_zip(zip_path)

    ui.status("解压更新包…")
    staging = zip_path.parent / "staging"
    payload_root = extract_payload(zip_path, staging, on_progress=ui.progress)

    ui.status("替换程序文件…")
    backup_dir = replace_program_files(payload_root, install_dir, on_status=ui.status)

    ui.status("启动新版本…")
    proc = start_app(install_dir, args.exe)
    time.sleep(HEALTH_CHECK_SECONDS)
    if proc.poll() is not None:  # 立刻退出 = 启动失败
        raise UpdateError(f"新版本启动失败（退出码 {proc.returncode}），已回滚到旧版本")
    finish_update(backup_dir)
    shutil.rmtree(staging, ignore_errors=True)
    zip_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    install_dir = Path(args.dir)
    setup_logging(install_dir)
    ui = ProgressWindow(enabled=not args.no_ui)
    backup_dir = install_dir / BACKUP_DIR_NAME
    try:
        run_update(args, ui)
    except Exception as exc:
        logger.exception("更新失败")
        message = f"更新失败：{exc}"
        if backup_dir.is_dir():
            try:
                ui.status("回滚到旧版本…")
                rollback(install_dir, backup_dir)
                message += "\n已恢复原版本。"
            except Exception as revert_exc:
                logger.exception("回滚失败")
                message += f"\n回滚也失败了：{revert_exc}\n请到发布页手动下载完整包。"
            else:
                # 回滚成功 ≠ 重启成功：重启失败只是提示，不能说成回滚失败
                try:
                    start_app(install_dir, args.exe)
                except Exception:
                    logger.exception("回滚后重启旧版失败")
                    message += "\n（请手动双击程序目录里的主程序继续使用旧版本）"
        ui.error(message)
        return 1
    ui.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
