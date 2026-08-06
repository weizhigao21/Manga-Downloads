"""应用设置与项目配置管理。"""

import json
from pathlib import Path

from opencc import OpenCC

# 项目根目录
ROOT_DIR = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT_DIR / "Config" / "settings.json"
PROJECT_CONFIG_PATH = ROOT_DIR / "Config" / "Config.json"

DEFAULT_SETTINGS = {
    "auto_fetch_on_startup": True,
}

DEFAULT_PROJECT_CONFIG = {
    "Page_number": 20,
    "Download": "./Download",
    "list_url": "https://www.wnacg.com/albums.html",
    "Download_workers": 3,
    "Download_retries": 3,
    "Folder_use_category_label": True,
    "Download_mode": "image",
}

# 网站导航栏分类（名称 -> 分类列表页相对 URL），供设置中切换浏览分类
CATEGORIES: dict[str, str] = {
    "全部更新": "albums.html",
    "同人誌": "albums-index-cate-5.html",
    "同人誌／漢化": "albums-index-cate-1.html",
    "同人誌／日語": "albums-index-cate-12.html",
    "同人誌／English": "albums-index-cate-16.html",
    "同人誌／CG畫集": "albums-index-cate-2.html",
    "同人誌／AI圖集": "albums-index-cate-37.html",
    "同人誌／3D漫畫": "albums-index-cate-22.html",
    "同人誌／Cosplay": "albums-index-cate-3.html",
    "單行本": "albums-index-cate-6.html",
    "單行本／漢化": "albums-index-cate-9.html",
    "單行本／日語": "albums-index-cate-13.html",
    "單行本／English": "albums-index-cate-17.html",
    "雜誌&短篇": "albums-index-cate-7.html",
    "雜誌&短篇／漢化": "albums-index-cate-10.html",
    "雜誌&短篇／日語": "albums-index-cate-14.html",
    "雜誌&短篇／English": "albums-index-cate-18.html",
    "韓漫": "albums-index-cate-19.html",
    "韓漫／漢化": "albums-index-cate-20.html",
    "韓漫／其他": "albums-index-cate-21.html",
}

# 列表页 URL 前缀（完整 URL = BASE_URL + 分类相对路径）
BASE_URL = "https://www.wnacg.com"


class AppSettings:
    """管理应用本地设置，支持繁简转换与自动抓取开关。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self):
        self._settings = DEFAULT_SETTINGS.copy()
        if SETTINGS_PATH.exists():
            try:
                with open(SETTINGS_PATH, encoding="utf-8") as f:
                    loaded = json.load(f)
                    self._settings.update(loaded)
            except (json.JSONDecodeError, OSError) as exc:
                print(f"加载设置失败: {exc}", flush=True)
        # 迁移旧版繁简体设置：旧值保存在 settings.json 中，现统一存入 Config.json
        if "convert_traditional_to_simplified" in self._settings:
            project_config = ProjectConfig()
            if project_config.convert_traditional_to_simplified is None:
                project_config.convert_traditional_to_simplified = self._settings[
                    "convert_traditional_to_simplified"
                ]
                project_config.save()
            # 迁移完成后从 settings.json 移除，避免双源不一致
            self._settings.pop("convert_traditional_to_simplified", None)
            self._save_settings()
        self._converter = OpenCC("t2s")  # 繁体转简体

    def _save_settings(self):
        """将应用设置写入 settings.json。"""
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_PATH, encoding="utf-8", mode="w") as f:
            json.dump(self._settings, f, ensure_ascii=False, indent=2)

    def save(self):
        """保存当前设置到文件。"""
        self._save_settings()

    @property
    def convert_traditional_to_simplified(self) -> bool:
        return bool(ProjectConfig().convert_traditional_to_simplified)

    @convert_traditional_to_simplified.setter
    def convert_traditional_to_simplified(self, value: bool):
        project_config = ProjectConfig()
        project_config.convert_traditional_to_simplified = bool(value)
        project_config.save()

    @property
    def auto_fetch_on_startup(self) -> bool:
        return bool(self._settings.get("auto_fetch_on_startup", True))

    @auto_fetch_on_startup.setter
    def auto_fetch_on_startup(self, value: bool):
        self._settings["auto_fetch_on_startup"] = bool(value)

    def convert(self, text: str) -> str:
        """根据设置将文本转换为简体（若开启）或保持原样。"""
        if not self.convert_traditional_to_simplified:
            return text
        if not text:
            return text
        return self._converter.convert(text)


class ProjectConfig:
    """管理项目级配置文件 Config/Config.json。"""

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
        return int(self._config.get("Download_workers", 3))

    @download_workers.setter
    def download_workers(self, value: int):
        self._config["Download_workers"] = int(value)

    @property
    def download_retries(self) -> int:
        return int(self._config.get("Download_retries", 3))

    @download_retries.setter
    def download_retries(self, value: int):
        self._config["Download_retries"] = int(value)

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
