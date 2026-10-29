"""`src/compat.py` 守卫：残缺的 backports.zstd 必须被降级，而不是让程序起不来。

背景（真实故障）：打包环境里收进了一份「能 import、但缺 ZstdError」的
`backports.zstd`，urllib3 2.8.0 在 `class BaseHTTPResponse` 的类体里直接取
`zstd.ZstdError`，于是 `import requests` 抛 AttributeError，程序一启动就崩。

这些用例全部离线、不依赖本机是否真的装了 backports.zstd。
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fake_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


@pytest.fixture
def fake_backports(monkeypatch):
    """在 sys.modules 里造一个 backports 父包，并保证用例结束后还原。

    Yields:
        安装函数的工厂：`install(zstd_module)` 把给定的 `backports.zstd` 挂上去。
    """

    monkeypatch.setitem(sys.modules, "backports", _fake_module("backports", __path__=[]))
    monkeypatch.setitem(sys.modules, "backports.zstd", None)
    parent = sys.modules["backports"]

    def install(zstd_module):
        parent.zstd = zstd_module
        sys.modules["backports.zstd"] = zstd_module
        return zstd_module

    return install


def _broken_zstd() -> types.ModuleType:
    """残缺形态：能导入，但缺 urllib3 需要的属性（复刻打包环境的残骸）。"""
    return _fake_module("backports.zstd", __name__="backports.zstd")


def _healthy_zstd() -> types.ModuleType:
    return _fake_module(
        "backports.zstd",
        __name__="backports.zstd",
        ZstdError=type("ZstdError", (Exception,), {}),
        ZstdDecompressor=object,
    )


class TestEnsureZstdCompat:
    def test_broken_module_is_disabled(self, fake_backports):
        """缺 ZstdError：必须变成「import 失败」，urllib3 才会按不支持处理。"""
        from src.compat import ensure_zstd_compat

        fake_backports(_broken_zstd())

        assert ensure_zstd_compat() is True
        assert sys.modules["backports.zstd"] is None
        with pytest.raises(ImportError):
            from backports import zstd  # noqa: F401

    def test_partially_broken_module_is_disabled(self, fake_backports):
        """只缺 ZstdDecompressor 也算残：解码器造不出来，同样必须降级。"""
        from src.compat import ensure_zstd_compat

        fake_backports(_fake_module("backports.zstd", ZstdError=ValueError))

        assert ensure_zstd_compat() is True
        with pytest.raises(ImportError):
            from backports import zstd  # noqa: F401

    def test_healthy_module_is_left_alone(self, fake_backports):
        """完好的 backports.zstd 不许动：zstd 解压能力不该被白白砍掉。"""
        from src.compat import ensure_zstd_compat

        healthy = fake_backports(_healthy_zstd())

        assert ensure_zstd_compat() is False
        from backports import zstd

        assert zstd is healthy
        assert hasattr(zstd, "ZstdError")

    def test_no_backports_installed(self, monkeypatch):
        """没装（常见情况）时是无操作，不能抛异常。"""
        from src.compat import ensure_zstd_compat

        monkeypatch.setitem(sys.modules, "backports", None)
        monkeypatch.setitem(sys.modules, "backports.zstd", None)

        assert ensure_zstd_compat() is False

    def test_broken_import_is_disabled(self, fake_backports, monkeypatch):
        """残缺到 import 就抛非 ImportError（如缺 DLL）时也要降级。

        urllib3 只捕捉 ImportError，其它异常会原样冒出去把启动搞挂。
        这里用一个会抛 OSError 的 finder 模拟「编译扩展加载失败」。
        """
        from src.compat import ensure_zstd_compat

        fake_backports(_broken_zstd())
        parent = sys.modules["backports"]
        parent.__dict__.pop("zstd", None)      # 让 `from backports import zstd` 真去走 import
        sys.modules.pop("backports.zstd", None)

        class _BoomFinder:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "backports.zstd":
                    raise OSError("DLL load failed while importing _zstd")
                return None

        monkeypatch.setattr(sys, "meta_path", [_BoomFinder(), *sys.meta_path])

        assert ensure_zstd_compat() is True
        assert sys.modules["backports.zstd"] is None

    def test_idempotent(self, fake_backports):
        """重复调用只处理一次（包初始化与显式调用都会走这里）。"""
        from src.compat import ensure_zstd_compat

        fake_backports(_broken_zstd())

        assert ensure_zstd_compat() is True
        assert ensure_zstd_compat() is False


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


_FAKE_PREAMBLE = """
import sys, types
bp = types.ModuleType("backports"); bp.__path__ = []
z = types.ModuleType("backports.zstd"); z.__name__ = "backports.zstd"
bp.zstd = z
sys.modules["backports"] = bp
sys.modules["backports.zstd"] = z
"""


class TestGuardIsWiredAndLoadBearing:
    """端到端：`import src` 必须在 urllib3 之前把残骸修掉。

    分两个子进程：urllib3 的可选依赖判定是 import 期一次性算的，
    同进程内无法既验证「会崩」又验证「修好了」。
    """

    def test_without_guard_urllib3_crashes(self):
        """对照组：复刻真实故障，证明这份残骸确实会让 import 崩掉。"""
        result = _run(_FAKE_PREAMBLE + "import urllib3.response\n")
        assert result.returncode != 0
        assert "ZstdError" in (result.stderr or "")

    def test_src_package_neutralises_broken_zstd(self):
        """`import src` 之后同样场景必须安然通过，且 HAS_ZSTD 为 False。"""
        result = _run(
            _FAKE_PREAMBLE
            + "import src\n"
            + "import urllib3.response as r\n"
            + "print('HAS_ZSTD', r.HAS_ZSTD)\n"
        )
        assert result.returncode == 0, result.stderr
        assert "HAS_ZSTD False" in result.stdout


class TestSpecExcludesZstd:
    def test_spec_excludes_backports(self):
        """打包配置必须整体排除 backports：只靠运行期兜底不够。

        少了这条，装过 backports.zstd 的打包机会再次产出「一半」的包。
        """
        spec = (PROJECT_ROOT / "漫画浏览器.spec").read_text(encoding="utf-8")
        excludes_block = spec.split("excludes=[", 1)[1].split("],", 1)[0]
        assert '"backports"' in excludes_block
        assert '"backports.zstd"' in excludes_block
