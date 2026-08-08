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

# 标题内「日文原名｜中文翻译」的分隔符（全角/半角竖线）
PIPE_RE = re.compile(r"[｜|]")

# 日文假名（平假名/片假名）与汉字字符范围
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def find_existing_translation(text: str) -> str | None:
    """从标题片段中找出可直接使用的已有翻译，无需调用 AI。

    处理两种情况：
    1. 片段含 `｜`/`|` 分隔（wnacg 部分标题格式为「日文原名｜中文翻译」）：
       分段后选择含日文假名最少且含汉字的一段作为已有翻译；
       若候选段不含汉字（如英文），视为无法直接使用，返回 None。
    2. 片段本身就是中文（无假名且含汉字）：直接返回，避免对中文调用翻译。

    Returns:
        可直接使用的翻译文本；无法判断时返回 None（交给 AI 翻译）。
    """
    parts = [p.strip() for p in PIPE_RE.split(text or "") if p.strip()]
    if len(parts) >= 2:
        # 选假名最少的一段，且必须含汉字（排除纯英文等候选）
        best = min(parts, key=lambda p: len(_KANA_RE.findall(p)))
        if _CJK_RE.search(best):
            return best
        return None
    # 无分隔符：整段已是中文则直接使用，否则交给 AI
    if _CJK_RE.search(text or "") and not _KANA_RE.search(text or ""):
        return text.strip()
    return None


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
