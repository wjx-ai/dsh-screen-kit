"""全局键盘钩子: 不管焦点在哪个窗口, 都能收到扫码枪的输入。

为什么需要: 扫码枪就是一个键盘, 字符进的是"系统当前活动窗口"。
Tk 窗口很难稳定占住前台, 用户必须点一下才能扫 —— 自助机不能这样。
低层键盘钩子(WH_KEYBOARD_LL)在按键进入任何窗口之前就能拿到, 与焦点无关。

用法:
    start_hook(callback)   # callback(扫到的字符串), 回车或停顿 400ms 触发
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_CAPITAL = 0x14

ULONG_PTR = ctypes.c_void_p


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wt.DWORD), ('scanCode', wt.DWORD), ('flags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ULONG_PTR)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wt.WPARAM, wt.LPARAM)

_buffer = []
_last_time = [0.0]
_callback = [None]
_lock = threading.Lock()
_idle_timer_started = [False]


# 虚拟键码 -> (无 shift, 有 shift)。
# 不用 ToUnicodeEx: 在低层钩子里拿不到正确的键盘状态, 实测大量字符变空。
# 扫码枪发的是 ASCII, 一张表就够。非 US 布局下会有偏差, 见文件头说明。
_VK_MAP = {
    0x20: (' ', ' '),
    0x30: ('0', ')'), 0x31: ('1', '!'), 0x32: ('2', '@'), 0x33: ('3', '#'),
    0x34: ('4', '$'), 0x35: ('5', '%'), 0x36: ('6', '^'), 0x37: ('7', '&'),
    0x38: ('8', '*'), 0x39: ('9', '('),
    0xBA: (';', ':'), 0xBB: ('=', '+'), 0xBC: (',', '<'), 0xBD: ('-', '_'),
    0xBE: ('.', '>'), 0xBF: ('/', '?'), 0xC0: ('`', '~'),
    0xDB: ('[', '{'), 0xDC: (chr(92), '|'), 0xDD: (']', '}'),
    0xDE: (chr(39), '"'),
}


def _char_for(vk: int) -> str:
    """把虚拟键码转成字符 (shift 状态从系统实时读)。"""
    shifted = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
    if 0x41 <= vk <= 0x5A:
        ch = chr(vk)
        return ch if shifted else ch.lower()
    pair = _VK_MAP.get(vk)
    if pair:
        return pair[1] if shifted else pair[0]
    return ''


def _emit() -> None:
    global _buffer
    with _lock:
        text = ''.join(_buffer).strip()
        _buffer = []
    if text and _callback[0]:
        try:
            _callback[0](text)
        except Exception:
            pass


def _watch_idle() -> None:
    """扫码枪如果没发回车, 靠停顿触发。"""
    while True:
        time.sleep(0.15)
        with _lock:
            if _buffer and time.time() - _last_time[0] > 0.45:
                need_emit = True
            else:
                need_emit = False
        if need_emit:
            _emit()


def _handler(nCode, wParam, lParam):
    if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
        info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = info.vkCode
        if vk == VK_RETURN:
            _emit()
        elif vk in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT, VK_CAPITAL):
            pass
        else:
            ch = _char_for(vk)
            if ch and ch.isprintable():
                with _lock:
                    _buffer.append(ch)
                    _last_time[0] = time.time()
    return user32.CallNextHookEx(0, nCode, wParam, lParam)


# 64 位下必须显式声明返回类型, 否则句柄被截断成 int,
# SetWindowsHookEx 会失败而不报错 (真踩过, INSTALLED 一直是 False)。
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wt.DWORD]
user32.CallNextHookEx.restype = ctypes.c_long
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.GetKeyboardLayout.restype = ctypes.c_void_p
user32.ToUnicodeEx.restype = ctypes.c_int
user32.ToUnicodeEx.argtypes = [wt.UINT, wt.UINT,
                               ctypes.POINTER(ctypes.c_ubyte),
                               wt.LPWSTR, ctypes.c_int, wt.UINT,
                               ctypes.c_void_p]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p,
                               wt.UINT, wt.UINT]

_proc = HOOKPROC(_handler)


INSTALLED = [False]


def _pump():
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _proc,
                                    kernel32.GetModuleHandleW(None), 0)
    INSTALLED[0] = bool(hook)
    if not hook:
        return
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def start_hook(callback) -> None:
    _callback[0] = callback
    threading.Thread(target=_pump, daemon=True).start()
    if not _idle_timer_started[0]:
        _idle_timer_started[0] = True
        threading.Thread(target=_watch_idle, daemon=True).start()