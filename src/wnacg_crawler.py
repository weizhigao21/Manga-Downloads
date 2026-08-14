"""漫画列表/搜索爬虫 - https://www.wnacg.com/

用法:
    python wnacg_crawler.py            # 抓取列表并打印 JSON
    python wnacg_crawler.py -o list.json   # 抓取列表并保存到文件
"""
import argparse
import json
import logging
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from lxml import html

# 独立运行（python src/wnacg_crawler.py）时 sys.path[0] 是 src/ 目录，
# 需将项目根加入路径后统一按 src.* 包导入；作为包导入时 __package__ 非空，跳过。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ProjectConfig
from src.net import _RETRYABLE_STATUS, _should_retry, _throttle, fetch_with_retry
from src.net import fetch as _net_fetch
from src.utils import data_root, safe_filename

# 项目根目录（src 的上级目录）与配置文件路径
ROOT_DIR = data_root()

# 目标页面与列表容器 XPath（<ul> 下每个 <li class="gallary_item"> 为一条漫画）
URL = "https://www.wnacg.com/albums.html"
G_URL = "https://www.wnacg.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.wnacg.com/",
}

# 封面右上角分类角标映射（来自 /themes/weitu/images/style.css）
CATE_LABELS = {
    "cate-1": "同人誌／漢化",
    "cate-2": "同人誌／CG畫集",
    "cate-3": "寫真 & Cosplay",
    "cate-5": "同人誌",
    "cate-6": "單行本",
    "cate-7": "雜誌&短篇",
    "cate-9": "單行本／漢化",
    "cate-10": "雜誌&短篇／漢化",
    "cate-12": "同人誌／日語",
    "cate-13": "單行本／日語",
    "cate-14": "雜誌&短篇／日語",
    "cate-16": "同人誌／English",
    "cate-17": "單行本／English",
    "cate-18": "雜誌&短篇／English",
    "cate-19": "韓漫",
    "cate-20": "韓漫／漢化",
    "cate-21": "韓漫／生肉",
    "cate-22": "3D&漫畫",
    "cate-23": "3D&漫畫／漢化",
    "cate-24": "3D&漫畫／其他",
    "cate-37": "AI&圖集",
}

# 仅独立运行（CLI）或未配置过日志时初始化 root logger；
# 作为库被 UI 层导入时不改动调用方的日志配置
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def fetch(url: str) -> str:
    """请求页面并返回 HTML 文本（自带全局节流与站点请求头）。"""
    return _net_fetch(url, HEADERS)


def _extract_int(text: str | None, pattern: str) -> int | None:
    if not text:
        return None
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


