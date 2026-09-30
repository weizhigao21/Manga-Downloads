"""在线漫画阅读器模块。

分层：
- cache_index：本地缓存定位与扫描（与下载目录合流，纯 Python，无 Qt）
- page_provider：页面获取编排（本地优先 → 直链索引 → 联网下载）
- prefetch_worker：后台预取线程
- reader_window：阅读器窗口

依赖方向：reader/ → src/（允许），reader/ → ui/database（仅取存储层），
reader/ 内部各模块之间单向依赖。阅读器不修改核心下载协议。
"""

from ui.reader.cache_index import (
    ReaderCacheIndex,
    count_local_images,
    find_local_dir,
    index_local_images,
    natural_sort_key,
    page_number_from_name,
    scan_local_images,
)

__all__ = [
    "ReaderCacheIndex",
    "count_local_images",
    "find_local_dir",
    "index_local_images",
    "natural_sort_key",
    "page_number_from_name",
    "scan_local_images",
]
