"""后端抽象: 屏幕截图 / 鼠标 / 键盘 / 窗口。

所有后端对外的坐标都用**截图像素**。各平台输入坐标的单位可能不一样
(macOS 的 Retina 屏幕尤其明显: 截图像素是坐标点的 2 倍),
所以每个后端自己负责把截图像素换算成系统输入坐标, 上层不用关心。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any


class BackendError(RuntimeError):
    """后端不可用或操作失败。消息要能直接给用户看 —— 说清缺什么、怎么装。"""


@dataclass
class WindowInfo:
    title: str
    app: str = ""
    handle: Any = None
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "app": self.app,
            "handle": self.handle,
            "rect": [self.x, self.y, self.width, self.height],
        }


@dataclass
class Capabilities:
    """这个后端能做什么。缺的能力要在启动时就告诉用户, 而不是用的时候才炸。"""

    capture: bool = False
    input_mouse: bool = False
    input_keys: bool = False
    windows: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "capture": self.capture,
            "input_mouse": self.input_mouse,
            "input_keys": self.input_keys,
            "windows": self.windows,
            "notes": self.notes,
        }


class Backend:
    """平台后端基类。子类只需要实现自己真能做的事。"""

    name = "base"

    # ---- 屏幕 ------------------------------------------------------------

    def screen_bounds(self) -> tuple[int, int, int, int]:
        """(x, y, width, height), 单位是截图像素。多屏时可能是负原点。"""
        raise BackendError(f"{self.name}: 没有实现 screen_bounds()")

    def capture(self, region: tuple[int, int, int, int] | None = None):
        """截屏, 返回 PIL Image (RGB)。region=(x, y, w, h) 用截图像素。"""
        raise BackendError(f"{self.name}: 没有实现 capture()")

    # ---- 输入 ------------------------------------------------------------

    def move(self, x: int, y: int) -> None:
        """把鼠标移到截图像素 (x, y)。"""
        raise BackendError(f"{self.name}: 不支持移动鼠标")

    def button(self, button: str, down: bool) -> None:
        """按下/松开鼠标键: left / right / middle。"""
        raise BackendError(f"{self.name}: 不支持鼠标点击")

    def key(self, name: str, down: bool) -> None:
        """按下/松开一个功能键 (名字见 base.KEY_ALIASES)。"""
        raise BackendError(f"{self.name}: 不支持键盘")

    def cursor(self) -> tuple[int, int]:
        raise BackendError(f"{self.name}: 不支持读取光标位置")

    # ---- 窗口 ------------------------------------------------------------

    def windows(self) -> list[WindowInfo]:
        return []

    def focus(self, handle: Any) -> bool:
        return False

    def foreground(self) -> Any:
        return None

    def ensure_active(self, x: int, y: int) -> bool:
        """保证 (x, y) 所在的窗口是活动窗口。默认不做任何事。

        为什么需要: 有些应用 (Electron、自绘界面) 只在自己是前台窗口时才把点击
        传给控件 —— 非前台时第一次点击只当"激活窗口", 控件收不到。
        表现就是"OCR 明明找到了按钮, 点下去什么也没发生"。
        """
        return True

    # ---- 元信息 ----------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return Capabilities()

    def describe(self) -> dict:
        return {"backend": self.name, "capabilities": self.capabilities().as_dict()}


# 允许按的键。故意不提供"打任意字符" —— 那等于把往任何窗口输入的权限交出去。
KEY_ALIASES = {
    "esc": "esc", "escape": "esc",
    "enter": "enter", "return": "enter",
    "tab": "tab", "space": "space",
    "backspace": "backspace", "delete": "delete", "del": "delete",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
}


def normalize_key(name: str) -> str:
    key = KEY_ALIASES.get(str(name or "").strip().lower())
    if key is None:
        raise BackendError(
            f"不支持的按键 {name!r}; 支持: {', '.join(sorted(set(KEY_ALIASES.values())))}"
        )
    return key


def current_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def load_backend(preferred: str = "") -> Backend:
    """按平台挑后端。preferred 可以强制指定 (测试用)。"""
    which = (preferred or current_platform()).lower()
    if which in ("windows", "win32"):
        from .win32 import WindowsBackend

        return WindowsBackend()
    if which in ("macos", "darwin", "mac"):
        from .macos import MacOSBackend

        return MacOSBackend()
    if which in ("linux", "x11", "wayland"):
        from .linux import LinuxBackend

        return LinuxBackend()
    raise BackendError(
        f"不认识的平台 {which!r}。支持: windows / macos / linux "
        f"(当前系统是 {sys.platform})"
    )