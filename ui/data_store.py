"""本地漫画数据缓存管理。"""

import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT_DIR / "Config" / "comics_cache.json"


def load_cache() -> list[dict] | None:
    """加载本地缓存的漫画列表，若不存在或损坏返回 None。"""
    if not CACHE_PATH.exists():
        return None
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def save_cache(comics: list[dict]):
    """保存漫画列表到本地缓存。"""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, encoding="utf-8", mode="w") as f:
        json.dump(comics, f, ensure_ascii=False, indent=2)
