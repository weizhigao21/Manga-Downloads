"""更新检查：查询 GitHub Releases、比较版本、挑选更新包并下载。

纯标准库 + requests，不导入 Qt（离线可测；耗时操作由 UI 层放进线程）。

发布约定（与 `打包.bat` / `更新程序.spec` 配套）：
- Release tag 形如 `v1.3.14`，与 `ui/version.py::__version__`（`1.3.14`）对齐；
- 更新包是**整个程序目录的 zip**，名字形如 `漫画浏览器-v1.3.14.zip`，
  内含 `漫画浏览器.exe` 与 `_internal/`（与 onedir 产物一致）。
"""
from __future__ import annotations

import logging
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# 发布仓库：更新检查走**公开**仓库（私有仓库匿名访问 404，终端用户查不到）
GITHUB_REPO = "weizhigao21/Manga-Downloads"
API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{GITHUB_REPO}/releases"

# 下载/查询的代理候选：先直连，失败再试本机常见代理端口（Clash 默认 7897）。
# 国内直连 GitHub 常超时，这一句省掉用户手动配置。
DEFAULT_PROXIES: tuple[str | None, ...] = (None, "http://127.0.0.1:7897")

_CACHE_DIR_NAME = "漫画浏览器更新"

_UA = {
    "User-Agent": "Manga-Downloads-Updater",
    "Accept": "application/vnd.github+json",
}
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")
_CHUNK = 64 * 1024


class UpdateError(RuntimeError):
    """更新检查/下载失败；消息可直接展示给用户。"""


@dataclass
class ReleaseInfo:
    """一个可用的新版本（含挑中的更新包）。"""

    version: str          # 1.3.14（去掉 v 前缀）
    tag: str              # v1.3.14
    title: str            # Release 标题
    notes: str            # 更新说明（markdown 原文）
    published_at: str
    asset_name: str
    asset_url: str
    asset_size: int       # 字节
    page_url: str


# ---- 版本比较 ----


def parse_version(text: str) -> tuple[int, ...]:
    """把 `v1.3.14` / `1.3.14` / `1.3.14-beta1` 解析成可比较的数字元组。

    只取**第一段连续数字**（`1.3.14` → `(1, 3, 14)`）；解析不出返回空元组，
    调用方应视为"无法比较"而不是"已是最新"，避免把异常 tag 当成新版。
    """
    match = _VERSION_RE.search(str(text or ""))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(remote: str, local: str) -> bool:
    """远端版本是否比本地新；任一侧解析失败时保守返回 False。"""
    remote_parts = parse_version(remote)
    local_parts = parse_version(local)
    if not remote_parts or not local_parts:
        return False
    return remote_parts > local_parts


# ---- Release 解析 ----


def pick_asset(assets: list[dict], version: str = "") -> dict | None:
    """从 Release 资源里挑更新包：优先名字含目标版本的 zip，其次任意 zip。"""
    zips = [
        asset
        for asset in (assets or [])
        if str(asset.get("name", "")).lower().endswith(".zip")
    ]
    if not zips:
        return None
    if version:
        for asset in zips:
            if version in str(asset.get("name", "")):
                return asset
    return zips[0]


def parse_release(data: dict) -> ReleaseInfo | None:
    """把 GitHub API 的 release JSON 解析成 ReleaseInfo；无可用 zip 返回 None。"""
    tag = str(data.get("tag_name") or "").strip()
    version = ".".join(str(p) for p in parse_version(tag))
    asset = pick_asset(data.get("assets") or [], version)
    if asset is None:
        return None
    if not version:  # tag 里没有数字：退回资源名前缀，仍比不出高低
        version = str(asset.get("name") or "").replace(".zip", "")
    return ReleaseInfo(
        version=version,
        tag=tag or f"v{version}",
        title=str(data.get("name") or tag or version),
        notes=str(data.get("body") or ""),
        published_at=str(data.get("published_at") or ""),
        asset_name=str(asset.get("name") or ""),
        asset_url=str(asset.get("browser_download_url") or ""),
        asset_size=int(asset.get("size") or 0),
        page_url=str(data.get("html_url") or RELEASE_PAGE),
    )


