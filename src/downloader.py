"""漫画下载与解压模块。

从 wnacg_crawler 拆出下载相关逻辑，保持单文件规模可控：
- 整包下载：逆向签名直链 → 下载 zip → 7z/zipfile 自动解压
- 图片逐张下载：分批解析图片页 → 并发下载
- 下载文件夹命名（含右上角分类标签）
"""
import logging
import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path

from lxml import html

from wnacg_crawler import (
    DOWNLOAD_MAX_RETRIES,
    DOWNLOAD_WORKERS,
    G_URL,
    HEADERS,
    _fetch_with_retry,
    _throttle,
    download_pictures,
    fetch,
    load_config,
    parse_picture_page,
)

logger = logging.getLogger(__name__)

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
    page_html = _fetch_with_retry(page_url)
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
    url = _generate_link(worker_api, file_key, file_name, page_url)

    logger.info("整包下载直链已生成: aid=%s", aid)
    return {"url": url, "file_name": file_name, "aid": aid}


def _generate_link(worker_api: str, file_key: str, file_name: str, page_url: str) -> str:
    """用 curl_cffi（模拟 Chrome TLS/HTTP2 指纹）调用 generate-link API。

    携带下载页响应中的 Cookie，并通过 impersonate 模拟 Chrome 指纹，
    绕过 Cloudflare 的 TLS/HTTP2 指纹校验（requests 会被 403 拦截）。
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        raise RuntimeError(
            "未安装 curl_cffi，请执行: pip install curl_cffi"
        )

    _throttle()
    download_page_cookies = _fetch_cookies(page_url)
    headers = {
        **HEADERS,
        "Accept": "*/*",
        "Origin": G_URL,
        "Referer": f"{G_URL}/",
        "Cookie": download_page_cookies,
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


def _fetch_cookies(url: str) -> str:
    """请求页面并返回响应中的 Cookie 字符串（保持与浏览器一致的会话状态）。"""
    _throttle()
    import requests

    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return "; ".join(f"{c.name}={c.value}" for c in resp.cookies)


def _safe_filename(name: str) -> str:
    """将名称转换为可用的目录名。

    处理 Windows 非法字符与易混淆字符：
    - 半角/全角斜杠与竖线（\\ / ／ ＼ | ｜）：替换为下划线，避免被当作路径分隔符
    - 半角/全角 &（& ＆）：替换为下划线
    - : * ? " < > 等非法字符：替换为下划线
    """
    name = re.sub(r'[\\/:*?"<>|｜／＼]', "_", name)
    name = name.replace("&", "_").replace("＆", "_")
    name = re.sub(r"_+", "_", name).strip(" _")
    return name or "untitled"


def _download_folder_name(comic: dict, use_label: bool = True) -> str:
    """生成下载文件夹名：`[右上角分类标签] 标题`；use_label=False 或无线标签时仅用标题。"""
    label = (comic.get("category_label") or "").strip()
    title = (comic.get("title") or "").strip()
    name = f"[{label}] {title}" if use_label and label else title
    return _safe_filename(name)


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


def _extract_archive(
    archive_path: Path,
    save_dir: Path,
    progress_callback=None,
    total_files: int = 0,
    base: int = 0,
    span: int = 100,
) -> None:
    """解压压缩包到 save_dir：优先使用 7z（快、中文文件名稳），回退内置 zipfile。

    Args:
        progress_callback: 可选回调 (当前进度, 100)。进度统一为 0-100 的百分比，
            可用 base/span 将本阶段映射到整体进度区间（如下载占 0-80，解压占 80-100）。
        total_files: zip 内的文件总数（zipfile 模式用于逐文件进度计算）。
        base/span: 本阶段在整体进度中的起始值与跨度（默认 0-100）。
    """
    seven_zip = _find_7z()
    if seven_zip:
        try:
            _extract_with_7z(
                seven_zip, archive_path, save_dir, progress_callback, total_files, base, span
            )
            return
        except Exception as exc:
            logger.warning("7z 解压失败，回退内置 zipfile: %s", exc)

    logger.info("使用内置 zipfile 解压: %s", archive_path)
    with zipfile.ZipFile(archive_path) as zf:
        names = zf.namelist()
        total_files = total_files or len(names)
        for i, name in enumerate(names, 1):
            zf.extract(name, save_dir)
            if progress_callback and total_files:
                pct = min(int(i * span / total_files), span)
                progress_callback(base + pct, 100)


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
    """
    proc = subprocess.Popen(
        [seven_zip, "x", "-y", f"-o{save_dir}", str(archive_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

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
            stop_monitor.wait(0.05)

    monitor = threading.Thread(target=_monitor, daemon=True)
    monitor.start()
    try:
        proc.wait(timeout=600)
    finally:
        stop_monitor.set()
        monitor.join(timeout=2)
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        stderr = proc.stderr.read().decode("utf-8", "ignore")
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

    tmp = save_dir.parent / f"{_safe_filename(zip_name)}.part"
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
    config = load_config()
    if base_dir is None:
        base_dir = Path(config.get("Download", "./Download"))
    use_label = bool(config.get("Folder_use_category_label", True))
    save_dir = base_dir / _download_folder_name(comic, use_label)

    # 压缩包下载模式：下载整包 zip 并自动解压
    if config.get("Download_mode", "image") == "zip":
        if stage_callback:
            stage_callback("准备")
        _download_package(comic, save_dir, progress_callback, stage_callback)
        return

    detail_html = fetch(G_URL + comic["detail_url"])
    page_nodes = html.fromstring(detail_html).xpath(
        '//*[@id="bodywrap"]/div[2]/div/div/div[1]/a[last()]/@href'
    )
    if not page_nodes:
        logger.warning("未找到阅读页链接: %s", comic["title"])
        return

    # 从阅读页 URL 提取总页数
    match = re.search(r"page-(\d+)-aid-(\d+)", page_nodes[0])
    page_count = int(match.group(1)) if match else 1
    workers = int(config.get("Download_workers", DOWNLOAD_WORKERS))
    retries = int(config.get("Download_retries", DOWNLOAD_MAX_RETRIES))

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
            page_nodes[0], start=batch_start, end=batch_end
        )
        if not batch:
            logger.warning("第 %d-%d 页未解析到图片，跳过", batch_start, batch_end)
            continue

        grand_total += len(batch)
        if progress_callback:
            progress_callback(grand_done, grand_total)

        if stage_callback:
            stage_callback(f"下载图片 {batch_start}-{batch_end} 页")

        def _batch_cb(cur: int, _total: int):
            progress_callback(grand_done + cur, grand_total)

        download_pictures(
            batch,
            save_dir,
            _batch_cb if progress_callback else None,
            max_workers=workers,
            max_retries=retries,
        )
        grand_done += len(batch)
        logger.info(
            "第 %d-%d 页下载完成，累计 %d/%d 张图片",
            batch_start, batch_end, grand_done, grand_total,
        )

    if grand_done == 0:
        raise RuntimeError(
            f"未解析到任何图片链接（作品: {comic.get('title', '')}），"
            "页面结构可能变化或已被反爬拦截，详见日志"
        )
