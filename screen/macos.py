"""macOS 后端 —— 截图用系统自带的 `screencapture`, 输入用 Quartz (CGEvent)。

依赖:
  · 截图: `screencapture` (macOS 自带, 不用装任何东西)
  · 输入/窗口: pip install pyobjc-framework-Quartz
    (窗口切换优先用 AppKit, 没装就退回 osascript)

系统权限 (缺了会"操作成功但没反应", 很容易误判成代码 bug):
  · 系统设置 -> 隐私与安全性 -> 屏幕录制      (截图)
  · 系统设置 -> 隐私与安全性 -> 辅助功能      (鼠标/键盘)

**Retina 缩放**是这里最容易搞错的地方: 截图像素是坐标点的 2 倍
(CGDisplayPixelsWide / CGDisplayBounds().width)。本模块对外统一用
**截图像素**, 内部再换算回坐标点 —— 换算错了表现就是"点偏一半"。

⚠ 本文件没有在真机 macOS 上运行验证过 (开发者只有 Windows)。
   README 里列了需要在 macOS 上逐条确认的检查项。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from .base import Backend, BackendError, Capabilities, WindowInfo, normalize_key

if sys.platform != "darwin":  # pragma: no cover
    raise ImportError("screen.macos 只能在 macOS 上导入")

# macOS 虚拟键码 (跟 Windows 的 VK 完全不是一套)
_KEYCODES = {
    "esc": 53, "enter": 36, "tab": 48, "space": 49,
    "backspace": 51, "delete": 117,
    "up": 126, "down": 125, "left": 123, "right": 124,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
}

_MOUSE_EVENTS = {
    ("left", True): 1, ("left", False): 2,
    ("right", True): 3, ("right", False): 4,
    ("middle", True): 25, ("middle", False): 26,
}
_BUTTON_NUMBERS = {"left": 0, "right": 1, "middle": 2}


def _quartz():
    try:
        import Quartz  # type: ignore

        return Quartz
    except ImportError as exc:
        raise BackendError(
            "macOS 上需要 Quartz: pip install pyobjc-framework-Quartz\n"
            "(截图不需要它, 但鼠标/键盘/窗口列表需要)"
        ) from exc


def _run(argv: list[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendError(f"执行 {argv[0]} 失败: {exc}") from exc
    return done.returncode, done.stdout, done.stderr


class MacOSBackend(Backend):
    name = "macos"

    def __init__(self) -> None:
        self._scale: float | None = None
        self._tmp_suffix = ".png"

    # ---- 缩放 ----------------------------------------------------------

    def scale(self) -> float:
        """截图像素 / 坐标点。Retina 屏是 2.0。"""
        if self._scale is not None:
            return self._scale
        Quartz = _quartz()
        try:
            display = Quartz.CGMainDisplayID()
            pixels = float(Quartz.CGDisplayPixelsWide(display))
            points = float(Quartz.CGDisplayBounds(display).size.width)
            self._scale = round(pixels / points, 3) if points else 1.0
        except Exception:
            self._scale = 1.0
        return self._scale

    def _to_points(self, x: float, y: float) -> tuple[float, float]:
        scale = self.scale() or 1.0
        return (x / scale, y / scale)

    # ---- 屏幕 ----------------------------------------------------------

    def screen_bounds(self) -> tuple[int, int, int, int]:
        """所有活动显示器的并集, 单位是截图像素。"""
        Quartz = _quartz()
        try:
            ids = Quartz.CGGetActiveDisplayList(16, None, None)[1]
        except Exception as exc:
            raise BackendError(f"枚举显示器失败: {exc}") from exc
        if not ids:
            raise BackendError("没有活动的显示器")
        left = top = None
        right = bottom = None
        for display in ids:
            bounds = Quartz.CGDisplayBounds(display)
            x, y = bounds.origin.x, bounds.origin.y
            w, h = bounds.size.width, bounds.size.height
            left = x if left is None else min(left, x)
            top = y if top is None else min(top, y)
            right = x + w if right is None else max(right, x + w)
            bottom = y + h if bottom is None else max(bottom, y + h)
        scale = self.scale()
        return (int(left * scale), int(top * scale),
                int((right - left) * scale), int((bottom - top) * scale))

    def capture(self, region: tuple[int, int, int, int] | None = None):
        from PIL import Image

        x, y, w, h = region or self.screen_bounds()
        px, py = self._to_points(x, y)
        pw, ph = self._to_points(w, h)
        handle, path = tempfile.mkstemp(suffix=self._tmp_suffix)
        os.close(handle)
        try:
            rect = f"-R{int(px)},{int(py)},{max(1, int(pw))},{max(1, int(ph))}"
            code, _out, err = _run(["screencapture", "-x", "-t", "png", rect, path])
            if code != 0 or not os.path.getsize(path):
                raise BackendError(
                    f"screencapture 失败 (exit {code}) {err.strip()}\n"
                    "多半是没有授予「屏幕录制」权限: 系统设置 -> 隐私与安全性 -> 屏幕录制"
                )
            with Image.open(path) as img:
                return img.convert("RGB").copy()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # ---- 输入 ----------------------------------------------------------

    def move(self, x: int, y: int) -> None:
        Quartz = _quartz()
        px, py = self._to_points(x, y)
        event = Quartz.CGEventCreateMouseEvent(None, 5, (px, py), 0)  # 5 = moved
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def button(self, button: str, down: bool) -> None:
        Quartz = _quartz()
        event_type = _MOUSE_EVENTS.get((button, down))
        if event_type is None:
            raise BackendError(f"未知鼠标键: {button!r} (支持 left/right/middle)")
        # CGEvent 不会记住上一个位置, 需要显式取当前光标点
        current = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        event = Quartz.CGEventCreateMouseEvent(None, event_type, current,
                                               _BUTTON_NUMBERS[button])
        if button == "middle":
            # 中键要走 OtherMouse*, 并且把按钮号写进事件里
            Quartz.CGEventSetIntegerValueField(
                event, Quartz.kCGMouseEventButtonNumber, 2)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def key(self, name: str, down: bool) -> None:
        Quartz = _quartz()
        code = _KEYCODES[normalize_key(name)]
        event = Quartz.CGEventCreateKeyboardEvent(None, code, bool(down))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    # ---- 窗口 ----------------------------------------------------------

    def windows(self) -> list[WindowInfo]:
        Quartz = _quartz()
        try:
            raw = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
        except Exception as exc:
            raise BackendError(f"取窗口列表失败: {exc}") from exc
        scale = self.scale()
        out: list[WindowInfo] = []
        for item in raw or []:
            bounds = item.get("kCGWindowBounds") or {}
            width = int(bounds.get("Width", 0))
            height = int(bounds.get("Height", 0))
            if width <= 1 or height <= 1:
                continue
            # 只要正常窗口层 (0), 跳过菜单栏/程序坞这些悬浮层
            if int(item.get("kCGWindowLayer", 0)) != 0:
                continue
            title = item.get("kCGWindowName") or ""
            owner = item.get("kCGWindowOwnerName") or ""
            if not title and not owner:
                continue
            out.append(WindowInfo(
                title=title or owner,
                app=owner,
                handle=int(item.get("kCGWindowNumber", 0)),
                x=int(bounds.get("X", 0) * scale),
                y=int(bounds.get("Y", 0) * scale),
                width=int(width * scale),
                height=int(height * scale),
            ))
        return out

    def foreground(self):
        try:
            from AppKit import NSWorkspace  # type: ignore

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return app.processIdentifier() if app else None
        except Exception:
            return None

    def focus(self, handle) -> bool:
        """把窗口所在的应用切到前台。

        macOS 没有"按窗口切前台"的公开接口, 只能切整个应用,
        所以这里按窗口号找到 pid 再激活它。
        """
        pid = self._pid_of_window(handle)
        if pid is None:
            return False
        try:
            from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is None:
                return False
            return bool(app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
        except Exception:
            code, _out, err = _run([
                "osascript", "-e",
                f'tell application "System Events" to set frontmost of '
                f'(first process whose unix id is {pid}) to true',
            ])
            if code != 0:
                raise BackendError(
                    f"切换前台失败 {err.strip()}\n"
                    "需要在 系统设置 -> 隐私与安全性 -> 辅助功能 里授权"
                )
            return True

    def _pid_of_window(self, handle) -> int | None:
        Quartz = _quartz()
        try:
            raw = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
        except Exception:
            return None
        for item in raw or []:
            if int(item.get("kCGWindowNumber", -1)) == int(handle):
                return int(item.get("kCGWindowOwnerPID", 0)) or None
        return None

    # ---- 元信息 --------------------------------------------------------

    def capabilities(self) -> Capabilities:
        notes: list[str] = []
        quartz_ok = True
        try:
            _quartz()
        except BackendError:
            quartz_ok = False
            notes.append("没装 pyobjc-framework-Quartz: 鼠标/键盘/窗口不可用, 只能截图")
        notes.append("需要「屏幕录制」和「辅助功能」两项系统权限")
        return Capabilities(capture=True, input_mouse=quartz_ok,
                            input_keys=quartz_ok, windows=quartz_ok, notes=notes)

    def describe(self) -> dict:
        info = super().describe()
        info["scale"] = self.scale()
        return info