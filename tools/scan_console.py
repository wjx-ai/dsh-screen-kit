"""发票自助打印 (控制台版) —— 扫码就出纸。

为什么用控制台而不是 Tk 窗口:
  实测 Tk 窗口的输入焦点会被 force_foreground 里的 SetFocus 抢到窗口本身,
  扫码枪的字进不了输入框, 用户必须点一下鼠标才恢复。控制台没有这个问题,
  它就是一个普通的命令行窗口, 字符直接进来。

扫码枪可能不发回车, 所以用"停顿 0.8 秒"作为结束标志。
"""
from __future__ import annotations

import ctypes
import msvcrt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from scan_kiosk import DEFAULT_PRINTER, run_once  # noqa: E402


def bring_console_front() -> None:
    """把控制台窗口顶到前台, 尽量让扫码枪的字符落到这里。"""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return
        if user32.GetForegroundWindow() == hwnd:
            return
        cur = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        attached = False
        if fg_thread and fg_thread != cur:
            attached = bool(user32.AttachThreadInput(fg_thread, cur, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if attached:
            user32.AttachThreadInput(fg_thread, cur, False)
    except Exception:
        pass


def read_scans(on_scan, idle=0.8):
    """逐字符读键盘, 停顿 idle 秒就当扫完。"""
    buf = ''
    last = 0.0
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ('\x00', '\xe0'):
                msvcrt.getwch()
                continue
            if ch in ('\r', '\n'):
                if buf.strip():
                    on_scan(buf.strip())
                    buf = ''
                continue
            if ch == '\x1b':
                print('\n结束')
                return
            if ch == '\x08':
                if buf:
                    buf = buf[:-1]
                    sys.stdout.write('\b \b')
                    sys.stdout.flush()
                continue
            buf += ch
            last = time.time()
            sys.stdout.write(ch)
            sys.stdout.flush()
        elif buf and time.time() - last > idle:
            on_scan(buf.strip())
            buf = ''
        else:
            time.sleep(0.02)


def main() -> int:
    printer = DEFAULT_PRINTER
    print('=' * 60)
    print('  发票自助打印: 扫码即出纸 (A5)')
    print('  扫一张出一张; 不按回车也会在停顿 0.8 秒后自动开始')
    print('  按 Esc 退出')
    print('=' * 60)
    print()

    bring_console_front()

    def banner(text: str) -> None:
        print()
        print('=' * 60)
        print('  ' + text)
        print('=' * 60)

    def work(scanned: str) -> None:
        print()
        buf = []

        def collect(msg: str) -> None:
            buf.append(msg)

        ok = run_once(scanned, printer, False, log=collect)
        if not ok:
            # 失败原因要让人一眼看到: 站在机器前面的患者或收费员
            last = buf[-1] if buf else '打印失败'
            banner(last.lstrip('⚠ '))
        else:
            banner('✅ 打印完成，请取走发票')
        print()
        print('请扫下一张 ＞ ', end='', flush=True)
        bring_console_front()

    print('请扫下一张 ＞ ', end='', flush=True)
    read_scans(work)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())