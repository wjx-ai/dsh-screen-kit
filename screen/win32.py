"""Windows 后端 —— 纯 ctypes, 零第三方依赖。

为什么手写而不是用 pyautogui / mss:
  目标机器上可能装不了东西 (公司电脑、离线环境), ctypes 永远可用。
另外需要自己控制 DPI 感知和虚拟桌面多屏坐标, 第三方库这两点常常不对。

这一层只做系统调用封装, 不含任何"该点哪里"的决策。
"""
from __future__ import annotations

import ctypes
import sys
import time

from .base import Backend, BackendError, Capabilities, WindowInfo, normalize_key

if sys.platform != "win32":  # pragma: no cover - 只为在别的平台上 import 不炸
    raise ImportError("screen.win32 只能在 Windows 上导入")

from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

H = ctypes.c_void_p
ULONG_PTR = ctypes.c_void_p

# ---- 常量 ---------------------------------------------------------------
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SW_RESTORE = 9

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
DIB_RGB_COLORS, SRCCOPY, CAPTUREBLT, BI_RGB = 0, 0x00CC0020, 0x40000000, 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# ---- 结构体 -------------------------------------------------------------
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# ---- 函数原型 (显式声明 restype, 否则 64 位指针会被截断) ----------------
user32.GetDC.restype = H
user32.GetDC.argtypes = [H]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [H, H]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SendInput.restype = wintypes.UINT
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetForegroundWindow.restype = H
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [H]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [H]
user32.SetFocus.restype = H
user32.SetFocus.argtypes = [H]
user32.ShowWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [H, ctypes.c_int]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [H]
user32.IsIconic.restype = wintypes.BOOL
user32.IsIconic.argtypes = [H]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [H]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [H, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [H, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [H, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [H, ctypes.POINTER(wintypes.DWORD)]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [H]
user32.GetDpiForSystem.restype = wintypes.UINT
user32.SetProcessDPIAware.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.restype = H
gdi32.CreateCompatibleDC.argtypes = [H]
gdi32.CreateDIBSection.restype = H
gdi32.CreateDIBSection.argtypes = [H, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(H), H, wintypes.DWORD]
gdi32.SelectObject.restype = H
gdi32.SelectObject.argtypes = [H, H]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [H]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [H]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          H, ctypes.c_int, ctypes.c_int, wintypes.DWORD]

kernel32.OpenProcess.restype = H
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [H]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [H, wintypes.DWORD, ctypes.c_wchar_p,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, H, H)
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumWindows.argtypes = [WNDENUMPROC, H]

# 功能键 -> 虚拟键码。换成 macOS/Linux 时只是这张表不一样。
_VK = {
    "esc": 0x1B, "enter": 0x0D, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
}
# 方向键和 Home/End 等是"扩展键", 不加这个标志有的程序收不到
_EXTENDED = {"up", "down", "left", "right", "home", "end", "pageup", "pagedown", "delete"}

_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

_DPI_DONE = False


def ensure_dpi_aware() -> str:
    """必须在任何截屏/取窗口尺寸之前调用, 否则坐标会被系统缩放二次污染。"""
    global _DPI_DONE
    if _DPI_DONE:
        return "already"
    _DPI_DONE = True
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        if shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (OSError, AttributeError):
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system-aware"
    except (OSError, AttributeError):
        pass
    return "none"


def _send_inputs(*inputs: INPUT) -> None:
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise BackendError(
            f"SendInput 只送出 {sent}/{len(inputs)} 个事件 "
            f"(错误码 {ctypes.get_last_error()}); 可能被 UIPI/杀软拦了"
        )


class WindowsBackend(Backend):
    name = "windows"

    def __init__(self) -> None:
        ensure_dpi_aware()

    # ---- 屏幕 ----------------------------------------------------------

    def screen_bounds(self) -> tuple[int, int, int, int]:
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w <= 0 or h <= 0:
            raise BackendError("取不到屏幕尺寸 (没有图形会话? 或者跑在服务里)")
        return (x, y, w, h)

    def capture(self, region: tuple[int, int, int, int] | None = None):
        from PIL import Image

        x, y, w, h = region or self.screen_bounds()
        width, height = int(w), int(h)
        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            raise BackendError("GetDC 失败")
        mem_dc = bmp = old = None
        try:
            mem_dc = gdi32.CreateCompatibleDC(hdc_screen)
            if not mem_dc:
                raise BackendError("CreateCompatibleDC 失败")
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = width
            bmi.bmiHeader.biHeight = -height          # 负数 = 自上而下
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = BI_RGB
            bits = H()
            bmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                         ctypes.byref(bits), None, 0)
            if not bmp or not bits:
                raise BackendError("CreateDIBSection 失败")
            old = gdi32.SelectObject(mem_dc, bmp)
            if not gdi32.BitBlt(mem_dc, 0, 0, width, height, hdc_screen, int(x), int(y),
                                SRCCOPY | CAPTUREBLT):
                raise BackendError(
                    "BitBlt 失败 —— 屏幕已锁或有 UAC 安全桌面时 Windows 不允许抓取"
                )
            raw = ctypes.string_at(bits, width * height * 4)
        finally:
            if mem_dc and old:
                gdi32.SelectObject(mem_dc, old)
            if bmp:
                gdi32.DeleteObject(bmp)
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, hdc_screen)
        return Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", 0, 1).convert("RGB")

    # ---- 输入 ----------------------------------------------------------

    def move(self, x: int, y: int) -> None:
        if not user32.SetCursorPos(int(x), int(y)):
            raise BackendError(f"SetCursorPos({x},{y}) 失败")

    def cursor(self) -> tuple[int, int]:
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            raise BackendError("GetCursorPos 失败")
        return (point.x, point.y)

    def button(self, button: str, down: bool) -> None:
        pair = _BUTTON_FLAGS.get(button)
        if pair is None:
            raise BackendError(f"未知鼠标键: {button!r} (支持 left/right/middle)")
        item = INPUT()
        item.type = INPUT_MOUSE
        item.union.mi = MOUSEINPUT(0, 0, 0, pair[0] if down else pair[1], 0, None)
        _send_inputs(item)

    def key(self, name: str, down: bool) -> None:
        key = normalize_key(name)
        vk = _VK[key]
        item = INPUT()
        item.type = INPUT_KEYBOARD
        flags = 0
        if not down:
            flags |= KEYEVENTF_KEYUP
        if key in _EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        item.union.ki = KEYBDINPUT(vk, 0, flags, 0, None)
        _send_inputs(item)

    # ---- 窗口 ----------------------------------------------------------

    def windows(self) -> list[WindowInfo]:
        found: list[WindowInfo] = []
        seen: set[int] = set()

        def callback(hwnd, _lparam):
            value = int(hwnd or 0)
            if value in seen:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 2)
            user32.GetWindowTextW(hwnd, buffer, length + 2)
            title = buffer.value.strip()
            if not title:
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = max(rect.right - rect.left, 0)
            height = max(rect.bottom - rect.top, 0)
            if width <= 1 or height <= 1:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            seen.add(value)
            found.append(WindowInfo(
                title=title,
                app=self._process_name(int(pid.value)),
                handle=value,
                x=rect.left, y=rect.top, width=width, height=height,
            ))
            return True

        user32.EnumWindows(WNDENUMPROC(callback), None)
        return found

    def _process_name(self, pid: int) -> str:
        if not pid:
            return ""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value.rsplit("\\", 1)[-1]
            return ""
        finally:
            kernel32.CloseHandle(handle)

    def foreground(self):
        return int(user32.GetForegroundWindow() or 0)

    def focus(self, handle) -> bool:
        """把窗口顶到前台。

        Windows 有"前台锁定": 非前台进程直接 SetForegroundWindow 常常静默失败。
        标准解法是把当前前台线程的输入队列附加到自己线程上再切换。
        """
        hwnd = H(int(handle))
        if not user32.IsWindow(hwnd):
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        if self.foreground() == int(handle):
            return True
        current = kernel32.GetCurrentThreadId()
        foreground = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = False
        try:
            if fg_thread and fg_thread != current:
                attached = bool(user32.AttachThreadInput(fg_thread, current, True))
            user32.BringWindowToTop(hwnd)
            user32.ShowWindow(hwnd, SW_RESTORE)
            ok = bool(user32.SetForegroundWindow(hwnd))
            user32.SetFocus(hwnd)
            return ok or self.foreground() == int(handle)
        finally:
            if attached:
                user32.AttachThreadInput(fg_thread, current, False)

    # ---- 元信息 --------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return Capabilities(capture=True, input_mouse=True, input_keys=True, windows=True)

    def describe(self) -> dict:
        info = super().describe()
        try:
            info["dpi_scale"] = round(user32.GetDpiForSystem() / 96.0, 3)
        except (AttributeError, OSError):
            info["dpi_scale"] = 1.0
        return info


def sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000.0)