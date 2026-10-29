"""后端逻辑包：爬虫与下载。"""

from .compat import ensure_zstd_compat

# 必须在包内任何模块 `import requests` 之前执行：urllib3 在 import 期就决定
# 是否启用 zstd，残缺的 backports.zstd 会让 import requests 直接抛
# AttributeError（打包环境实测），进而整个程序起不来。详见 src/compat.py。
ensure_zstd_compat()
