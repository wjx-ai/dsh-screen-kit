"""发票自助打印 (控制台版) —— 扫码就出纸。

屏幕版式 (固定三行, 原地刷新, 不刷屏):

    一共有 5 张发票
    正在打印第 2 张发票...

    一共有 5 张发票
    5 张发票已打印完成，请取走发票
    ⏳ 4 秒后清屏

    请扫描收费单上的发票二维码

用 VT 转义序列原地重绘, 所以屏幕不会滚动、不会堆积历史信息。
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
BUFFER_COLS, BUFFER_ROWS = 50, 18
TITLE = '发票自助打印  (关闭本窗口即退出)'
PROMPT = '请扫描收费单上的发票二维码'
COUNTDOWN = 5

_scan_queue: queue.Queue = queue.Queue()
_stop = threading.Event()
_lock = threading.Lock()
_last_scan = {'text': '', 'at': 0.0}
_clear_gen = [0]
_batch = {'active': False, 'done': 0, 'total': 0}


def enable_vt() -> None:
    """打开 VT 转义支持, 否则 \x1b[2K 这些不生效。"""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def setup_console() -> None:
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    try:
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.SetWindowTextW(hwnd, TITLE)
    except Exception:
        pass

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
    notes = []

    # 必须先缩小窗口/缓冲区: 否则 32px 字号会让窗口超出屏幕, Windows 直接拒绝改字号
    try:
        ws = wt.SMALL_RECT()
        ws.Left, ws.Top = 0, 0
        ws.Right, ws.Bottom = BUFFER_COLS - 1, BUFFER_ROWS - 1
        kernel32.SetConsoleWindowInfo(handle, True, ctypes.byref(ws))
        kernel32.SetConsoleScreenBufferSize(handle, COORD(BUFFER_COLS, BUFFER_ROWS))
        notes.append('resize=ok')
    except Exception as exc:
        notes.append('resize=%s' % exc)

    before = 0
    ok = False
    face_used = ''
    for face in ('Microsoft YaHei', 'SimSun', 'Consolas'):
        trial = CONSOLE_FONT_INFOEX()
        trial.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
        kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(trial))
        before = trial.dwFontSize.Y
        trial.FaceName = face
        trial.dwFontSize.X = 0
        trial.dwFontSize.Y = FONT_SIZE
        res = kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(trial))
        check = CONSOLE_FONT_INFOEX()
        check.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
        kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(check))
        notes.append('%s:res=%s,size=%s' % (face, bool(res), check.dwFontSize.Y))
        if res and check.dwFontSize.Y == FONT_SIZE:
            ok = True
            face_used = face
            break

    try:
        log_path = os.path.join(os.environ.get('TEMP', '.'), 'kiosk_font.log')
        with open(log_path, 'a', encoding='utf-8') as fh:
            fh.write('before=%s wanted=%s ok=%s face=%s wt=%s | %s\n' % (
                before, FONT_SIZE, ok, face_used,
                bool(os.environ.get('WT_SESSION')), '; '.join(notes)))
    except Exception:
        pass


class Screen:
    """原地重绘一个文字块, 不滚动、不堆积。"""

    def __init__(self) -> None:
        self._lines = 0
        self._text: tuple = ()

    def draw(self, lines) -> None:
        lines = tuple(lines)
        if lines == self._text:
            return
        with _lock:
            out = ['\r\x1b[2K']
            for _ in range(self._lines - 1):
                out.append('\x1b[1A\r\x1b[2K')
            out.append('\n'.join('  ' + ln for ln in lines))
            sys.stdout.write(''.join(out))
            sys.stdout.flush()
            self._lines = len(lines)
            self._text = lines

    def clear_to(self, lines) -> None:
        """清屏后重新画。"""
        with _lock:
            os.system('cls')
            sys.stdout.write('\n' + '\n'.join('  ' + ln for ln in lines))
            sys.stdout.flush()
            self._lines = len(lines)
            self._text = tuple(lines)


SCREEN = Screen()


def header() -> str:
    total = _batch['total']
    if not total:
        return ''
    return '一共有 %d 张发票' % total


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
        return
    _last_scan['text'] = text
    _last_scan['at'] = now
    _clear_gen[0] += 1
    with _lock:
        if not _batch['active']:
            _batch['active'] = True
            _batch['done'] = 0
            _batch['total'] = 0
        _batch['total'] += 1
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


def countdown(lines_builder, seconds: int = COUNTDOWN) -> bool:
    """倒计时, 沙漏翻转 + 沙子进度条。

    每 0.25 秒一帧: 沙漏在 ⏳/⌛ 之间来回翻, 进度条按剩余时间缩短,
    这样屏幕上一直有东西在动, 人知道机器还在做事。
    """
    _clear_gen[0] += 1
    gen = _clear_gen[0]
    steps = int(seconds / 0.25)
    for step in range(steps, 0, -1):
        left = step * 0.25
        glyph = '⏳' if int(left * 4) % 2 == 0 else '⌛'
        remaining = max(1, int(round(left)))     # 别显示 0 秒, 最后一帧就是 1
        bar = '█' * remaining + '░' * max(0, seconds - remaining)
        SCREEN.draw(lines_builder('  %s %d 秒后清屏  %s' % (glyph, remaining, bar)))
        time.sleep(0.25)
        if _clear_gen[0] != gen or not _scan_queue.empty():
            return False
    SCREEN.clear_to([PROMPT, ''])
    return True


def worker(printer: str) -> None:
    while not _stop.is_set():
        item = _scan_queue.get()
        if item is None:
            break

        with _lock:
            index = _batch['done'] + 1
            total_now = _batch['total']

        known = sk.cached_id(item)
        if known:
            dup, when = sk.already_printed(known)
            if dup:
                def dup_lines(_left=None):
                    return [header(), '%s 已经打印过这张发票了。' % when]

                if _scan_queue.empty():
                    if countdown(lambda cd: dup_lines() + [cd], COUNTDOWN + 5):
                        with _lock:
                            _batch['active'] = False
                            _batch['done'] = 0
                            _batch['total'] = 0
                else:
                    SCREEN.draw(dup_lines())
                continue

        spinner = {'i': 0}

        def paint() -> None:
            dots = ['.', '..', '...'][spinner['i'] % 3]
            SCREEN.draw([header(), '正在打印第 %d 张发票%s' % (index, dots)])

        paint()

        # 点要动起来: 后台每 0.5 秒重绘一次, 顺带把"一共有N张"也刷新
        anim_stop = threading.Event()

        def animate() -> None:
            while not anim_stop.is_set():
                spinner['i'] += 1
                paint()
                anim_stop.wait(0.5)

        threading.Thread(target=animate, daemon=True).start()

        buf = []

        def collect(msg: str) -> None:
            buf.append(msg)

        ok = False
        try:
            ok = run_once(item, printer, False, log=collect)
        except Exception:
            buf.append('打印失败，请联系工作人员')
        finally:
            anim_stop.set()

        with _lock:
            if ok:
                _batch['done'] += 1
            finished = _batch['done']
            still = _batch['total'] - finished
            active_total = _batch['total']

        if not ok:
            last = (buf[-1] if buf else '') or '打印失败，请联系工作人员'
            fail_lines = [header()] + ['  ' + ln.strip()
                                       for ln in last.split('\n') if ln.strip()]
            if _scan_queue.empty():
                if countdown(lambda cd: fail_lines + [cd], COUNTDOWN + 5):
                    with _lock:
                        _batch['active'] = False
                        _batch['done'] = 0
                        _batch['total'] = 0
            else:
                SCREEN.draw(fail_lines)
            bring_console_front()
            continue

        if _scan_queue.empty():
            summary = '%d 张发票已打印完成，请取走发票' % active_total

            def done_lines(cd: str):
                return [header(), summary, cd]

            if countdown(done_lines, COUNTDOWN):
                with _lock:
                    _batch['active'] = False
                    _batch['done'] = 0
                    _batch['total'] = 0
            bring_console_front()


def main() -> int:
    enable_vt()
    setup_console()
    SCREEN.clear_to([PROMPT, ''])

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
