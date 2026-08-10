"""后台相似作品扫描线程：页面作品自动查询本地数据库（名称/作者/封面三信号）。

抓取/搜索完成一页后启动：为缺失封面的作品下载封面（受全局节流控制，
同时填充本地封面缓存）→ 计算封面感知哈希并写入 cover_hashes 缓存表
（"封面先算好存库"）→ 批量查询相似记录 → 回调主窗口更新卡片角标。

全程只读本地数据库，不向站点发起任何列表/搜索请求；唯一网络开销是
页面作品的封面图片下载（与卡片显示时的封面加载一致，可被缓存复用）。
"""
import logging

from PySide6.QtCore import QThread, Signal

from src.net import _throttle
from ui.database import AID_RE, COVERS_DIR, _cover_hash, find_similar_map
from ui.threads import register

logger = logging.getLogger(__name__)

# 封面下载请求头（与站点请求保持温和，携带 Referer 避免被拒）
_COVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.wnacg.com/",
}


class SimilarWorker(QThread):
    """后台相似作品扫描线程。

    items 为 [(detail_url, title, translated_title, cover_url), ...]，
    即页面抓取所得作品（一页约 20-40 部）。
    """

    finished_ok = Signal(dict)  # {detail_url: [相似记录, ...]}
    error = Signal(str)

    def __init__(self, items: list[tuple[str, str, str, str]], parent=None):
        super().__init__(parent)
        self._items = items

    def run(self):
        register(self)
        try:
            similar_map = self._scan()
            self.finished_ok.emit(similar_map)
        except Exception as exc:  # noqa: BLE001 - 扫描失败不应影响主流程
            logger.warning("相似作品扫描失败: %s", exc)
            self.error.emit(str(exc))

    def _scan(self) -> dict:
        """下载缺失封面（本地已有则跳过）并批量查询相似记录。"""
        import requests

        page_cover_hashes: dict[str, str] = {}
        for detail_url, _title, _translated, cover_url in self._items:
            match = AID_RE.search(detail_url or "")
            if not match:
                continue
            path = COVERS_DIR / f"{match.group(1)}.jpg"
            if not path.exists() and cover_url:
                try:
                    _throttle()
                    with requests.get(
                        cover_url,
                        headers=_COVER_HEADERS,
                        timeout=15,
                        stream=True,
                    ) as resp:
                        if resp.status_code == 200:
                            COVERS_DIR.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(resp.content)
                except Exception as exc:  # noqa: BLE001 - 单张封面失败不影响其余
                    logger.debug("封面下载失败 aid=%s: %s", match.group(1), exc)
                    continue
            # 封面文件就绪后计算哈希（命中缓存表则直接返回并复用）
            hash_value = _cover_hash(match.group(1))
            if hash_value:
                page_cover_hashes[detail_url] = hash_value

        return find_similar_map(
            [(url, title, translated) for url, title, translated, _ in self._items],
            page_cover_hashes,
        )
