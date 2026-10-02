"""漫画下载与解压模块。

从 wnacg_crawler 拆出下载相关逻辑，保持单文件规模可控：
- 整包下载：逆向签名直链 → 下载 zip → 7z/zipfile 自动解压
- 图片逐张下载：分批解析图片页 → 并发下载
- 下载文件夹命名（含右上角分类标签）
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path, PurePosixPath

from lxml import html

# 独立运行时（sys.path[0] 为 src/ 目录）需将项目根加入路径，统一按 src.* 包导入。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ProjectConfig
from src.net import _throttle, fetch, fetch_page_with_cookies
from src.utils import IMAGE_SUFFIXES, display_title_for_comic, safe_filename
from src.wnacg_crawler import (
    G_URL,
    HEADERS,
    download_pictures,
    parse_picture_page,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 7z 子进程登记表
#
# 7z.exe 由 subprocess.Popen 启动，是独立于本进程的后台子进程。若解压所在线程
# 被强制终止（terminate）或应用在解压中途退出，局部变量 proc 随之丢失，7z 会
# 变成孤儿进程继续占用文件句柄——表现为"程序关了，但 7z 还在跑"。
#
# 因此在进程启动时登记、结束时注销；应用退出收尾（shutdown_all）时统一
# terminate + 限时等待 + kill，确保不留孤儿。
# ---------------------------------------------------------------------------
_child_procs: set[subprocess.Popen] = set()
_child_procs_lock = threading.Lock()


def _register_proc(proc: subprocess.Popen) -> None:
    with _child_procs_lock:
        _child_procs.add(proc)


def _unregister_proc(proc: subprocess.Popen) -> None:
    with _child_procs_lock:
        _child_procs.discard(proc)


def terminate_child_processes(timeout_ms: int = 1500) -> int:
    """终止所有仍在运行的子进程（7z 等），返回实际终止的数量。

    先 terminate 优雅结束，限时等待后仍未退出则 kill。重复调用安全：
    已结束的进程立即跳过。应用退出前调用，避免留下孤儿 7z.exe。
    """
    with _child_procs_lock:
        procs = list(_child_procs)
    killed = 0
    for proc in procs:
        try:
            if proc.poll() is not None:
                _unregister_proc(proc)
                continue
            proc.terminate()
        except Exception:  # noqa: BLE001 - 收尾期任何异常都不应中断流程
            _unregister_proc(proc)
            continue
        try:
            proc.wait(timeout=timeout_ms / 1000)
        except Exception:  # noqa: BLE001 - 含 TimeoutExpired
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        _unregister_proc(proc)
        killed += 1
    if killed:
        logger.info("已终止 %d 个子进程（7z 等）", killed)
    return killed


# 下载页 <script> 中的 const CONFIG = { WORKER_API / FILE_KEY / FILE_NAME }
CONFIG_RE = re.compile(r"const\s+CONFIG\s*=\s*\{(?P<body>.*?)\};", re.DOTALL)
CONFIG_FIELD_RE = re.compile(
    r'(?P<key>WORKER_API|FILE_KEY|FILE_NAME)\s*:\s*["\'](?P<value>.*?)["\']'
)


def fetch_download_package_info(detail_url: str) -> dict:
    """逆向 wnacg 整包下载接口，返回可下载的 zip 直链信息。

    流程（来自浏览器抓包逆向）：
    1. GET /download-index-aid-{aid}.html，提取 <script> 中的
       const CONFIG = { WORKER_API, FILE_KEY, FILE_NAME }
    2. POST WORKER_API，JSON body {"file_key": ..., "file_name": ...}，
       换取带签名与过期时间的直链
    3. 返回 {"url": 直链, "file_name": 文件名, "aid": aid}

    注意：d1.wcdn.date 受 Cloudflare 保护并校验浏览器指纹（TLS/HTTP2）。
    generate-link 调用使用 curl_cffi 模拟 Chrome 指纹（impersonate），
    避免 requests 因指纹差异被 403 拦截。

    Raises:
        ValueError: 详情链接中无法提取 aid。
        RuntimeError: 无 CONFIG 配置或 generate-link 返回异常。
    """
    match = re.search(r"aid-(\d+)", detail_url or "")
    if not match:
        raise ValueError(f"无法从详情链接提取 aid: {detail_url}")
    aid = match.group(1)

    # 1. 获取下载页，提取 CONFIG 配置
    page_url = f"{G_URL}/download-index-aid-{aid}.html"
    page_html, page_cookies = fetch_page_with_cookies(page_url, HEADERS)
    config_match = CONFIG_RE.search(page_html)
    if not config_match:
        raise RuntimeError(
            f"下载页未找到 CONFIG 配置（aid={aid}，可能无整包下载）"
        )
    config = dict(CONFIG_FIELD_RE.findall(config_match.group("body")))
    worker_api = config.get("WORKER_API")
    file_key = config.get("FILE_KEY")
    file_name = config.get("FILE_NAME")
    if not (worker_api and file_key and file_name):
        raise RuntimeError(f"下载页 CONFIG 字段不完整: {config}（aid={aid}）")

    logger.info("整包下载配置: aid=%s, 文件名=%s", aid, file_name)

    # 2. 调用 generate-link API 换取带签名的下载直链
    url = _generate_link(worker_api, file_key, file_name, page_cookies)

    logger.info("整包下载直链已生成: aid=%s", aid)
    return {"url": url, "file_name": file_name, "aid": aid}


def _generate_link(worker_api: str, file_key: str, file_name: str, cookies: str) -> str:
    """用 curl_cffi（模拟 Chrome TLS/HTTP2 指纹）调用 generate-link API。

    携带下载页响应中的 Cookie，并通过 impersonate 模拟 Chrome 指纹，
    绕过 Cloudflare 的 TLS/HTTP2 指纹校验（requests 会被 403 拦截）。

    Args:
        cookies: 下载页请求得到的 Cookie 字符串（与 HTML 同一次请求取得）。
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as exc:
        raise RuntimeError(
            "未安装 curl_cffi，请执行: pip install curl_cffi"
        ) from exc

    headers = {
        **HEADERS,
        "Accept": "*/*",
        "Origin": G_URL,
        "Referer": f"{G_URL}/",
        "Cookie": cookies,
    }
    resp = cffi_requests.post(
        worker_api,
        headers=headers,
        json={"file_key": file_key, "file_name": file_name},
        impersonate="chrome",
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success") or not data.get("url"):
        raise RuntimeError(f"generate-link 返回异常: {data}")
    return data["url"]


def _download_folder_name(comic: dict, use_label: bool = True) -> str:
    """生成下载文件夹基础名：`[右上角分类标签] 显示名`；use_label=False 或无线标签时仅用标题。

    显示名优先取翻译名（AI 翻译 > 标题自带中文 > 原标题），
    与卡片显示一致；名称冲突由 _resolve_save_dir 追加作品 ID 处理。
    """
    label = (comic.get("category_label") or "").strip()
    title = display_title_for_comic(
        comic.get("title", ""), comic.get("translated_title", "")
    )
    name = f"[{label}] {title}" if use_label and label else title
    return safe_filename(name)


def _extract_aid(detail_url: str) -> str:
    """从详情链接提取作品 ID（aid），提取失败时返回空字符串。"""
    match = re.search(r"aid-(\d+)", detail_url or "")
    return match.group(1) if match else ""


def _read_title_info(save_dir: Path) -> dict:
    """读取作品目录里的 title_info.json；缺失或损坏时返回空字典。"""
    info = save_dir / "title_info.json"
    if not info.exists():
        return {}
    try:
        data = json.loads(info.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_save_dir(save_dir: Path, comic: dict) -> Path:
    """处理下载目录命名冲突：已存在同名目录且非本作品时追加 (aid) 保证唯一。

    判定规则：
    - 目录不存在：直接使用；
    - title_info.json 里的 aid 与本作品一致：同一作品，复用目录
      （图片模式断点续传会自动跳过已下载文件）；
    - 目录没存 aid（本字段出现之前建的旧目录）：退回比较 original_title；
    - 其余情况（撞名，不同作品同名）：返回 `名称 (aid)` 目录。

    阅读缓存目录也会写 title_info.json（见 _ensure_title_info），因此
    「先在线阅读缓存了一批、之后点下载全本」同样命中这条规则，复用同一
    目录，不会另建 `名称 (aid)` 导致同一作品两份、缓存图片白下第二遍。
    """
    if not save_dir.exists():
        return save_dir
    data = _read_title_info(save_dir)
    aid = _extract_aid(comic.get("detail_url", ""))
    stored_aid = str(data.get("aid") or "").strip()
    if stored_aid and aid:
        # 有作品 ID 就以 ID 为准：标题相同但 ID 不同的确是两部作品
        if stored_aid == aid:
            return save_dir
    elif data.get("original_title") == comic.get("title", ""):
        return save_dir  # 旧目录没存 ID：退回标题比较（保持原有行为）
    if aid:
        return save_dir.parent / f"{save_dir.name} ({aid})"
    return save_dir


def _find_7z() -> str | None:
    """查找系统 7z 可执行文件（PATH 或常见安装目录），未找到返回 None。"""
    path = shutil.which("7z") or shutil.which("7za")
    if path:
        return path
    for candidate in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def _safe_zip_members(zf: zipfile.ZipFile) -> list[str]:
    """校验 zip 内所有条目路径安全，返回通过校验的成员名列表。

    防御 zip-slip（CVE-2007-4559，Python < 3.11 的 extract 无内置防护）：
    - 拒绝绝对路径（如 ``/etc/passwd``）与盘符前缀（如 ``C:\\evil``）；
    - 拒绝含 ``..`` 段的条目（可写出到解压目录之外）；
    - 拒绝符号链接条目（symlink 可指向任意路径，配合后续写入造成写穿）。

    Raises:
        ValueError: 存在不安全条目时抛出（中止解压）。
    """
    members = []
    for info in zf.infolist():
        raw = info.filename
        parts = PurePosixPath(raw.replace("\\", "/")).parts
        if (
            raw.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:", raw)
            or any(part == ".." for part in parts)
            or (info.external_attr >> 16) & 0o170000 == 0o120000  # S_IFLNK
        ):
            raise ValueError(f"压缩包包含不安全的条目路径: {raw!r}")
        members.append(raw)
    return members


def _prune_shadowed_copies(save_dir: Path, archive_names: list[str]) -> int:
    """删除被压缩包原文件取代的旧缓存副本，保证同一页只留一份。

    阅读器边看边缓存时按 `页码.jpg` 落盘，而整包解压出的是压缩包内的原始
    文件名（站点多打包成 `001.webp`）：两者**主文件名相同、扩展名不同**，
    同一页于是存了两份。这里以「主文件名（不含扩展名）相同」为判据，删除
    被刚解压出的原件取代的旧副本。

    判据刻意不用「末位数字相同」这种更宽松的页码匹配：合集类作品里
    `05_01.jpg` 与 `05_preⅢ_01.png` 是不同图，末位数字却都是 1，
    按页码匹配会误删。代价是缓存名与压缩包名连主文件名都不同时
    （如 `1.jpg` 对 `001.webp`）清不掉，这种由断点续传那侧兜住。

    Returns:
        删除的文件数。
    """
    if not save_dir.is_dir() or not archive_names:
        return 0
    keep_names = {PurePosixPath(name.replace("\\", "/")).name for name in archive_names}
    archive_stems = {Path(name).stem.lower() for name in keep_names if Path(name).stem}
    if not archive_stems:
        return 0

    removed = 0
    for path in save_dir.rglob("*"):
        if not path.is_file() or path.name in keep_names:
            continue
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if path.stem.lower() not in archive_stems:
            continue
        try:
            path.unlink()
            removed += 1
            logger.info("清理同页重复副本: %s", path.name)
        except OSError as exc:
            logger.warning("清理同页重复副本失败 %s: %s", path.name, exc)
    return removed


def _extract_archive(
    archive_path: Path,
    save_dir: Path,
    progress_callback=None,
    total_files: int = 0,
    base: int = 0,
    span: int = 100,
) -> None:
    """解压压缩包到 save_dir：优先使用 7z（快、中文文件名稳），回退内置 zipfile。

    解压前统一校验 zip 条目路径安全（zip-slip 防御），
    无论 7z 还是 zipfile 模式，不安全的压缩包一律拒绝解压。

    Args:
        progress_callback: 可选回调 (当前进度, 100)。进度统一为 0-100 的百分比，
            可用 base/span 将本阶段映射到整体进度区间（如下载占 0-80，解压占 80-100）。
        total_files: zip 内的文件总数（zipfile 模式用于逐文件进度计算）。
        base/span: 本阶段在整体进度中的起始值与跨度（默认 0-100）。
    """
    with zipfile.ZipFile(archive_path) as zf:
        safe_names = _safe_zip_members(zf)
    total_files = total_files or len(safe_names)

    seven_zip = _find_7z()
    if seven_zip:
        try:
            _extract_with_7z(
                seven_zip, archive_path, save_dir, progress_callback, total_files, base, span
            )
        except Exception as exc:
            logger.warning("7z 解压失败，回退内置 zipfile: %s", exc)
        else:
            _prune_shadowed_copies(save_dir, safe_names)
            return

    logger.info("使用内置 zipfile 解压: %s", archive_path)
    with zipfile.ZipFile(archive_path) as zf:
        for i, name in enumerate(safe_names, 1):
            zf.extract(name, save_dir)
            if progress_callback and total_files:
                pct = min(int(i * span / total_files), span)
                progress_callback(base + pct, 100)
    _prune_shadowed_copies(save_dir, safe_names)


def _extract_with_7z(
    seven_zip: str,
    archive_path: Path,
    save_dir: Path,
    progress_callback=None,
    total_files: int = 0,
    base: int = 0,
    span: int = 100,
) -> None:
    """用 7z 解压，通过监控落盘文件数汇报实时进度。

    7z 命令行不输出逐文件百分比（-bsp1 对 zip 包无效），因此启动一个监控线程
    统计 save_dir 下已解压出的文件数量，按 total_files 换算为百分比回调。

    Windows 下以 CREATE_NO_WINDOW 启动 7z，避免下载/解压时弹出 cmd 黑窗闪烁。
    """
    # 仅 Windows 生效：不创建控制台窗口（7z.exe 是控制台程序，默认会闪窗）
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    # 用 cwd 指定解压目标目录而非 `-o` 参数：7z 的 -o 对含空格路径解析不可靠，
    # cwd 方式彻底规避，且解压结构（含归档内子目录）与目标目录语义一致。
    # 压缩包与 cwd 必须传绝对路径：归档路径为相对路径（如 ./Download/xxx.part）
    # 时，7z 会以 cwd（中文下载目录）为基准解析而找不到文件，报
    # "系统找不到指定的文件"（实测 returncode=2，7z 始终回退 zipfile）。
    save_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [seven_zip, "x", "-y", str(archive_path.resolve())],
        cwd=str(save_dir.resolve()),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    # 登记子进程：解压线程被强杀 / 应用退出时由 terminate_child_processes 统一收尾
    _register_proc(proc)

    stop_monitor = threading.Event()

    def _monitor():
        last = 0
        while not stop_monitor.is_set():
            if progress_callback and total_files and save_dir.exists():
                count = sum(
                    1 for _ in save_dir.rglob("*") if _.is_file()
                )
                if count != last:
                    last = count
                    pct = min(int(count * span / total_files), span)
                    progress_callback(base + pct, 100)
            stop_monitor.wait(0.5)

    monitor = threading.Thread(target=_monitor, daemon=True)
    monitor.start()
    try:
        proc.wait(timeout=600)
    finally:
        stop_monitor.set()
        monitor.join(timeout=2)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        _unregister_proc(proc)
    if proc.returncode != 0:
        # 中文系统 7z 输出为 GBK/cp936，按编码优先级尝试解码，失败再宽容替换
        raw_err = proc.stderr.read()
        stderr = None
        for encoding in ("gbk", "utf-8"):
            try:
                stderr = raw_err.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if stderr is None:
            stderr = raw_err.decode("utf-8", "replace")
        raise RuntimeError(f"7z 解压失败: {stderr}")
    if progress_callback:
        progress_callback(base + span, 100)


def _download_package(
    comic: dict,
    save_dir: Path,
    progress_callback=None,
    stage_callback=None,
):
    """整包下载模式：获取签名直链 → 下载 zip → 自动解压到 save_dir。

    Args:
        progress_callback: 可选回调 (当前, 100)。进度统一为 0-100 的百分比：
            下载阶段按字节计算占 0-80，解压阶段按实时进度占 80-100，进度条平滑连续。
        stage_callback: 可选回调(str)，通知当前阶段（获取下载链接/下载压缩包/解压中）。
    """
    from curl_cffi import requests as cffi_requests

    if stage_callback:
        stage_callback("获取下载链接")
    info = fetch_download_package_info(comic["detail_url"])
    url = info["url"]
    zip_name = info["file_name"] or f"{comic.get('title', 'comic')}.zip"

    tmp = save_dir.parent / f"{safe_filename(zip_name)}.part"
    save_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 1. 下载 zip（curl_cffi 模拟 Chrome 指纹，避免被 Cloudflare 拦截）
        if stage_callback:
            stage_callback("下载压缩包")
        _throttle()
        resp = cffi_requests.get(
            url, headers=HEADERS, impersonate="chrome", stream=True, timeout=180
        )
        try:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total:
                        # 下载阶段占整体进度的 0-80（按字节精确计算，不再用 MB 粗粒度）
                        pct = int(downloaded * 80 / total)
                        progress_callback(min(pct, 80), 100)
            if total and downloaded != total:
                raise RuntimeError(
                    f"压缩包下载不完整: 已下载 {downloaded} / {total} 字节"
                )
        finally:
            resp.close()

        # 2. 自动解压到与图片模式一致的 [分类标签] 标题 文件夹
        if stage_callback:
            stage_callback("解压中")
        save_dir.mkdir(parents=True, exist_ok=True)
        total_files = 0
        try:
            with zipfile.ZipFile(tmp) as zf:
                total_files = len(
                    [n for n in zf.namelist() if not n.endswith("/")]
                )
        except zipfile.BadZipFile:
            total_files = 0
        # 解压阶段占整体进度的 80-100，与下载阶段平滑衔接
        _extract_archive(tmp, save_dir, progress_callback, total_files, base=80, span=20)
        logger.info(
            "整包下载完成并解压: %s, 共 %d 个文件", save_dir, total_files
        )
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _extract_reading_page_url(detail_doc) -> str | None:
    """从详情页提取阅读页 URL（含 page-N-aid-M，N 为总页数）。

    优先收集所有指向阅读页的链接（photos-index-page-*），取最后一个
    （列表按页码升序排列，最后一个是末页，其 page-N 即为总页数）；
    找不到时回退到历史版本绝对路径。
    """
    links = detail_doc.xpath(
        '//a[contains(@href, "photos-index-page")]/@href'
    )
    if not links:
        links = detail_doc.xpath(
            '//div[contains(@class, "pic_box")]//a[contains(@href, "aid-")]/@href'
        )
    if not links:
        links = detail_doc.xpath(
            '//*[@id="bodywrap"]/div[2]/div/div/div[1]/a[last()]/@href'
        )
    return links[-1] if links else None


def _write_title_info(save_dir: Path, comic: dict) -> None:
    """在作品目录内写入标题信息 JSON（原始名 + 翻译名 + 作品 ID）。

    文件名为 title_info.json，包含 original_title（原始名称）、
    translated_title（翻译名称，未翻译时为空）与 aid（作品 ID）。
    只存 ID 不存 detail_url；下载侧靠 aid 判断同名目录是否属于同一作品。
    """
    info = {
        "original_title": comic.get("title", ""),
        "translated_title": comic.get("translated_title", ""),
        "aid": _extract_aid(comic.get("detail_url", "")),
    }
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / "title_info.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("已写入标题信息: %s/title_info.json", save_dir)
    except OSError as exc:
        logger.warning("写入标题信息 JSON 失败: %s", exc)


def _ensure_title_info(save_dir: Path, comic: dict) -> None:
    """title_info.json 不存在时才写入。

    阅读器「边看边缓存」会自己创建作品目录，但没走下载流程，也就不会写
    title_info.json。缺了它，之后点「下载全本」时 _resolve_save_dir 认不出
    这是同一部作品，会另建 `名称 (aid)` 目录：同一部作品出现两个文件夹，
    已缓存的那部分图片还要被重新下一遍。已有文件时不覆盖（可能带着更完整
    的翻译名）。
    """
    if (save_dir / "title_info.json").exists():
        return
    _write_title_info(save_dir, comic)


def download_comic(
    comic: dict,
    base_dir: Path | None = None,
    progress_callback=None,
    stage_callback=None,
    batch_pages: int = 4,
):
    """按配置的下载方式下载单部漫画。

    - Download_mode == "zip"：整包下载，获取签名直链 → 下载 zip → 自动解压。
    - 否则（image）：分批解析图片页并下载：解析 N 页 → 下载 → 再解析 N 页 → 再下载。

    Args:
        progress_callback: 可选回调 (当前, 总)。
            图片模式单位为张数（总张数随批次推进增长）；
            压缩包模式为 0-100 百分比（下载 0-80，解压 80-100）。
        stage_callback: 可选回调(str)，通知当前阶段（解析/下载/解压等）。
        batch_pages: 每批解析的页数，默认 4（仅图片模式生效）。
        并发数与重试次数从 Config/Config.json 读取（Download_workers / Download_retries）。
    """
    config = ProjectConfig()
    if base_dir is None:
        base_dir = Path(config.download_dir)
    use_label = config.folder_use_category_label
    save_dir = base_dir / _download_folder_name(comic, use_label)
    # 名称冲突（同翻译名不同作品）时自动追加作品 ID，保证目录唯一
    save_dir = _resolve_save_dir(save_dir, comic)

    # 压缩包下载模式：下载整包 zip 并自动解压
    if config.download_mode == "zip":
        if stage_callback:
            stage_callback("准备")
        _download_package(comic, save_dir, progress_callback, stage_callback)
        # 解压完成后写入标题信息 JSON
        _write_title_info(save_dir, comic)
        return

    detail_html = fetch(G_URL + comic["detail_url"], HEADERS)
    reading_url = _extract_reading_page_url(html.fromstring(detail_html))
    if not reading_url:
        logger.warning("未找到阅读页链接: %s", comic["title"])
        return

    # 从阅读页 URL 提取总页数
    match = re.search(r"page-(\d+)-aid-(\d+)", reading_url)
    page_count = int(match.group(1)) if match else 1
    workers = config.download_workers
    retries = config.download_retries

    batch_pages = max(1, batch_pages)
    grand_done = 0
    grand_total = 0
    for batch_start in range(1, page_count + 1, batch_pages):
        batch_end = min(batch_start + batch_pages - 1, page_count)
        logger.info(
            "解析第 %d-%d 页并下载（共 %d 页）", batch_start, batch_end, page_count
        )
        if stage_callback:
            stage_callback(f"解析图片 {batch_start}-{batch_end} 页")
        batch = parse_picture_page(
            reading_url, start=batch_start, end=batch_end
        )
        if not batch:
            logger.warning("第 %d-%d 页未解析到图片，跳过", batch_start, batch_end)
            continue

        grand_total += len(batch)
        if progress_callback:
            progress_callback(grand_done, grand_total)

        if stage_callback:
            stage_callback(f"下载图片 {batch_start}-{batch_end} 页")

        # 默认参数绑定当前批次的累计值，避免闭包捕获循环变量（若改异步则防患未然）
        def _batch_cb(cur: int, _total: int, _base: int = grand_done, _all: int = grand_total):
            progress_callback(_base + cur, _all)

        failed = download_pictures(
            batch,
            save_dir,
            _batch_cb if progress_callback else None,
            max_workers=workers,
            max_retries=retries,
        )
        grand_done += len(batch) - failed
        logger.info(
            "第 %d-%d 页下载完成，累计 %d/%d 张图片",
            batch_start, batch_end, grand_done, grand_total,
        )

    if grand_done == 0:
        raise RuntimeError(
            f"未解析到任何图片链接（作品: {comic.get('title', '')}），"
            "页面结构可能变化或已被反爬拦截，详见日志"
        )

    # 图片下载完成后写入标题信息 JSON
    _write_title_info(save_dir, comic)
