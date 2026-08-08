"""作品名称 AI 翻译（DeepSeek / OpenAI 兼容接口）。

通过 OpenAI SDK 调用兼容接口（默认 DeepSeek），将漫画标题翻译为简体中文。
openai 库为可选依赖，调用时才导入，未安装时给出明确提示。
"""
import logging

from PySide6.QtCore import QThread, Signal

from src.utils import split_title_segments

logger = logging.getLogger(__name__)

# 翻译指令：只输出翻译结果，不带解释与引号
SYSTEM_PROMPT = (
    "你是专业的漫画翻译。将用户输入的作品名称翻译为简体中文，"
    "只输出翻译结果本身，不要任何解释、注释或引号。"
)


def translate_title(title: str, api_base: str, api_key: str, model: str) -> str:
    """翻译完整标题：方括号块（社团/作者/标签）原样保留，只翻译主体文本。

    如 `[钢鉄しゃぼん玉 (玉ぼん)] 続·カノジョは… [惑星]` 仅翻译中间
    的作品名，方括号内容不翻译，翻译结果按原顺序拼回。
    """
    segments = split_title_segments(title)
    translated_parts: list[str] = []
    for text, translatable in segments:
        if translatable and text.strip():
            # 保留片段原有首尾空白，翻译结果只替换中间内容
            leading = text[: len(text) - len(text.lstrip())]
            trailing = text[len(text.rstrip()):]
            translated_parts.append(
                f"{leading}{translate_text(text, api_base, api_key, model)}{trailing}"
            )
        else:
            translated_parts.append(text)
    return "".join(translated_parts)


def translate_text(text: str, api_base: str, api_key: str, model: str) -> str:
    """调用翻译接口，将作品名称翻译为简体中文。

    Args:
        text: 待翻译的作品名称。
        api_base: OpenAI 兼容接口地址（如 https://api.deepseek.com）。
        api_key: API Key。
        model: 模型名（如 deepseek-v4-pro）。

    Returns:
        翻译结果；输入为空时原样返回。

    Raises:
        RuntimeError: 未安装 openai 库、未配置 Key 或接口调用失败。
    """
    text = (text or "").strip()
    if not text:
        return text
    if not api_key:
        raise RuntimeError("未配置 API Key，请先在 设置 → 名称翻译设置 中填写")

    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("未安装 OpenAI SDK，请执行: pip install openai")

    client = OpenAI(api_key=api_key, base_url=api_base)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        stream=False,
    )
    result = response.choices[0].message.content.strip()
    if not result:
        raise RuntimeError("翻译接口返回为空")
    logger.info("翻译成功: %s -> %s", text, result)
    return result


class TranslateWorker(QThread):
    """后台翻译线程，避免网络请求阻塞 UI。

    Signals:
        finished(str): 翻译成功，携带翻译结果。
        error(str): 翻译失败，携带错误信息。
    """

    finished = Signal(str)
    error = Signal(str)

    def __init__(self, text: str, api_base: str, api_key: str, model: str):
        super().__init__()
        self.text = text
        self.api_base = api_base
        self.api_key = api_key
        self.model = model

    def run(self):
        try:
            # 使用 translate_title：方括号块（社团/作者/标签）保留，只翻译主体
            result = translate_title(
                self.text, self.api_base, self.api_key, self.model
            )
            self.finished.emit(result)
        except Exception as exc:
            logger.warning("翻译失败: %s", exc)
            self.error.emit(str(exc))
