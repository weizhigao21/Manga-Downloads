"""应用设置与项目配置管理。"""

import json

from opencc import OpenCC

from src.config import ProjectConfig
from src.utils import data_root

# 应用数据根目录（源码=项目根，打包后=exe 所在目录）
ROOT_DIR = data_root()
SETTINGS_PATH = ROOT_DIR / "Config" / "settings.json"

DEFAULT_SETTINGS = {
    "auto_fetch_on_startup": True,
    # 名称翻译（DeepSeek / OpenAI 兼容接口）
    "translate_api_base": "https://api.deepseek.com",
    "translate_model": "deepseek-v4-pro",
    "translate_api_key": "",
    # 存储路径（空字符串 = 使用默认位置 Config/comics.db 与 Config/covers）
    "db_path": "",
    "covers_dir": "",
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

    @property
    def translate_api_base(self) -> str:
        """翻译接口地址（OpenAI 兼容，默认 DeepSeek）。"""
        return str(self._settings.get("translate_api_base", "https://api.deepseek.com"))

    @translate_api_base.setter
    def translate_api_base(self, value: str):
        self._settings["translate_api_base"] = str(value).strip()

    @property
    def translate_model(self) -> str:
        """翻译使用的模型名。"""
        return str(self._settings.get("translate_model", "deepseek-v4-pro"))

    @translate_model.setter
    def translate_model(self, value: str):
        self._settings["translate_model"] = str(value).strip()

    @property
    def translate_api_key(self) -> str:
        """翻译 API Key（仅保存在本地 settings.json）。"""
        return str(self._settings.get("translate_api_key", ""))

    @translate_api_key.setter
    def translate_api_key(self, value: str):
        self._settings["translate_api_key"] = str(value).strip()

    @property
    def db_path(self) -> str:
        """漫画数据库文件路径（空字符串表示默认位置）。"""
        return str(self._settings.get("db_path", "")).strip()

    @db_path.setter
    def db_path(self, value: str):
        self._settings["db_path"] = str(value).strip()

    @property
    def covers_dir(self) -> str:
        """封面缓存目录（空字符串表示默认位置）。"""
        return str(self._settings.get("covers_dir", "")).strip()

    @covers_dir.setter
    def covers_dir(self, value: str):
        self._settings["covers_dir"] = str(value).strip()

    def convert(self, text: str) -> str:
        """根据设置将文本转换为简体（若开启）或保持原样。"""
        if not self.convert_traditional_to_simplified:
            return text
        if not text:
            return text
        return self._converter.convert(text)


