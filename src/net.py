"""通用网络层：全局请求节流、页面抓取与带退避的重试。

供爬虫（wnacg_crawler）与下载器（downloader）共用，保证对主站的
整体请求密度受控，避免触发反爬限流；限流响应（429/403/5xx）时
指数退避重试。
"""
import logging
import random
import threading
import time

import requests

logger = logging.getLogger(__name__)

# ---- 全局请求节流：控制对主站的请求密度，避免触发反爬限流 ----
# 相邻请求的最小间隔（秒）。解析一部 18 页作品约需 234 次请求，
# 0.6s 间隔 ≈ 1.7 次/秒，显著低于站点限流阈值，且仍在可接受速度。
REQUEST_INTERVAL = 0.6
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_TIME = 0.0

# 视为限流/反爬、需要退避重试的状态码
_RETRYABLE_STATUS = (429, 403, 500, 502, 503)


def _throttle():
    """相邻请求之间保持最小间隔（跨线程生效）。"""
    global _LAST_REQUEST_TIME
    with _REQUEST_LOCK:
        now = time.monotonic()
        wait = REQUEST_INTERVAL - (now - _LAST_REQUEST_TIME)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_TIME = time.monotonic()


def fetch(url: str, headers: dict, timeout: int = 30) -> str:
    """请求页面并返回 HTML 文本（自带全局节流）。"""
    _throttle()
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def fetch_with_retry(url: str, headers: dict, retries: int = 3, timeout: int = 30) -> str:
    """带退避重试的页面请求，遇限流状态码时延长等待后重试。

    限流等待时间指数增长（3s → 6s → 12s...），给站点留出恢复窗口。
    """
    for attempt in range(retries):
        try:
            return fetch(url, headers, timeout)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in _RETRYABLE_STATUS:
                wait = 3.0 * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "页面请求被限流(HTTP %d)，第 %d/%d 次重试，等待 %.1fs: %s",
                    status, attempt + 1, retries, wait, url,
                )
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"页面请求重试 {retries} 次仍失败: {url}")


def fetch_page_with_cookies(
    url: str, headers: dict, retries: int = 3, timeout: int = 30
) -> tuple[str, str]:
    """请求页面并返回 (HTML, Cookie 字符串)，带退避重试。

    页面 HTML 与 Cookie 在一次请求中同时取得，避免对同一页面重复
    请求触发限流；对限流状态码指数退避重试（与 fetch_with_retry 一致）。
    """
    for attempt in range(retries):
        try:
            _throttle()
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            cookies = "; ".join(f"{c.name}={c.value}" for c in resp.cookies)
            return resp.text, cookies
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in _RETRYABLE_STATUS:
                wait = 3.0 * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "页面请求被限流(HTTP %d)，第 %d/%d 次重试，等待 %.1fs: %s",
                    status, attempt + 1, retries, wait, url,
                )
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"页面请求重试 {retries} 次仍失败: {url}")