def _proxy_map(proxy: str | None) -> dict | None:
    return {"http": proxy, "https": proxy} if proxy else None


def fetch_latest_release(timeout: int = 15) -> ReleaseInfo:
    """查询最新 Release；失败抛 UpdateError（消息可直接展示）。

    依次尝试 `DEFAULT_PROXIES` 里的直连与本地代理：直连失败再走代理，
    两者都失败才报错。
    """
    last_error: Exception | None = None
    for proxy in DEFAULT_PROXIES:
        try:
            resp = requests.get(
                API_LATEST, headers=_UA, timeout=timeout, proxies=_proxy_map(proxy)
            )
            if resp.status_code == 404:
                raise UpdateError("发布仓库不存在或未公开，无法检查更新")
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                raise UpdateError("GitHub 接口访问频繁被限流，请稍后再试")
            resp.raise_for_status()
            release = parse_release(resp.json())
            if release is None:
                raise UpdateError("最新版本没有提供 zip 更新包")
            return release
        except UpdateError:
            raise
        except Exception as exc:  # 网络异常：换下一个代理候选
            last_error = exc
            logger.info("检查更新失败（%s）: %s", proxy or "直连", exc)
    raise UpdateError(f"无法连接 GitHub：{last_error}")


# ---- 下载 ----


def update_cache_dir() -> Path:
    """更新包缓存目录：`%TEMP%/漫画浏览器更新`（更新程序也从这里取包）。"""
    return Path(tempfile.gettempdir()) / _CACHE_DIR_NAME


def download_asset(
    release: ReleaseInfo,
    dest_dir: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    timeout: tuple[int, int] = (10, 60),
) -> Path:
    """下载更新包，返回 zip 路径（先写 `.part`，成功后改名，断点不残留）。

    Args:
        on_progress: 回调 (已下载字节, 总字节)；总字节未知时第二参为 0。
        cancel_check: 返回 True 时中止并抛 UpdateError。
    """
    if not release.asset_url:
        raise UpdateError("更新包地址为空")
    dest_dir = Path(dest_dir) if dest_dir else update_cache_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / (release.asset_name or f"漫画浏览器-v{release.version}.zip")
    part = target.with_suffix(target.suffix + ".part")

    last_error: Exception | None = None
    for proxy in DEFAULT_PROXIES:
        try:
            with requests.get(
                release.asset_url,
                headers={"User-Agent": _UA["User-Agent"]},
                timeout=timeout,
                stream=True,
                proxies=_proxy_map(proxy),
                allow_redirects=True,
            ) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or release.asset_size or 0)
                done = 0
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(_CHUNK):
                        if cancel_check and cancel_check():
                            raise UpdateError("已取消下载")
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done += len(chunk)
                        if on_progress:
                            on_progress(done, total)
            if part.stat().st_size <= 0:
                raise UpdateError("下载到的更新包为空")
            if part.exists() and target.exists():
                target.unlink()
            part.replace(target)
            return target
        except UpdateError:
            part.unlink(missing_ok=True)
            raise
        except Exception as exc:  # 换下一个代理候选
            last_error = exc
            part.unlink(missing_ok=True)
            logger.info("下载更新包失败（%s）: %s", proxy or "直连", exc)
    raise UpdateError(f"下载更新包失败：{last_error}")


def cleanup_cache(keep: Path | None = None) -> None:
    """清理缓存目录（保留 `keep` 指向的文件）；失败只记日志。"""
    cache = update_cache_dir()
    if not cache.is_dir():
        return
    for entry in cache.iterdir():
        if keep is not None and entry == keep:
            continue
        try:
            if entry.is_dir():
                import shutil

                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError as exc:
            logger.info("清理更新缓存失败: %s (%s)", entry, exc)
