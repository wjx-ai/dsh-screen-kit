"""发票自助打印 (控制台版) —— 扫码就出纸。

设计要点:
  1. 收票和处理分开两个线程 —— 打印一张要十几秒, 这期间必须能继续收下一张。
     否则第二张扫码的字堆在控制台缓冲区里, 处理完一次读进来, 两张粘成一串。
  2. 界面只给患者看: 大字号, 一句话, 处理时有转动的省略号。
     技术细节 (停顿触发、Esc) 不上屏。
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

_scan_queue: queue.Queue = queue.Queue()
_stop = threading.Event()
_lock = threading.Lock()


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


class Spinner:
    """转动的省略号: 处理时有动静, 人不至于以为机器死了。"""

    def __init__(self, text: str = '正在打印'):
        self.text = text
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        frames = ['.', '..', '...']
        i = 0
        while not self._stop.is_set():
            with _lock:
                sys.stdout.write('\r  ' + self.text + frames[i % 3] + '    ')
                sys.stdout.flush()
            i += 1
            self._stop.wait(0.45)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        with _lock:
            sys.stdout.write('\r' + ' ' * 40 + '\r')
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
    pending = _scan_queue.qsize()
    _scan_queue.put(text)
    if pending:
        with _lock:
            # 先把自己那行擦掉, 否则会和转动的省略号抢同一行, 糊成一团
            sys.stdout.write('\r' + ' ' * 44 + '\r')
            sys.stdout.write('  已收到，前面还有 %d 张，请稍候\n' % pending)
            sys.stdout.flush()


def reader(idle: float = 0.8) -> None:
    """只管收: 每收到一段就塞进队列, 立刻回去继续收。

    这样打印一张的十几秒里, 后面扫的票都会被排队, 不会丢也不会粘连。
    """
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
    """只管处理: 从队列取一张, 打完再取下一张。"""
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
        except Exception as exc:
            buf.append('打印失败，请联系工作人员')
            collect('')
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
            print('  ' + PROMPT)
            print()
            sys.stdout.flush()
        if _scan_queue.qsize() == 0:
            bring_console_front()


def main() -> int:
    setup_console()
    with _lock:
        sys.stdout.write('\n  ' + PROMPT + '\n\n')
        sys.stdout.flush()

    threading.Thread(target=reader, daemon=True).start()
    worker_thread = threading.Thread(target=worker, args=(DEFAULT_PRINTER,), daemon=True)
    worker_thread.start()

    try:
        while not _stop.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        _stop.set()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
