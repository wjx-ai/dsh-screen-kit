"""dsh-screen-kit 的 Python 侧。

对外只有两样东西:
  · `load_backend()`  —— 选一个平台后端
  · `ocr` 模块        —— 把截图变成带坐标的文字
"""
from .base import (
    Backend,
    BackendError,
    Capabilities,
    WindowInfo,
    current_platform,
    load_backend,
)

__all__ = [
    "Backend",
    "BackendError",
    "Capabilities",
    "WindowInfo",
    "current_platform",
    "load_backend",
]
__version__ = "0.2.0"