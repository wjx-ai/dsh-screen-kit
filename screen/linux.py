"""Linux 后端 —— 输入用 X11 的 XTEST 扩展 (ctypes, 零 pip 依赖),
截图按机器上实际有的工具依次降级。

为什么输入手写 XTEST: 装 xdotool 要多一个系统包, 而 XTEST 只要 libX11 + libXtst,
几乎所有带图形界面的发行版都有。这跟 Windows 那层用 ctypes 是同一个理由。

截图的工具选择 (按优先级):
    mss            pip install mss     最省事、最快
    grim           只有 Wayland 有
    imagemagick    import / magick
    scrot          X11 老工具

⚠ Wayland 下 XTEST 完全无效 —— Wayland 不允许程序直接注入输入事件。
   这种情况下要么用 ydotool (需要额外 daemon), 要么这个插件在该会话里不能点鼠标。
   不要试图绕过: 那是 Wayland 的安全模型, 绕不过去。

⚠ 本文件没有在真机 Linux 上运行验证过 (开发者只有 Windows)。
   README 里列了需要在 Linux 上逐条确认的检查项。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile

from .base import Backend, BackendError, Capabilities, WindowInfo, normalize_key

if not sys.platform.startswith("linux"):  # pragma: no cover
    raise ImportError("screen.linux 只能在 Linux 上导入")

# X11 keysym 名字。XTEST 走 keysym -> keycode 的转换。
_KEYSYMS = {
    "esc": "Escape", "enter": "Return", "tab": "Tab", "space": "space",
    "backspace": "BackSpace", "delete": "Delete",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
}

_BUTTON_NUMBERS = {"left": 1, "middle": 2, "right": 3}

_CURRENT_TIME = 0


def _is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY"))


class _X11:
    """libX11 + libXtst 的最小封装。找不到库就报一条能看懂的错误。"""

    def __init__(self) -> None:
        self.x11 = self._load(("libX11.so.6", "libX11.so"), "X11")
        self.xtst = self._load(("libXtst.so.6", "libXtst.so"), "Xtst (XTEST 扩展)")
        x = self.x11
        t = self.xtst
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XDefaultScreen.argtypes = [ctypes.c_void_p]
        x.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XFlush.argtypes = [ctypes.c_void_p]
        x.XStringToKeysym.restype = ctypes.c_ulong
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        t.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_ulong]
        t.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                          ctypes.c_ulong]
        t.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                       ctypes.c_ulong]
        self.dpy = x.XOpenDisplay(None)
        if not self.dpy:
            raise BackendError(
                "连不上 X 显示 (DISPLAY 没设?)。\n"
                "  · SSH 里跑的话先 export DISPLAY=:0\n"
                "  · Wayland 会话里 XTEST 不可用, 见模块说明"
            )

    def _load(self, names, label: str):
        for name in names:
            try:
                return ctypes.CDLL(name)
            except OSError:
                continue
        raise BackendError(
            f"找不到 {label} 库。装一下:\n"
            "  Debian/Ubuntu: sudo apt install libx11-6 libxtst6\n"
            "  Fedora:        sudo dnf install libX11 libXtst"
        )

    def size(self) -> tuple[int, int]:
        screen = self.x11.XDefaultScreen(self.dpy)
        return (self.x11.XDisplayWidth(self.dpy, screen),
                self.x11.XDisplayHeight(self.dpy, screen))

    def move(self, x: int, y: int) -> None:
        if not self.xtst.XTestFakeMotionEvent(self.dpy, -1, int(x), int(y), _CURRENT_TIME):
            raise BackendError("XTestFakeMotionEvent 失败")
        self.x11.XFlush(self.dpy)

    def button(self, number: int, down: bool) -> None:
        if not self.xtst.XTestFakeButtonEvent(self.dpy, number, bool(down), _CURRENT_TIME):
            raise BackendError("XTestFakeButtonEvent 失败")
        self.x11.XFlush(self.dpy)

    def key(self, keysym_name: str, down: bool) -> None:
        keysym = self.x11.XStringToKeysym(keysym_name.encode())
        if not keysym:
            raise BackendError(f"X11 不认识 keysym {keysym_name!r}")
        keycode = self.x11.XKeysymToKeycode(self.dpy, keysym)
        if not keycode:
            raise BackendError(f"keysym {keysym_name!r} 没有对应的键码")
        if not self.xtst.XTestFakeKeyEvent(self.dpy, keycode, bool(down), _CURRENT_TIME):
            raise BackendError("XTestFakeKeyEvent 失败")
        self.x11.XFlush(self.dpy)


class LinuxBackend(Backend):
    name = "linux"

    def __init__(self) -> None:
        self.wayland = _is_wayland()
        self._x11: _X11 | None = None

    # ---- X11 懒加载 ----------------------------------------------------

    def x11(self) -> _X11:
        if self.wayland:
            raise BackendError(
                "当前是 Wayland 会话, 不允许程序注入鼠标/键盘事件。\n"
                "  · 换成 X11 会话 (登录界面选 \"Xorg\"), 或\n"
                "  · 安装 ydotool 并启动它的 daemon, 然后用它的命令\n"
                "插件本身不会绕过这个限制 —— 那是 Wayland 的安全模型。"
            )
        if self._x11 is None:
            self._x11 = _X11()
        return self._x11

    # ---- 屏幕 ----------------------------------------------------------

    def screen_bounds(self) -> tuple[int, int, int, int]:
        if not self.wayland:
            w, h = self.x11().size()
            return (0, 0, w, h)
        # Wayland 下只能从工具里问尺寸
        code, out, _err = _run(["wlr-randr"]) if shutil.which("wlr-randr") else (1, "", "")
        if code == 0:
            for line in out.splitlines():
                line = line.strip()
                if "current" in line and "x" in line:
                    parts = line.split()
                    for i, token in enumerate(parts):
                        if token == "current" and i + 1 < len(parts):
                            dims = parts[i + 1].split("x")
                            if len(dims) == 2 and dims[0].isdigit():
                                return (0, 0, int(dims[0]), int(dims[1]))
        raise BackendError("Wayland 下取不到屏幕尺寸; 装 wlr-randr 或改用 X11 会话")

    def capture(self, region: tuple[int, int, int, int] | None = None):
        from PIL import Image

        x, y, w, h = region or self.screen_bounds()
        handle, path = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        try:
            errors: list[str] = []
            # 1) mss —— 最省事, X11/Wayland 都能用
            try:
                import mss  # type: ignore
                from mss.tools import to_png  # type: ignore

                with mss.mss() as sct:
                    shot = sct.grab({"left": int(x), "top": int(y),
                                     "width": int(w), "height": int(h)})
                    to_png(shot.rgb, shot.size, output=path)
                with Image.open(path) as img:
                    return img.convert("RGB").copy()
            except ImportError:
                errors.append("没装 mss")
            except Exception as exc:
                errors.append(f"mss 失败: {exc}")

            # 2) grim (Wayland 原生)
            if shutil.which("grim"):
                code, _o, err = _run(["grim", "-g", f"{int(x)},{int(y)} {int(w)}x{int(h)}", path])
                if code == 0 and os.path.getsize(path):
                    with Image.open(path) as img:
                        return img.convert("RGB").copy()
                errors.append(f"grim 失败: {err.strip()}")

            # 3) ImageMagick
            for tool in (["import"], ["magick", "import"]):
                if shutil.which(tool[0]) or (len(tool) > 1 and shutil.which(tool[1])):
                    code, _o, err = _run(
                        tool + ["-window", "root", "-crop",
                                f"{int(w)}x{int(h)}+{int(x)}+{int(y)}", "+repage", path])
                    if code == 0 and os.path.getsize(path):
                        with Image.open(path) as img:
                            return img.convert("RGB").copy()
                    errors.append(f"{' '.join(tool)} 失败: {err.strip()}")

            # 4) scrot
            if shutil.which("scrot"):
                code, _o, err = _run(
                    ["scrot", "-a", f"{int(x)},{int(y)},{int(w)},{int(h)}", path])
                if code == 0 and os.path.getsize(path):
                    with Image.open(path) as img:
                        return img.convert("RGB").copy()
                errors.append(f"scrot 失败: {err.strip()}")

            raise BackendError(
                "截图失败, 没有任何可用工具。装一个:\n"
                "  pip install mss            (推荐)\n"
                "  sudo apt install grim      (Wayland)\n"
                "  sudo apt install imagemagick 或 scrot\n"
                "已尝试: " + "; ".join(errors)
            )
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # ---- 输入 ----------------------------------------------------------

    def move(self, x: int, y: int) -> None:
        self.x11().move(x, y)

    def button(self, button: str, down: bool) -> None:
        number = _BUTTON_NUMBERS.get(button)
        if number is None:
            raise BackendError(f"未知鼠标键: {button!r} (支持 left/right/middle)")
        self.x11().button(number, down)

    def key(self, name: str, down: bool) -> None:
        self.x11().key(_KEYSYMS[normalize_key(name)], down)

    # ---- 窗口 ----------------------------------------------------------

    def windows(self) -> list[WindowInfo]:
        if shutil.which("wmctrl"):
            code, out, err = _run(["wmctrl", "-lG"])
            if code != 0:
                raise BackendError(f"wmctrl 失败: {err.strip()}")
            found: list[WindowInfo] = []
            for line in out.splitlines():
                parts = line.split(None, 7)
                if len(parts) < 8:
                    continue
                handle, _desktop, x, y, w, h, _host, title = parts
                try:
                    width, height = int(w), int(h)
                except ValueError:
                    continue
                if width <= 1 or height <= 1:
                    continue
                found.append(WindowInfo(
                    title=title.strip(), app="", handle=int(handle, 16),
                    x=int(x), y=int(y), width=width, height=height,
                ))
            return found
        if shutil.which("xdotool"):
            code, out, err = _run(["xdotool", "search", "--onlyvisible", "--name", "."])
            if code != 0:
                raise BackendError(f"xdotool 失败: {err.strip()}")
            found = []
            for token in out.split():
                if not token.isdigit():
                    continue
                _c, name, _e = _run(["xdotool", "getwindowname", token])
                _c2, geo, _e2 = _run(["xdotool", "getwindowgeometry", "--shell", token])
                values = dict(
                    line.split("=", 1) for line in geo.splitlines() if "=" in line
                )
                try:
                    found.append(WindowInfo(
                        title=name.strip(), app="", handle=int(token),
                        x=int(values.get("X", 0)), y=int(values.get("Y", 0)),
                        width=int(values.get("WIDTH", 0)), height=int(values.get("HEIGHT", 0)),
                    ))
                except ValueError:
                    continue
            return [w for w in found if w.width > 1 and w.height > 1]
        raise BackendError(
            "列窗口需要 wmctrl 或 xdotool: sudo apt install wmctrl xdotool"
        )

    def focus(self, handle) -> bool:
        if shutil.which("wmctrl"):
            code, _out, _err = _run(["wmctrl", "-i", "-a", hex(int(handle))])
            return code == 0
        if shutil.which("xdotool"):
            code, _out, _err = _run(["xdotool", "windowactivate", str(int(handle))])
            return code == 0
        raise BackendError("切前台需要 wmctrl 或 xdotool")

    # ---- 元信息 --------------------------------------------------------

    def capabilities(self) -> Capabilities:
        notes: list[str] = []
        input_ok = not self.wayland
        if self.wayland:
            notes.append("Wayland 会话: 不能注入鼠标/键盘, 只有截图可用")
        else:
            try:
                self.x11()
            except BackendError as exc:
                input_ok = False
                notes.append(str(exc).splitlines()[0])
        return Capabilities(capture=True, input_mouse=input_ok,
                            input_keys=input_ok,
                            windows=bool(shutil.which("wmctrl") or shutil.which("xdotool")),
                            notes=notes)

    def describe(self) -> dict:
        info = super().describe()
        info["session"] = "wayland" if self.wayland else "x11"
        return info


def _run(argv: list[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendError(f"执行 {argv[0]} 失败: {exc}") from exc
    return done.returncode, done.stdout, done.stderr