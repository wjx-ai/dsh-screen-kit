"""发票自助打印 (控制台版) —— 扫码就出纸。

界面规则:
  * 屏幕上永远只有两三行, 大字号
  * 队列里有几张, 并到"正在打印..."那一行里, 不额外刷屏
  * 全部打完就清屏, 只留下"请扫描发票上的二维码"
    —— 上一个人看到的提示, 对下一个人是噪音
  * 失败信息多留一会儿 (要能读完), 成功留短一点
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import msvcrt
import os
import queue
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from scan_kiosk import DEFAULT_PRINTER, run_once  # noqa: E402

FONT_SIZE = 20
TITLE = '发票自助打印  (关闭本窗口即退出)'
PROMPT = '请扫描发票上的二维码'
KEEP_OK = 4.0        # 成功提示停留几秒后清屏
KEEP_FAIL = 12.0     # 失败提示要让人读完, 留久一点

_scan_queue: queue.Queue = queue.Queue()
_stop = threading.Event()
_lock = threading.Lock()
_last_scan = {'text': '', 'at': 0.0}
_clear_gen = [0]                 # 清屏的"代号", 来了新票就作废上一次的清屏


def setup_console() -> None:
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    try:
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.SetWindowTextW(hwnd, TITLE)
    except Exception:
        pass

    try:
        class COORD(ctypes.Structure):
            _fields_ = [('X', ctypes.c_short), ('Y', ctypes.c_short)]

        class CONSOLE_FONT_INFOEX(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_ulong),
                        ('nFont', ctypes.c_ulong),
                        ('dwFontSize', COORD),
                        ('FontFamily', ctypes.c_uint),
                        ('FontWeight', ctypes.c_uint),
                        ('FaceName', ctypes.c_wchar * 32)]

        handle = kernel32.GetStdHandle(-11)
        info = CONSOLE_FONT_INFOEX()
        info.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
        kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(info))
        info.dwFontSize.X = 0
        info.dwFontSize.Y = FONT_SIZE
        kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(info))

        kernel32.SetConsoleScreenBufferSize(handle, COORD(100, 30))
        box = wt.SMALL_RECT()
        box.Left, box.Top = 0, 0
        box.Right, box.Bottom = 99, 29
        kernel32.SetConsoleWindowInfo(handle, True, ctypes.byref(box))
    except Exception:
        pass


def clear_screen() -> None:
    with _lock:
        os.system('cls')
        sys.stdout.write('\n  ' + PROMPT + '\n\n')
        sys.stdout.flush()


def schedule_clear(delay: float) -> None:
    """延时清屏; 期间来了新票就作废。"""
    _clear_gen[0] += 1
    gen = _clear_gen[0]

    def run() -> None:
        time.sleep(delay)
        if _clear_gen[0] != gen or not _scan_queue.empty():
            return
        clear_screen()

    threading.Thread(target=run, daemon=True).start()


class Spinner:
    """转动的省略号; 排队张数并在这同一行里, 不另起一行刷屏。"""

    def __init__(self, text: str = '正在打印'):
        self.text = text
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _line(self, i: int) -> str:
        dots = ['.', '..', '...'][i % 3]
        pending = _scan_queue.qsize()
        tail = '   (还有 %d 张)' % pending if pending else ''
        return '\r  ' + self.text + dots + '        ' + tail

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            with _lock:
                sys.stdout.write(self._line(i) + '    ')
                sys.stdout.flush()
            i += 1
            self._stop.wait(0.45)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        with _lock:
            sys.stdout.write('\r' + ' ' * 60 + '\r')
            sys.stdout.flush()


def bring_console_front() -> None:
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


def _submit(text: str) -> None:
    text = text.strip()
    if not text:
        return
    now = time.time()
    if text == _last_scan['text'] and now - _last_scan['at'] < 3.0:
        return                        # 手抖连扫, 直接吃掉
    _last_scan['text'] = text
    _last_scan['at'] = now
    _clear_gen[0] += 1                # 有新票: 取消之前排队的清屏
    _scan_queue.put(text)


def reader(idle: float = 0.8) -> None:
    buf = ''
    last = 0.0
    while not _stop.is_set():
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ('\x00', '\xe0'):
                msvcrt.getwch()
                continue
            if ch in ('\r', '\n'):
                _submit(buf)
                buf = ''
                continue
            if ch == '\x1b':
                _stop.set()
                break
            if ch == '\x08':
                buf = buf[:-1]
                continue
            buf += ch
            last = time.time()
        elif buf and time.time() - last > idle:
            _submit(buf)
            buf = ''
        else:
            time.sleep(0.02)
    _scan_queue.put(None)


def worker(printer: str) -> None:
    while not _stop.is_set():
        item = _scan_queue.get()
        if item is None:
            break
        spinner = Spinner('正在打印')
        spinner.start()
        buf = []

        def collect(msg: str) -> None:
            buf.append(msg)

        ok = False
        try:
            ok = run_once(item, printer, False, log=collect)
        except Exception:
            buf.append('打印失败，请联系工作人员')
        finally:
            spinner.stop()

        with _lock:
            print()
            if ok:
                print('  打印完成，请取走发票')
            else:
                last = (buf[-1] if buf else '') or '打印失败，请联系工作人员'
                for line in last.split('\n'):
                    if line.strip():
                        print('  ' + line.strip())
            print()
            sys.stdout.flush()

        if _scan_queue.empty():
            # 队列空了: 过一会儿清屏, 只留提示。
            # 成功留短一点, 失败留久一点 —— 读不完的提示等于没提示。
            schedule_clear(KEEP_OK if ok else KEEP_FAIL)
            bring_console_front()


def main() -> int:
    setup_console()
    clear_screen()

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=worker, args=(DEFAULT_PRINTER,), daemon=True).start()

    try:
        while not _stop.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        _stop.set()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
