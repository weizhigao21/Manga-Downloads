"""通用工具函数。"""

import re


def safe_filename(name: str) -> str:
    """将名称转换为可用的目录名。

    处理 Windows 非法字符与易混淆字符：
    - 半角/全角斜杠与竖线（\\ / ／ ＼ | ｜）：替换为下划线，避免被当作路径分隔符
    - 半角/全角 &（& ＆）：替换为下划线
    - : * ? " < > 等非法字符：替换为下划线
    """
    name = re.sub(r'[\\/:*?"<>|｜／＼]', "_", name)
    name = name.replace("&", "_").replace("＆", "_")
    name = re.sub(r"_+", "_", name).strip(" _")
    return name or "untitled"
