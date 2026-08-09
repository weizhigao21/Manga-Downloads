"""项目级配置管理（Config/Config.json）。

纯标准库实现，供 src 层（爬虫/下载器）与 ui 层共用：
- src 层通过 ProjectConfig 读取 Config.json，不再直接 open 文件；
- ui 层从本模块导入 ProjectConfig，保持原有 API（save/属性读写）。
"""
import json

from src.utils import data_root

ROOT_DIR = data_root()
PROJECT_CONFIG_PATH = ROOT_DIR / "Config" / "Config.json"

DEFAULT_PROJECT_CONFIG = {
    "Page_number": 20,
    "Download": "./Download",
    "list_url": "https://www.wnacg.com/albums.html",
    "Download_workers": 3,
    "Download_retries": 3,
    "Folder_use_category_label": True,
    "Download_mode": "image",
}


class ProjectConfig:
    """管理项目级配置文件 Config/Config.json（单例）。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self):
        self._config = DEFAULT_PROJECT_CONFIG.copy()
        if PROJECT_CONFIG_PATH.exists():
            try:
                with open(PROJECT_CONFIG_PATH, encoding="utf-8") as f:
                    loaded = json.load(f)
                    self._config.update(loaded)
            except (json.JSONDecodeError, OSError) as exc:
                print(f"加载项目配置失败: {exc}", flush=True)

    def save(self):
        """保存当前项目配置到文件。"""
        PROJECT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PROJECT_CONFIG_PATH, encoding="utf-8", mode="w") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)

    @property
    def page_number(self) -> int:
        return int(self._config.get("Page_number", 20))

    @page_number.setter
    def page_number(self, value: int):
        self._config["Page_number"] = int(value)

    @property
    def download_dir(self) -> str:
        return str(self._config.get("Download", "./Download"))

    @download_dir.setter
    def download_dir(self, value: str):
        self._config["Download"] = str(value)

    @property
    def list_url(self) -> str:
        return str(self._config.get("list_url", "https://www.wnacg.com/albums.html"))

    @list_url.setter
    def list_url(self, value: str):
        self._config["list_url"] = str(value)

    @property
    def convert_traditional_to_simplified(self) -> bool | None:
        """项目配置中的繁简体转换开关（Config.json）。"""
        value = self._config.get("convert_traditional_to_simplified")
        return value if value is None else bool(value)

    @convert_traditional_to_simplified.setter
    def convert_traditional_to_simplified(self, value: bool):
        self._config["convert_traditional_to_simplified"] = bool(value)

    @property
    def download_workers(self) -> int:
        """下载并发数，夹取到 [1, 8]：0/负数会让线程池抛错，过高易触发反爬。"""
        value = int(self._config.get("Download_workers", 3))
        return max(1, min(value, 8))

    @download_workers.setter
    def download_workers(self, value: int):
        value = int(value)
        self._config["Download_workers"] = max(1, min(value, 8))

    @property
    def download_retries(self) -> int:
        """失败重试次数，夹取到 [0, 10]（0 表示不重试）。"""
        value = int(self._config.get("Download_retries", 3))
        return max(0, min(value, 10))

    @download_retries.setter
    def download_retries(self, value: int):
        value = int(value)
        self._config["Download_retries"] = max(0, min(value, 10))

    @property
    def folder_use_category_label(self) -> bool:
        """下载文件夹名是否包含右上角分类标签。"""
        return bool(self._config.get("Folder_use_category_label", True))

    @folder_use_category_label.setter
    def folder_use_category_label(self, value: bool):
        self._config["Folder_use_category_label"] = bool(value)

    @property
    def download_mode(self) -> str:
        """下载方式：image=逐图下载，zip=压缩包整包下载（下载后自动解压）。"""
        mode = str(self._config.get("Download_mode", "image")).lower()
        return mode if mode in ("image", "zip") else "image"

    @download_mode.setter
    def download_mode(self, value: str):
        self._config["Download_mode"] = value if value in ("image", "zip") else "image"
