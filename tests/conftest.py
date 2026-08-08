"""pytest 配置：项目根加入 sys.path；UI 测试使用离屏（offscreen）平台。"""
import os
import sys
from pathlib import Path

# 无头/CI 环境下 Qt 测试需要离屏渲染，必须在导入 PySide6 前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
