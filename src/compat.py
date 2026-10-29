"""运行时兼容修补：抵消第三方库在「冻结打包」环境下对可选依赖的误判。

目前只有一处：残缺的 `backports.zstd` 会让 urllib3 在 **import 阶段** 直接崩掉，
而 import requests 失败等于程序无法启动，所以必须在下游库加载前把它修掉。

纯标准库实现：不依赖 Qt，也不依赖 requests（否则就成了要修的东西本身）。
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

# urllib3 用到这两个名字：ZstdError 进 DECODER_ERROR_CLASSES，
# ZstdDecompressor 用来构造解码器。缺任何一个都说明这份 backports.zstd 是残的。
_ZSTD_REQUIRED_ATTRS = ("ZstdError", "ZstdDecompressor")


def _disable_zstd_module(reason: str) -> None:
    """把 `backports.zstd` 变成「不可 import」（而不是「能 import 但缺东西」）。

    两步缺一不可：
    - `sys.modules[name] = None`：后续 import 直接抛 ImportError
      （"import of backports.zstd halted; None in sys.modules"）；
    - 摘掉父包上的 `zstd` 属性：`from backports import zstd` 会先 getattr，
      留着旧对象就绕过了 sys.modules 的拦截。
    """
    sys.modules["backports.zstd"] = None
    parent = sys.modules.get("backports")
    if parent is not None and hasattr(parent, "zstd"):
        try:
            delattr(parent, "zstd")
        except AttributeError:      # 并发下已被别人摘掉，无妨
            pass
    logger.warning(
        "backports.zstd 不可用（%s），已降级为「不支持 zstd 解压」，"
        "以免 urllib3 在导入阶段崩溃",
        reason,
    )


def ensure_zstd_compat() -> bool:
    """把「能 import 但缺关键属性」的 `backports.zstd` 降级为「不可用」。

    urllib3 2.5+ 用 `from backports import zstd` **是否成功** 来判断要不要启用
    zstd 解压，随后在 `class BaseHTTPResponse` 的类体里直接取 `zstd.ZstdError`。
    打包环境里有时会收进一份**残缺**的 `backports.zstd`（包目录在、编译扩展不在）：
    import 成功但属性缺失，于是 `import requests` 当场抛

        AttributeError: module 'backports.zstd' has no attribute 'ZstdError'

    这里改为让它 import 失败，urllib3 会走正常的 `except ImportError` 分支、按
    「不支持 zstd」处理（也不会在 Accept-Encoding 里声明 zstd）。代价只是失去
    zstd 响应解压——本项目抓取的站点不使用该编码，换来的是程序能正常启动。

    Returns:
        True 表示本次做了降级处理；False 表示无需处理（未安装 / 完好 / 已处理过）。
    """
    try:
        from backports import zstd
    except ImportError:
        # 未安装，或已被本函数处理过（sys.modules 里是 None）。urllib3 同样会
        # 收到 ImportError，行为一致。
        return False
    except Exception as exc:        # noqa: BLE001 - 残缺形态可能是缺 DLL 等任意错误
        # 连 import 都炸了：urllib3 只捕 ImportError，不处理的话启动同样会挂。
        _disable_zstd_module(f"导入异常 {type(exc).__name__}: {exc}")
        return True

    missing = [name for name in _ZSTD_REQUIRED_ATTRS if not hasattr(zstd, name)]
    if not missing:
        return False

    _disable_zstd_module("缺少 " + " / ".join(missing))
    return True
