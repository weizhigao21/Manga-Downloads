"""通用工具函数。"""

import re
import sys
from pathlib import Path


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


def data_root() -> Path:
    """返回应用数据根目录（Config 目录的所在位置）。

    源码运行时为项目根目录；PyInstaller 打包后 `__file__` 指向只读临时
    解压目录，此时应返回 exe 所在目录，用户可在此放置/读写配置文件。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# 方括号块（社团名/作者/标签等），标题翻译时整体保留
BRACKET_RE = re.compile(r"(\[[^\]]*\])")


def split_title_segments(title: str) -> list[tuple[str, bool]]:
    """将标题按方括号块切分为 [(文本, 是否可翻译)]。

    方括号块（如 `[社团名]`、`[标签]`）标记为不可翻译并原样保留，
    其余主体文本标记为可翻译。

    Examples:
        "[A] 主体 [B]" -> [("[A]", False), (" 主体 ", True), ("[B]", False)]
    """
    segments: list[tuple[str, bool]] = []
    for part in BRACKET_RE.split(title or ""):
        if not part:
            continue
        if part.startswith("[") and part.endswith("]"):
            segments.append((part, False))
        else:
            segments.append((part, True))
    return segments
