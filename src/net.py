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

# 会话级连接池：页面请求复用同一 Session，TCP/TLS 握手只做一次，
# 避免每次请求新建连接（一部 18 页作品约 234 次请求，收益显著）
_SESSION = requests.Session()


def _throttle():
    """相邻请求之间保持最小间隔（跨线程生效）。"""
    global _LAST_REQUEST_TIME
    with _REQUEST_LOCK:
        now = time.monotonic()
        wait = REQUEST_INTERVAL - (now - _LAST_REQUEST_TIME)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_TIME = time.monotonic()


def _decode_encoding(resp) -> None:
    """设置响应编码：优先响应头 charset，无有效声明时才做全文检测。

    requests 对无 charset 的 text 响应默认视为 ISO-8859-1（对中文页面
    会乱码），且 apparent_encoding 使用 chardet 全文检测开销较高；
    仅在确实没有有效编码声明时才回退检测。
    """
    enc = resp.encoding
    if not enc or enc.lower() in ("iso-8859-1", "latin-1"):
        resp.encoding = resp.apparent_encoding or "utf-8"


def fetch(url: str, headers: dict, timeout: int = 30) -> str:
    """请求页面并返回 HTML 文本（自带全局节流，复用连接池）。"""
    _throttle()
    resp = _SESSION.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    _decode_encoding(resp)
    return resp.text


def _should_retry(exc: BaseException) -> bool:
    """判断异常是否值得退避重试。

    可重试：
    - 超时（Timeout / ReadTimeout / ConnectTimeout）：网络抖动常见；
    - 连接错误（ConnectionError 及其子类）：弱网/连接池满时常见；
    - HTTP 限流状态码（429/403/5xx，见 _RETRYABLE_STATUS）。

    不可重试：业务类 4xx（404/400 等）、证书错误、其他非 requests 异常。
    """
    if isinstance(exc, requests.Timeout):
        return True
    if isinstance(exc, requests.ConnectionError):
        return True
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        return status in _RETRYABLE_STATUS
    return False


def _request_with_retry(
    request_fn, url: str, headers: dict, retries: int, timeout: int
):
    """通用带退避重试的请求：可重试异常指数退避，其余直接抛出。

    限流/连接错误/超时的等待时间指数增长（3s → 6s → 12s...），
    给站点与网络留出恢复窗口。
    """
    for attempt in range(retries):
        try:
            return request_fn(url, headers, timeout)
        except requests.RequestException as exc:
            if not _should_retry(exc):
                raise
            wait = 3.0 * (2**attempt) + random.uniform(0, 1)
            logger.warning(
                "请求失败(%s)，第 %d/%d 次重试，等待 %.1fs: %s",
                type(exc).__name__, attempt + 1, retries, wait, url,
            )
            time.sleep(wait)
    raise RuntimeError(f"页面请求重试 {retries} 次仍失败: {url}")


def fetch_with_retry(url: str, headers: dict, retries: int = 3, timeout: int = 30) -> str:
    """带退避重试的页面请求：限流状态码 / 连接错误 / 超时均会重试。

    限流等待时间指数增长（3s → 6s → 12s...），给站点留出恢复窗口。
    """
    return _request_with_retry(fetch, url, headers, retries, timeout)


def fetch_page_with_cookies(
    url: str, headers: dict, retries: int = 3, timeout: int = 30
) -> tuple[str, str]:
    """请求页面并返回 (HTML, Cookie 字符串)，带退避重试。

    页面 HTML 与 Cookie 在一次请求中同时取得，避免对同一页面重复
    请求触发限流；对限流/连接错误/超时指数退避重试（与 fetch_with_retry 一致）。
    """
    def _fetch_page(url, headers, timeout):
        _throttle()
        resp = _SESSION.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        _decode_encoding(resp)
        cookies = "; ".join(f"{c.name}={c.value}" for c in resp.cookies)
        return resp.text, cookies

    return _request_with_retry(_fetch_page, url, headers, retries, timeout)