def _extract_date(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else None


def parse_comics(html_text: str) -> list[dict]:
    """解析列表节点，返回漫画信息列表。

    兼容两种页面容器结构（条目内部结构一致）：
        - 分类/全部列表页: <ul> 下 li.gallary_item
        - 搜索结果页: <ul class="cc"> 下 li.li.gallary_item

    每个字典包含字段:
        - title: 漫画标题
        - date: 上传日期 (YYYY-MM-DD)
        - detail_url: 详情页相对路径
        - image_count: 图片数量
        - cover_url: 封面缩略图 URL（可能为空）
        - category: cate-N 类名
        - category_label: 分类角标文本
    """
    doc = html.fromstring(html_text)
    items = doc.xpath('//ul/li[contains(@class, "gallary_item")]')
    if not items:
        raise RuntimeError("未找到漫画列表节点")

    comics = []
    for li in items:
        link_nodes = li.xpath('./div[1]/a/@href')
        title_nodes = li.xpath('./div[1]/a/@title')
        cover_nodes = li.xpath('./div[1]/a/img/@src')
        meta_nodes = li.xpath('./div[2]/div[2]/text()')

        if not link_nodes or not title_nodes:
            continue

        meta_text = meta_nodes[0] if meta_nodes else ""
        count = _extract_int(meta_text, r"(\d+)張") or 0
        date = _extract_date(meta_text) or ""

        cover_url = cover_nodes[0] if cover_nodes else ""
        # 缩略图可能是相对路径或 // 协议相对 URL
        if cover_url and cover_url.startswith("//"):
            cover_url = f"https:{cover_url}"
        elif cover_url and not cover_url.startswith("http"):
            cover_url = G_URL + cover_url

        # 提取封面右上角分类角标
        pic_box_class = " ".join(li.xpath('./div[contains(@class, "pic_box")]/@class'))
        category_match = re.search(r"cate-(\d+)", pic_box_class)
        category = f"cate-{category_match.group(1)}" if category_match else ""
        category_label = CATE_LABELS.get(category, "")

        comics.append(
            {
                "title": title_nodes[0].strip(),
                "date": date,
                "detail_url": link_nodes[0],
                "image_count": count,
                "cover_url": cover_url,
                "category": category,
                "category_label": category_label,
            }
        )
    return comics


def _page_url(base_url: str, page: int) -> str:
    """将基础列表页 URL 转换为指定页码的 URL。

    支持:
        https://www.wnacg.com/albums.html            -> 第 2 页
        https://www.wnacg.com/albums-index-page-2.html
        分类页（cate-N）翻页格式不同，为 page 在前 cate 在后:
        https://www.wnacg.com/albums-index-cate-5.html
            -> 第 2 页 https://www.wnacg.com/albums-index-page-2-cate-5.html
    """
    if page <= 1:
        return base_url
    return re.sub(
        r"(albums)(?:-index-page-\d+)?(?:-index)?(?:-cate-(\d+))?\.html$",
        lambda m: (
            rf"{m.group(1)}-index-page-{page}-cate-{m.group(2)}.html"
            if m.group(2)
            else rf"{m.group(1)}-index-page-{page}.html"
        ),
        base_url,
    )


def fetch_page(page: int, list_url: str | None = None) -> list[dict]:
    """抓取指定页码的漫画列表。

    Args:
        page: 页码，从 1 开始。
        list_url: 列表页 URL，默认读取 Config/Config.json 中的 list_url。

    Returns:
        漫画信息字典列表
    """
    if list_url:
        url = list_url
    else:
        url = ProjectConfig().list_url

    page_url = _page_url(url, max(1, page))
    logger.info("正在抓取列表: %s", page_url)
    html_text = fetch(page_url)
    return parse_comics(html_text)


def fetch_comics(
    list_url: str | None = None, pages: int | None = None
) -> list[dict]:
    """抓取漫画列表并返回结构化数据（支持翻页，按 detail_url 去重）。

    Args:
        list_url: 列表页 URL，默认读取 Config/Config.json 中的 list_url。
        pages: 抓取页数，默认读取 Config/Config.json 中的 Page_number。

    Returns:
        漫画信息字典列表
    """
    if pages is None:
        pages = ProjectConfig().page_number
    pages = max(1, pages)

    comics: list[dict] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        page_comics = fetch_page(page, list_url)
        if not page_comics:
            logger.info("第 %d 页无内容，停止翻页", page)
            break
        for comic in page_comics:
            if comic["detail_url"] in seen:
                continue
            seen.add(comic["detail_url"])
            comics.append(comic)
        logger.info("第 %d 页抓取完成，累计 %d 部漫画", page, len(comics))

    logger.info("抓取完成，共 %d 部漫画（%d 页）", len(comics), pages)
    return comics


def search_comics(keyword: str, page: int = 1) -> list[dict]:
    """按关键词搜索漫画，返回与列表页同结构的漫画信息。

    搜索结果页的列表结构与列表页一致（li.gallary_item），可直接复用 parse_comics：
    - 第 1 页: /search/?q=...&f=_all&s=create_time_DESC&syn=yes
    - 第 N 页: /search/index.php?q=...&m=&syn=yes&f=_all&s=create_time_DESC&p=N
    """
    from urllib.parse import quote

    q = quote(keyword)
    if page <= 1:
        url = f"{G_URL}/search/?q={q}&f=_all&s=create_time_DESC&syn=yes"
    else:
        url = (
            f"{G_URL}/search/index.php?q={q}&m=&syn=yes"
            f"&f=_all&s=create_time_DESC&p={page}"
        )
    logger.info("正在搜索: %s（第 %d 页）", keyword, page)
    html_text = fetch_with_retry(url, HEADERS)
    return parse_comics(html_text)


# 并发下载控制（避免触发反爬，保持温和速率）
DOWNLOAD_WORKERS = 3
DOWNLOAD_MAX_RETRIES = 3
# 图片详情页解析并发数（同样保持温和，避免触发限流）
PARSE_WORKERS = 2


def _normalize_image_url(url: str) -> str:
    """将提取到的图片 URL 归一化为可请求的完整地址。"""
    url = url.strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = f"https:{url}"
    elif not url.startswith("http"):
        url = G_URL + url
    return url


def _extract_image_url(detail_doc) -> str | None:
    """从图片详情页提取大图直链。

    页面结构随站点改版可能变化，因此提供多个候选 XPath 按优先级尝试：
    优先匹配含常见图片容器的相对路径，最后才回退到历史版本的绝对路径。
    同时过滤图标/占位图等非内容图片。
    """
    candidates = (
        # 常见大图容器
        '//div[contains(@class, "pic_box") or contains(@class, "photo")]//a/img/@src',
        '//span/a/img/@src',
        # 任意 a 内的大图（排除小图标）
        '//a/img/@src',
        # 历史版本结构兜底
        '/html/body/div[5]/span/a/img/@src',
    )
    ignored_keywords = ("icon", "logo", "placeholder", "loading", "spinner")
    for xpath in candidates:
        for src in detail_doc.xpath(xpath):
            url = _normalize_image_url(src)
            if not url:
                continue
            lower = url.lower()
            if any(k in lower for k in ignored_keywords):
                continue
            return url
    return None


def _fetch_picture_link(link: str, retries: int = DOWNLOAD_MAX_RETRIES) -> str | None:
    """获取单张图片详情页中的图片直链，失败时返回 None。

    对限流（429/403/5xx）、连接错误与超时进行指数退避重试（0.5s → 1s → 2s），
    避免密集请求或弱网抖动导致整部作品的图片链接获取失败；业务类 4xx
    （如 404）不重试，直接跳过。
    """
    for attempt in range(retries):
        try:
            detail_html = fetch(G_URL + link)
            detail_doc = html.fromstring(detail_html)
            img_url = _extract_image_url(detail_doc)
            if not img_url:
                logger.warning("图片详情页未找到图片直链: 条目=%s", link)
                return None
            return img_url
        except requests.RequestException as exc:
            if not _should_retry(exc):
                logger.warning("获取图片详情页失败: 条目=%s, 错误: %s", link, exc)
                return None
            wait = 0.5 * (2**attempt) + random.uniform(0, 0.5)
            logger.warning(
                "获取图片详情页失败(%s)，第 %d/%d 次重试，等待 %.1fs: %s",
                type(exc).__name__, attempt + 1, retries, wait, link,
            )
            time.sleep(wait)
    logger.warning("获取图片详情页重试 %d 次仍失败，跳过: %s", retries, link)
    return None


def parse_picture_page(reading_url: str, start: int = 1, end: int | None = None) -> list[dict]:
    """解析某个漫画的阅读页，返回所有图片链接。

    Args:
        reading_url: 阅读页 URL（含 page-N-aid-X 信息）。
        start/end: 页区间（1-based），用于分批解析（如先解析第 1-4 页）。
                   默认解析全部页。

    返回列表元素格式:
        - link: 图片直链
        - number: 页码/名称
    """
    match = re.search(r"page-(\d+)-aid-(\d+)", reading_url)
    if match:
        page_count, aid = int(match.group(1)), match.group(2)
    else:
        aid_match = re.search(r"aid-(\d+)", reading_url)
        aid = aid_match.group(1) if aid_match else ""
        page_count = 1

    end = min(end or page_count, page_count)
    logger.info("解析图片页: aid=%s, 页区间=%d-%d（共 %d 页）", aid, start, end, page_count)
    pictures = []
    for num in range(start - 1, end):
        page_url = f"{G_URL}/photos-index-page-{num + 1}-aid-{aid}.html"
        try:
            page_html = fetch_with_retry(page_url, HEADERS)
        except Exception as exc:
            # 记录详细错误后重新抛出，交由上层（下载任务）处理
            logger.warning(
                "获取图片列表页失败: aid=%s, 第 %d/%d 页, url=%s, 错误: %s",
                aid, num + 1, page_count, page_url, exc,
            )
            raise
        try:
            doc = html.fromstring(page_html)
            picture_nodes = doc.xpath('//*[@id="bodywrap"]//ul/li[contains(@class, "gallary_item")]')
        except Exception as exc:
            logger.warning(
                "解析图片列表页 HTML 失败: aid=%s, 第 %d/%d 页, 错误: %s",
                aid, num + 1, page_count, exc,
            )
            raise
        if not picture_nodes:
            logger.warning(
                "图片列表页未找到任何图片条目: aid=%s, 第 %d/%d 页, HTML 长度=%d（页面结构可能变化或被反爬拦截）",
                aid, num + 1, page_count, len(page_html or ""),
            )

        page_pictures = 0
        entries = []
        for pic in picture_nodes:
            link_nodes = pic.xpath('./div[contains(@class, "pic_box")]/a/@href')
            number_nodes = pic.xpath('./div[contains(@class, "info")]//span[contains(@class, "name")]/text()')
            if not link_nodes or not number_nodes:
                logger.debug("跳过缺少链接或名称的图片条目: aid=%s, 第 %d/%d 页", aid, num + 1, page_count)
                continue
            entries.append((link_nodes[0], number_nodes[0]))

        # 并发获取每张图片的直链（受控并发 + 退避重试，避免触发限流）
        with ThreadPoolExecutor(max_workers=PARSE_WORKERS) as executor:
            img_urls = list(executor.map(lambda e: _fetch_picture_link(e[0]), entries))

        for (link, number), img_url in zip(entries, img_urls, strict=True):
            if img_url:
                pictures.append({"link": img_url, "number": number})
                page_pictures += 1
            else:
                logger.warning(
                    "跳过获取直链失败的图片: aid=%s, 第 %d/%d 页, 条目=%s",
                    aid, num + 1, page_count, link,
                )

        logger.info(
            "图片列表页解析完成: aid=%s, 第 %d/%d 页, 解析到 %d 张图片",
            aid, num + 1, page_count, page_pictures,
        )

    if not pictures:
        logger.warning(
            "未解析到任何图片链接: aid=%s, page_count=%s（页面结构可能变化或已被反爬拦截）",
            aid, page_count,
        )
    else:
        logger.info("图片页解析完成: aid=%s, 共 %d 张图片", aid, len(pictures))
    return pictures


def download_pictures(
    pictures: list[dict],
    save_dir: Path,
    progress_callback=None,
    max_workers: int = DOWNLOAD_WORKERS,
    max_retries: int = DOWNLOAD_MAX_RETRIES,
) -> int:
    """并发下载图片列表到指定目录。

    通过受控并发数 + 全局节流 + 指数退避重试，在提升速度的同时避免触发站点反爬限制。
    单张图片失败（重试耗尽）只记录不中断整批，其余图片继续下载。

    Args:
        progress_callback: 可选回调 (当前张数, 总张数)，每下载一张调用一次。
        max_workers: 并发线程数，默认 3（并发过高容易被反爬拦截）。
        max_retries: 失败重试次数，默认 3。

    Returns:
        下载失败（重试耗尽）的图片数量；调用方可用于修正累计进度。
    """
    save_dir.mkdir(parents=True, exist_ok=True)
    total = len(pictures)
    if total == 0:
        if progress_callback:
            progress_callback(0, 0)
        return 0

    done = 0
    failed: list[Exception] = []
    lock = threading.Lock()

    def _download_one(pic: dict):
        nonlocal done
        url = pic["link"]
        # 页码/名称来自网页 HTML（不可信来源），清洗后拼接，防止路径穿越
        target = save_dir / f"{safe_filename(str(pic['number']))}.jpg"
        # 断点续传：目标文件已存在且非空时跳过，不重复下载
        if target.exists() and target.stat().st_size > 0:
            with lock:
                done += 1
                if progress_callback:
                    progress_callback(done, total)
            return
        last_exc: Exception = Exception("未知下载错误")
        for attempt in range(max_retries):
            try:
                # 全局节流：与页面抓取共用请求密度控制，避免密集下载触发反爬
                _throttle()
                with requests.get(url, headers=HEADERS, timeout=30, stream=True) as resp:
                    # 429（限流）/ 403（反爬）/ 5xx（服务端错误）时退避重试
                    if resp.status_code in _RETRYABLE_STATUS or resp.status_code >= 500:
                        raise requests.HTTPError(
                            f"HTTP {resp.status_code}", response=resp
                        )
                    resp.raise_for_status()
                    # 先写临时文件再原子替换：中断/强制终止不会留下半截图片
                    tmp = target.with_name(target.name + ".part")
                    try:
                        with open(tmp, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=64 * 1024):
                                if chunk:
                                    f.write(chunk)
                        tmp.replace(target)
                    finally:
                        # 失败/中断时清理残留 .part，避免积累垃圾文件
                        if tmp.exists():
                            try:
                                tmp.unlink()
                            except OSError:
                                pass
                break
            except (requests.RequestException, OSError) as exc:
                last_exc = exc
                # 指数退避：0.5s, 1s, 2s
                time.sleep(0.5 * (2**attempt))
        else:
            # 重试耗尽：记录失败原因，不中断整批（其余图片继续下载）
            with lock:
                failed.append(last_exc)
            return

        with lock:
            done += 1
            if progress_callback:
                progress_callback(done, total)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(_download_one, pictures))

    if failed:
        logger.warning(
            "图片下载完成：成功 %d/%d 张，失败 %d 张（示例原因: %s）",
            done, total, len(failed), failed[0],
        )
    return len(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 wnacg 漫画列表")
    parser.add_argument("-o", "--output", help="保存结果的 JSON 文件路径")
    parser.add_argument("--download", action="store_true", help="同时下载前 3 部符合条件的漫画")
    args = parser.parse_args()

    try:
        comics = fetch_comics()
    except Exception as exc:  # 网络或解析失败
        logger.error("抓取失败: %s", exc)
        return 1

    output_text = json.dumps(comics, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output_text, encoding="utf-8")
        logger.info("结果已保存到: %s", args.output)
    else:
        print(output_text)

    if args.download:
        # 延迟导入避免循环依赖（downloader 依赖本模块）
        from src.downloader import download_comic

        base_dir = Path(ProjectConfig().download_dir)
        for comic in comics[:3]:
            if comic["image_count"] < 20:
                logger.info("跳过图片数量不足的漫画: %s", comic["title"])
                continue
            logger.info("开始下载: %s", comic["title"])
            download_comic(comic, base_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
