"""清理「同一页两种格式」的历史重复文件（阅读缓存 .jpg 与压缩包原件）。

背景：修复前阅读器边看边缓存固定写 `页码.jpg`，而整包下载解压出的是压缩包
内的原始文件名（站点多打包成 `页码.webp`），同一页于是在作品目录里存了两份。
新版本解压后会自动清理；本脚本用于清理修复之前留下的旧数据。

用法：
    python tools/dedupe_same_stem.py                 # 只扫描并报告，不动文件
    python tools/dedupe_same_stem.py --apply         # 确认后执行删除
    python tools/dedupe_same_stem.py "E:/hhh/漫画"   # 指定下载根目录

判据（刻意保守）：
    只看**主文件名（不含扩展名）相同、扩展名不同**的图片，且仅当组里恰好有
    一个 `.jpg` 时才判定它是多余副本（阅读缓存与图片模式下载都只会写出
    `.jpg`，非 `.jpg` 的文件只可能来自压缩包解压，是原件），保留原件删掉它。
    两种格式都不是 `.jpg`、或无法判断时，只报告不删除。

    刻意不按末位数字匹配：合集作品里 `05_01.jpg` 与 `05_preIII_01.png`
    末位数字都是 1，却是不同的图，按数字匹配会误删。
"""
import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils import IMAGE_SUFFIXES  # noqa: E402

# 阅读缓存与图片模式下载固定使用的扩展名
CACHE_SUFFIX = ".jpg"


def scan(download_dir: Path) -> tuple[list[tuple[Path, Path, Path]], list[Path]]:
    """返回 (可安全清理的 [(目录, 保留, 删除)], 需人工确认的目录)。"""
    removable: list[tuple[Path, Path, Path]] = []
    manual: list[Path] = []
    for root, _dirs, files in os.walk(download_dir):
        folder = Path(root)
        images = [f for f in files if Path(f).suffix.lower() in IMAGE_SUFFIXES]
        if len(images) < 2:
            continue
        by_stem: dict[str, list[str]] = defaultdict(list)
        for name in images:
            by_stem[Path(name).stem.lower()].append(name)

        ambiguous = False
        for group in by_stem.values():
            if len({Path(n).suffix.lower() for n in group}) < 2:
                continue
            cache_copies = [n for n in group if Path(n).suffix.lower() == CACHE_SUFFIX]
            originals = [n for n in group if Path(n).suffix.lower() != CACHE_SUFFIX]
            if len(cache_copies) != 1 or not originals:
                ambiguous = True
                continue
            keep = sorted(originals)[0]
            removable.append((folder, folder / keep, folder / cache_copies[0]))
        if ambiguous:
            manual.append(folder)
    return removable, manual


def main() -> int:
    parser = argparse.ArgumentParser(description="清理同一页两种格式的重复图片")
    parser.add_argument("download_dir", nargs="?", help="下载根目录（默认读配置）")
    parser.add_argument("--apply", action="store_true", help="确认执行删除（默认只报告）")
    args = parser.parse_args()

    if args.download_dir:
        base = Path(args.download_dir)
    else:
        from src.config import ProjectConfig

        base = Path(ProjectConfig().download_dir)

    if not base.is_dir():
        print(f"下载目录不存在: {base}")
        return 1

    print(f"扫描目录: {base}")
    removable, manual = scan(base)
    if not removable and not manual:
        print("没有发现同名词干的重复图片。")
        return 0

    wasted = sum(drop.stat().st_size for _f, _k, drop in removable if drop.exists())
    print(f"可清理: {len({f for f, _k, _d in removable})} 个作品目录、"
          f"{len(removable)} 个文件，约 {wasted / 1024 / 1024:.1f} MB")
    print("=" * 100)
    for folder, keep, drop in removable:
        print(f"{folder}")
        print(f"    保留 {keep.name:28} 删除 {drop.name}")

    if manual:
        print("=" * 100)
        print(f"以下 {len(manual)} 个目录含同名词干但两种都不是 .jpg，无法判断来源，只报告不处理：")
        for folder in manual:
            print(f"    {folder}")

    if not args.apply:
        print("=" * 100)
        print("以上只是预览，未删除任何文件。确认无误后加 --apply 再跑一次。")
        return 0

    removed = 0
    for _folder, _keep, drop in removable:
        try:
            drop.unlink()
            removed += 1
        except OSError as exc:
            print(f"删除失败 {drop}: {exc}")
    print(f"已删除 {removed} 个重复文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
