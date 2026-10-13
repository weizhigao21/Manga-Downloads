"""发布前自检：真实二进制端到端验证：用构建出的 更新程序.exe 跑一次"失败→回滚"。

场景：新版本的"主程序"是启动即退出的程序（用 where.exe 顶替）→ 更新程序的
健康检查判定新版本启动失败 → 必须回滚到旧版本，且用户数据（Config/Download/logs）
一字节不动。
"""
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPDATER = ROOT / "build" / "stage-updater" / "更新程序.exe"
STAND_IN_APP = Path(r"C:\Windows\System32\where.exe")  # 启动即退出，用来模拟"启动失败"


def main() -> int:
    assert UPDATER.is_file(), f"没有构建产物: {UPDATER}"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        install = tmp / "app"
        internal = install / "_internal"
        internal.mkdir(parents=True)
        # 旧版本：exe + _internal + 用户数据
        shutil.copy2(STAND_IN_APP, install / "漫画浏览器.exe")
        (internal / "old.dll").write_bytes(b"OLD-DLL")
        (install / "Config").mkdir()
        (install / "Config" / "comics.db").write_bytes(b"DB-CONTENT")
        (install / "Download").mkdir()
        (install / "Download" / "a.jpg").write_bytes(b"IMG")

        # 更新包：新 exe（同样启动即退出 → 判定失败）+ 新 _internal
        zip_path = tmp / "cache" / "漫画浏览器-v9.9.9.zip"
        zip_path.parent.mkdir(parents=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(STAND_IN_APP, "漫画浏览器.exe")
            zf.writestr("_internal/new.dll", b"NEW-DLL")

        # 更新程序被复制到临时目录再运行（模拟 launch_updater）
        runner = tmp / "runner" / "更新程序.exe"
        runner.parent.mkdir(parents=True)
        shutil.copy2(UPDATER, runner)

        proc = subprocess.run(
            [str(runner), "--no-ui", "--pid", "0", "--zip", str(zip_path),
             "--dir", str(install), "--exe", "漫画浏览器.exe"],
            capture_output=True, text=True, timeout=120, errors="replace",
        )
        print("退出码:", proc.returncode)
        print("stderr 尾部:", (proc.stderr or "").strip().splitlines()[-3:])

        problems = []
        if proc.returncode != 1:
            problems.append(f"失败场景应返回 1，实际 {proc.returncode}")
        if (install / "_internal" / "new.dll").exists():
            problems.append("新版本文件未回滚（new.dll 仍在）")
        if not (install / "_internal" / "old.dll").exists():
            problems.append("旧版本文件未恢复（old.dll 丢失）")
        if (install / ".backup-update").exists():
            problems.append("回滚后备份目录未清理")
        if (install / "Config" / "comics.db").read_bytes() != b"DB-CONTENT":
            problems.append("用户数据 Config/comics.db 被破坏")
        if (install / "Download" / "a.jpg").read_bytes() != b"IMG":
            problems.append("用户数据 Download/a.jpg 被破坏")
        if not (install / "漫画浏览器.exe").is_file():
            problems.append("主程序 exe 丢失")
        if not (install / "logs" / "updater.log").is_file():
            problems.append("更新日志未写入 logs/updater.log")

        if problems:
            print("\n❌ 验证失败：" + "\n  - ".join(problems))
            return 1
        print("\n✅ 真实二进制验证通过：失败已回滚、用户数据完好、日志已落盘")
        return 0


if __name__ == "__main__":
    sys.exit(main())
