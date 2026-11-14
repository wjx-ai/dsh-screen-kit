"""发票自助打印 (控制台版) —— 扫码就出纸。

界面规则 (连扫多张时):
  正在打印第1张...      -> 换行不换屏, 同一行动态更新
  正在打印第2张...
  3张发票已打印完成，请取走发票。
  （5 秒后自动清空）     -> 倒计时
  请扫描收费单上的发票二维码

  打印期间继续扫码的, 自动排进本批, 编号接着走。
  队列空 = 本批结束, 报总数并倒计时清屏。
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

import scan_kiosk as sk  # noqa: E402
from scan_kiosk import DEFAULT_PRINTER, run_once  # noqa: E402

FONT_SIZE = 32
# 字号变大后一行放不下多少字, 缓冲区和窗口跟着调, 否则内容超出窗口
BUFFER_COLS, BUFFER_ROWS = 50, 18
TITLE = '发票自助打印  (关闭本窗口即退出)'
PROMPT = '请扫描收费单上的发票二维码'
COUNTDOWN = 5

_scan_queue: queue.Queue = queue.Queue()
_stop = threading.Event()
_lock = threading.Lock()
_last_scan = {'text': '', 'at': 0.0}
_clear_gen = [0]
_batch = {'active': False, 'done': 0}


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
        before = info.dwFontSize.Y
        # 点阵字体 (新宋体/Terminal) 只有固定像素尺寸, 设 40 会被静默忽略 ——
        # 实测返回成功但字号仍是 16。所以必须换 TrueType 中文字体。
        candidates = ['Microsoft YaHei', 'SimSun', 'Consolas']
        ok = False
        for face in candidates:
            trial = CONSOLE_FONT_INFOEX()
            trial.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
            kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(trial))
            trial.FaceName = face
            trial.FontFamily = 0
            trial.FontWeight = 400
            trial.dwFontSize.X = 0
            trial.dwFontSize.Y = FONT_SIZE
            res = kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(trial))
            check = CONSOLE_FONT_INFOEX()
            check.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
            kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(check))
            ok = bool(res) and check.dwFontSize.Y == FONT_SIZE
            if ok:
                break


        # 诊断: Windows Terminal 托管的控制台不认这套老 API, 字号改不动。
        # 把实际情况写下来, 免得每次都靠猜。
        after = CONSOLE_FONT_INFOEX()
        after.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
        kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(after))
        try:
            log_path = os.path.join(os.environ.get('TEMP', '.'), 'kiosk_font.log')
            with open(log_path, 'a', encoding='utf-8') as fh:
                fh.write('before=%s wanted=%s ok=%s after=%s wt=%s face=%s\n' % (
                    before, FONT_SIZE, bool(ok), after.dwFontSize.Y,
                    bool(os.environ.get('WT_SESSION')), after.FaceName))
        except Exception:
            pass
    except Exception:
        pass

    try:
        handle = kernel32.GetStdHandle(-11)

        class COORD2(ctypes.Structure):
            _fields_ = [('X', ctypes.c_short), ('Y', ctypes.c_short)]

        ws = wt.SMALL_RECT()
        ws.Left, ws.Top = 0, 0
        ws.Right, ws.Bottom = BUFFER_COLS - 1, BUFFER_ROWS - 1
        kernel32.SetConsoleScreenBufferSize(handle, COORD2(BUFFER_COLS, BUFFER_ROWS))
        kernel32.SetConsoleWindowInfo(handle, True, ctypes.byref(ws))
    except Exception:
        pass


def clear_screen() -> None:
    with _lock:
        os.system('cls')
        sys.stdout.write('\n  ' + PROMPT + '\n\n')
        sys.stdout.flush()


def countdown_and_clear(seconds: int = COUNTDOWN) -> bool:
    """倒计时后清屏。期间来了新票就取消, 返回 False。"""
    _clear_gen[0] += 1
    gen = _clear_gen[0]
    for left in range(seconds, 0, -1):
        with _lock:
            sys.stdout.write('\r  （%d 秒后自动清空）      ' % left)
            sys.stdout.flush()
        for _ in range(20):                       # 每 50ms 看一次有没有新票
            time.sleep(0.05)
            if _clear_gen[0] != gen or not _scan_queue.empty():
                with _lock:
                    sys.stdout.write('\r' + ' ' * 30 + '\r')
                    sys.stdout.flush()
                return False
    clear_screen()
    return True


class Spinner:
    """转动的省略号, 文案由调用方给 (正在打印第N张)。"""

    def __init__(self, text: str):
        self.text = text
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            dots = ['.', '..', '...'][i % 3]
            with _lock:
                sys.stdout.write('\r  ' + self.text + dots + '      ')
                sys.stdout.flush()
            i += 1
            self._stop.wait(0.5)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        with _lock:
            sys.stdout.write('\r' + ' ' * 46 + '\r')
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
        return                                  # 手抖连扫, 直接吃掉
    _last_scan['text'] = text
    _last_scan['at'] = now
    _clear_gen[0] += 1                          # 新票: 取消倒计时清屏
    with _lock:
        if not _batch['active']:                # 新一批开始
            _batch['active'] = True
            _batch['done'] = 0
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

        with _lock:
            index = _batch['done'] + 1

        # 老票重扫: 先判重, 不要先说"正在打印"再告诉人家打过了
        known = sk.cached_id(item)
        if known:
            dup, when = sk.already_printed(known)
            if dup:
                with _lock:
                    print()
                    print('  这张发票 %s 已经打印过一张了。' % when)
                    print('  如果确实需要再打一张，请联系收费处。')
                    print()
                    sys.stdout.flush()
                if _scan_queue.empty():
                    if countdown_and_clear(COUNTDOWN + 5):
                        with _lock:
                            _batch['active'] = False
                            _batch['done'] = 0
                continue

        spinner = Spinner('正在查询第%d张' % index)
        spinner.start()
        buf = []

        def collect(msg: str) -> None:
            buf.append(msg)

        def on_phase(name: str) -> None:
            spinner.text = ('正在打印第%d张' if name == 'print'
                            else '正在查询第%d张') % index

        ok = False
        try:
            ok = run_once(item, printer, False, log=collect, on_phase=on_phase)
        except Exception:
            buf.append('打印失败，请联系工作人员')
        finally:
            spinner.stop()

        with _lock:
            _batch['done'] += 1
            finished = _batch['done']

        with _lock:
            print()
            if not ok:
                last = (buf[-1] if buf else '') or '打印失败，请联系工作人员'
                for line in last.split('\n'):
                    if line.strip():
                        print('  ' + line.strip())
                print()
            sys.stdout.flush()

        if not ok:
            # 失败时不说"全部完成", 只把原因留着, 让人读完再清
            if _scan_queue.empty():
                if countdown_and_clear(COUNTDOWN + 5):
                    with _lock:
                        _batch['active'] = False
                        _batch['done'] = 0
                bring_console_front()
            continue

        if _scan_queue.empty():
            with _lock:
                print('  %d 张发票已打印完成，请取走发票。' % finished)
                print()
                sys.stdout.flush()
            if countdown_and_clear(COUNTDOWN):
                with _lock:
                    _batch['active'] = False
                    _batch['done'] = 0
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
