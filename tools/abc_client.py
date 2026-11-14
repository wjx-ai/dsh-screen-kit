"""在 ABC 里按姓名搜索收费单, 并解析列表行。

为什么用剪贴板 + Ctrl+V 而不是 SendInput:
  实测 SendInput 注入键盘无效 (返回成功但字不进去), 而 keybd_event 有效。
  中文键码打不出来, 所以走剪贴板; 剪贴板必须用 ctypes 直接设,
  用 powershell Set-Clipboard 会弹出控制台抢走焦点, 粘不进去。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
import sys
import time

sys.path.insert(0, r'D:\dsh\dsh-screen-kit')
import bridge  # noqa: E402

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
user32.GetForegroundWindow.restype = ctypes.c_void_p

SEARCH_X, SEARCH_Y = 820, 80
LIST = '389,120,530,600'
NAME_RE = re.compile(r'^[\u4e00-\u9fa5]{2,4}$')
TIME_ONLY = re.compile(r'(\d{1,2}:\d{2})')
DATE_ONLY = re.compile(r'(\d{1,2}-\d{1,2}|\d{2}-\d{2})')


def set_clipboard(text: str) -> bool:
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    data = text.encode('utf-16-le') + b'\x00\x00'
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.1)
    else:
        return False
    try:
        user32.EmptyClipboard()
        user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(h))
    finally:
        user32.CloseClipboard()
    return True


def key(vk, up=False):
    user32.keybd_event(vk, 0, 2 if up else 0, 0)


def combo(*vks):
    for vk in vks:
        key(vk)
        time.sleep(0.04)
    for vk in reversed(vks):
        key(vk, up=True)
        time.sleep(0.04)


def focus_abc() -> bool:
    for w in bridge.backend().windows():
        if 'ABC' in (w.title or ''):
            try:
                bridge.backend().focus(w.handle)
            except Exception:
                pass
            time.sleep(0.4)
            return True
    return False


def search(name: str) -> None:
    set_clipboard(name)
    time.sleep(0.2)
    focus_abc()
    bridge.click_at(SEARCH_X, SEARCH_Y)   # 点搜索框
    time.sleep(0.6)
    combo(0x11, 0x41)                    # Ctrl+A 全选旧内容
    time.sleep(0.2)
    combo(0x11, 0x56)                    # Ctrl+V 粘贴
    time.sleep(0.6)
    key(0x0D); key(0x0D, up=True)        # Enter
    time.sleep(2.0)


def read_rows():
    """读列表: 返回 [{name, when, is_today, x, y}] 和顶部提示。"""
    image, ox, oy = bridge.grab(LIST)
    blocks = bridge.ocr_mod.ocr(image)
    head = ''
    rows = []
    for b in blocks:
        t = b.text.strip()
        if '搜索到' in t or '条信息' in t:
            head = t
            continue
        m_date = DATE_ONLY.search(t)
        m_time = TIME_ONLY.search(t)
        if m_date:
            kind, when = 'past', m_date.group(1)
        elif m_time:
            kind, when = 'today', m_time.group(1)
        else:
            continue
        # 同一行左侧的名字
        name = ''
        for b2 in blocks:
            t2 = b2.text.strip()
            if NAME_RE.match(t2) and abs((b2.y + b2.height // 2) - (b.y + b.height // 2)) < 16:
                name = t2
                break
        rows.append({'name': name, 'when': when, 'is_today': kind == 'today',
                     'x': b.x + ox + b.width // 2,
                     'y': b.y + oy + b.height // 2})
    return head, rows


if __name__ == '__main__':
    who = sys.argv[1] if len(sys.argv) > 1 else '许桂香'
    search(who)
    head, rows = read_rows()
    print('提示:', head)
    for r in rows:
        print('  %-6s %-6s %s  (%d,%d)' % (r['name'], r['when'],
                                           '今天' if r['is_today'] else '往日',
                                           r['x'], r['y']))
    print('今天共 %d 行' % sum(1 for r in rows if r['is_today']))
