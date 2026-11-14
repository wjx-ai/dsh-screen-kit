"""发票自助打印 —— 扫码枪扫一下就出一张 A5。

流程:
  扫码枪(键盘) -> 本窗口输入框收到短链 -> 用 Chrome 打开 ->
  页面上显示着 einvoice 地址 -> OCR 读出来 -> 直接下载 -> A5 打印

为什么中间要开浏览器: 短链是个 JS 应用, 发票地址要 JS 跑完才出现在页面上。
无头 Chrome 试过, 页面里的 socket.io 长连接让 --dump-dom 永远等不到加载完成。
所以开一个受控窗口, OCR 读, 打印完把它关掉。

用法:
    python tools/scan_kiosk.py            # 自助机界面
    python tools/scan_kiosk.py --test <扫码内容> --dry-run   # 不开界面, 只验流程
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import bridge  # noqa: E402
from print_invoice import DEFAULT_PRINTER, download, page_sizes, print_pdf  # noqa: E402
from read_invoice_url import find_chrome_window, read_invoice_url, read_page  # noqa: E402

CHROME = r'C:\Users\Administrator\AppData\Local\Google\Chrome\Application\chrome.exe'
WM_CLOSE = 0x0010


CODE_RE = re.compile(r'/s/([A-Za-z0-9]+)')
INVOICE_ID_RE = re.compile(r'/d/([0-9A-Za-z]+)')
HEX_ONLY = re.compile(r'[0-9a-f]')
CONFUSE = str.maketrans({'O': '0', 'o': '0', 'l': '1', 'I': '1', 'i': '1',
                         '|': '1', 'S': '5', 's': '5', 'B': '8', 'G': '6',
                         'Z': '2', 'z': '2', 'T': '7', 'Q': '0', 'D': '0'})


def normalize_scan(text: str) -> str | None:
    """把扫码枪读到的内容整理成能用的地址。

    实测扫码枪会把域名里的点吃掉: https://abcyuncn/s/5i5jr6d
    (不是 OCR 认错, 是输入本身少了那个点)。
    所以不信任域名: 只取末尾的短码, 自己拼回正确地址。
    """
    text = (text or '').strip()
    if not text:
        return None

    m = CODE_RE.search(text)
    if m:
        return 'https://abcyun.cn/s/' + m.group(1)

    m = INVOICE_ID_RE.search(text)
    if m:
        hex_id = ''.join(HEX_ONLY.findall(m.group(1).translate(CONFUSE).lower()))
        if len(hex_id) >= 15:
            return 'http://einvoice.jsczt.cn/d/' + hex_id

    if text.startswith('http'):
        return text
    return None


def force_foreground(root) -> None:
    """把这个窗口顶到系统最前面。

    只调 Tk 的 focus_force 不够: 扫码枪是键盘输入, 字符进的是
    *系统当前活动窗口*。窗口不在前台时, 扫出来的字全跑到别的程序里了。
    """
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(root.winfo_id())
        GA_ROOT = 2
        root_hwnd = user32.GetAncestor(hwnd, GA_ROOT)
        fg = user32.GetForegroundWindow()
        if fg == root_hwnd:
            return
        cur = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        attached = False
        if fg_thread and fg_thread != cur:
            attached = bool(user32.AttachThreadInput(fg_thread, cur, True))
        user32.BringWindowToTop(root_hwnd)
        ok = bool(user32.SetForegroundWindow(root_hwnd))
        # 注意: 这里绝对不能调 SetFocus(root_hwnd) —— 那会把 Tk 的内部输入焦点
        # 从 Entry 抢到窗口本身, 于是扫码枪的字符进不了输入框,
        # 用户必须点一下鼠标才恢复。这个坑真踩过。
        if attached:
            user32.AttachThreadInput(fg_thread, cur, False)
        after = user32.GetForegroundWindow()
        try:
            import time as _t
            with open(os.path.join(os.environ.get('TEMP', '.'), 'focus.log'), 'a',
                      encoding='utf-8') as fh:
                fh.write('%s root=%s fg_before=%s ok=%s fg_after=%s attached=%s\n' % (
                    _t.strftime('%H:%M:%S'), root_hwnd, fg, ok, after, attached))
        except Exception:
            pass
    except Exception as exc:
        try:
            with open(os.path.join(os.environ.get('TEMP', '.'), 'focus.log'), 'a',
                      encoding='utf-8') as fh:
                fh.write('ERROR %s\n' % exc)
        except Exception:
            pass


def close_window(handle: int) -> None:
    """关掉我们开的那个浏览器窗口, 不留残页。"""
    try:
        ctypes.windll.user32.PostMessageW(int(handle), WM_CLOSE, 0, 0)
    except Exception:
        pass


# 页面上出现这些字样, 说明这张打不了 —— 不能干等, 也不能自作主张去开票。
# 业务规则: 开票只能在院内网做, 自助机只负责打印。
# 所以一律引导患者去收费处, 页面上那个「去开票」按钮我们绝不点。
NOT_INVOICED = {
    '未开票': '这张发票还没有开具，请先到医院收费处开票，开好后再来打印',
    '待开票': '这张发票还没有开具，请先到医院收费处开票，开好后再来打印',
    '未开具': '这张发票还没有开具，请先到医院收费处开票，开好后再来打印',
    '已作废': '这张发票已作废，请联系医院收费处处理',
    '已红冲': '这张发票已冲销，请联系医院收费处处理',
    '暂无':   '查不到这张发票，请联系医院收费处',
    '未找到': '查不到这张发票，请联系医院收费处',
    '不存在': '查不到这张发票，请联系医院收费处',
}


INVOICE_URL_RE = re.compile(r'einvoice\.jsczt\.cn/d/([0-9A-Za-z]+)')


def _clean_id(raw: str) -> str:
    return ''.join(HEX_ONLY.findall((raw or '').translate(CONFUSE).lower()))


def resolve_short_link(short_url: str, timeout: float = 30.0):
    """短链是服务端 302, 直接读 Location 就够了, 不需要浏览器。"""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(short_url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        opener.open(req, timeout=timeout)
        return short_url
    except urllib.error.HTTPError as exc:
        return exc.headers.get('Location') or short_url
    except Exception:
        return short_url


def _dump_dom(target: str, chrome_timeout: int, timeout: float) -> str:
    # 每次都用全新的临时配置目录。
    # 复用同一个目录会出问题: 实测某个时刻之后它就一直卡在"请稍等"的壳页面,
    # 换全新目录立刻正常。Chrome 的 profile 状态没人能保证干净, 别指望它。
    profile = os.path.join(tempfile.gettempdir(),
                           'kiosk-chrome-%d' % os.getpid())
    shutil.rmtree(profile, ignore_errors=True)
    try:
        cmd = [CHROME, '--headless=old', '--disable-gpu', '--dump-dom',
               '--no-first-run', '--no-default-browser-check',
               '--user-data-dir=' + profile,
               '--timeout=%d' % chrome_timeout, target]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return proc.stdout.decode('utf-8', 'replace')
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def resolve_headless(target: str, timeout: float = 90.0, attempts: int = 3):
    """用无头 Chrome 渲染页面, 从导出的 DOM 里找发票地址。

    为什么是无头: 用户不希望打印时有个浏览器弹到前台。
    为什么必须给 --timeout: 页面里有 socket.io 长连接, 不给超时 Chrome
    会一直等, 进程永远不退出 (实测卡死 60 秒)。

    为什么要重试: 导出时机不巧会拿到还没渲染完的壳页面
    (会看到 "doesn't work properly without JavaScript" 和 "请稍等")。
    那种情况下不能下"没开票"的结论, 必须再导一次。
    """
    last_text = ''
    for attempt in range(attempts):
        dom = _dump_dom(target, 20000, timeout)
        m = INVOICE_URL_RE.search(dom)
        if m:
            hex_id = _clean_id(m.group(1))
            if len(hex_id) >= 18:
                return 'http://einvoice.jsczt.cn/d/' + hex_id, dom
        body = re.sub(r'<script[\s\S]*?</script>', ' ', dom)
        body = re.sub(r'<style[\s\S]*?</style>', ' ', body)
        text = re.sub(r'<[^>]+>', ' ', body)
        last_text = re.sub(r'\s+', ' ', text)
        # 页面真的渲染出来了才会有这些字样; 都没有说明还是壳
        if '开票状态' in last_text or '医疗电子票据' in last_text:
            return None, last_text
    return None, last_text


def resolve_url(scanned: str, wait_limit: float = 60.0, log=print):
    """把扫码得到的东西变成发票 PDF 地址。

    返回 (地址 or None, 原因文字)。原因要能直接给人看:
    自助机前面站着患者, 不能只丢一句失败。
    """
    cannonical = normalize_scan(scanned)
    if not cannonical:
        log('  认不出这是什么: ' + scanned[:60])
        return None, '扫码内容认不出来'
    if cannonical != scanned.strip():
        log('  已整理为: ' + cannonical)
    scanned = cannonical
    if 'einvoice.jsczt.cn/d/' in scanned:
        return scanned, ''

    target = resolve_short_link(scanned)
    log('  页面: ' + target[:90])
    log('  后台解析中 ...')
    try:
        url, page = resolve_headless(target, timeout=wait_limit)
    except subprocess.TimeoutExpired:
        return None, '解析超时（网络太慢或系统卡住了）'
    except Exception as exc:
        log('  解析出错: ' + str(exc))
        return None, '解析出错，请联系工作人员'

    if url:
        return url, ''

    flat = page.replace(' ', '')
    for word, advice in NOT_INVOICED.items():
        if word in flat:
            log('  页面提示: ' + word)
            return None, advice
    return None, '页面里没有发票下载地址（可能还没开票，或网络有问题）'


# ---- 防重复打印 --------------------------------------------------------
#
# 患者很容易把同一张二维码扫两次 (手抖、以为没扫上)。不拦就会打两张,
# 既费纸又像机器出故障。策略:
#   1. 同一串扫码内容 3 秒内重复 -> 静默忽略 (在 scan_console 里做)
#   2. 同一张发票在窗口期内已打过 -> 明确提示, 不再打
#
# 记录落盘, 这样程序重启后仍然记得 (用过关掉再开重复打的情况)。

PRINT_STATE = os.path.expandvars(r'%LOCALAPPDATA%\dsh-screen-kit\printed_invoices.json')
DUPLICATE_WINDOW_MIN = 30


def _load_printed() -> dict:
    try:
        with open(PRINT_STATE, encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_printed(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(PRINT_STATE), exist_ok=True)
        cutoff = time.time() - 24 * 3600          # 只留一天, 文件不会越滚越大
        data = {k: v for k, v in data.items() if isinstance(v, (int, float)) and v > cutoff}
        with open(PRINT_STATE, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)
    except Exception:
        pass


SCAN_CACHE = os.path.expandvars(r'%LOCALAPPDATA%\dsh-screen-kit\scan_cache.json')


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _write_json(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cutoff = time.time() - 7 * 24 * 3600
        data = {k: v for k, v in data.items()
                if isinstance(v, dict) and v.get('at', 0) > cutoff}
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception:
        pass


def cached_id(scanned: str) -> str:
    """扫过的那串内容上次对应的发票 id (有就省掉十几秒解析)。"""
    entry = _read_json(SCAN_CACHE).get(scanned.strip())
    if isinstance(entry, dict):
        return str(entry.get('id') or '')
    return ''


def remember_id(scanned: str, invoice_id: str) -> None:
    data = _read_json(SCAN_CACHE)
    data[scanned.strip()] = {'id': invoice_id, 'at': time.time()}
    _write_json(SCAN_CACHE, data)


def already_printed(invoice_id: str):
    """这张打过吗? 返回 (是否打过, 上次时间文字)。"""
    data = _load_printed()
    ts = data.get(invoice_id)
    if not isinstance(ts, (int, float)):
        return False, ''
    if time.time() - ts > DUPLICATE_WINDOW_MIN * 60:
        return False, ''
    return True, time.strftime('%H:%M', time.localtime(ts))


def mark_printed(invoice_id: str) -> None:
    data = _load_printed()
    data[invoice_id] = time.time()
    _save_printed(data)


def printer_job_count(printer: str) -> int:
    """打印机队列里还剩几个任务; 出错返回 -1。"""
    winspool = ctypes.WinDLL('winspool.drv', use_last_error=True)
    winspool.OpenPrinterW.restype = wt_bool = ctypes.c_int
    handle = ctypes.c_void_p()
    if not winspool.OpenPrinterW(ctypes.c_wchar_p(printer), ctypes.byref(handle), None):
        return -1
    try:
        needed = ctypes.c_ulong(0)
        returned = ctypes.c_ulong(0)
        # 级别 1 拿 JOB_INFO_1, 只要数量所以先探大小
        winspool.EnumJobsW(handle, 0, 99, 1, None, 0,
                           ctypes.byref(needed), ctypes.byref(returned))
        if needed.value:
            buf = ctypes.create_string_buffer(needed.value)
            if winspool.EnumJobsW(handle, 0, 99, 1, buf, needed.value,
                                  ctypes.byref(needed), ctypes.byref(returned)):
                return int(returned.value)
            return -1
        return 0
    finally:
        winspool.ClosePrinter(handle)


def printer_is_printing(printer: str) -> bool:
    """打印机是不是还在动 (PRINTER_STATUS_PRINTING = 0x400)。"""
    winspool = ctypes.WinDLL('winspool.drv', use_last_error=True)

    class PRINTER_INFO_2(ctypes.Structure):
        _fields_ = [('pServerName', ctypes.c_wchar_p),
                    ('pPrinterName', ctypes.c_wchar_p),
                    ('pShareName', ctypes.c_wchar_p),
                    ('pPortName', ctypes.c_wchar_p),
                    ('pDriverName', ctypes.c_wchar_p),
                    ('pComment', ctypes.c_wchar_p),
                    ('pLocation', ctypes.c_wchar_p),
                    ('pDevMode', ctypes.c_void_p),
                    ('pSepFile', ctypes.c_wchar_p),
                    ('pPrintProcessor', ctypes.c_wchar_p),
                    ('pDatatype', ctypes.c_wchar_p),
                    ('pParameters', ctypes.c_wchar_p),
                    ('pSecurityDescriptor', ctypes.c_void_p),
                    ('Attributes', ctypes.c_ulong),
                    ('Priority', ctypes.c_ulong),
                    ('DefaultPriority', ctypes.c_ulong),
                    ('StartTime', ctypes.c_ulong),
                    ('UntilTime', ctypes.c_ulong),
                    ('Status', ctypes.c_ulong)]

    handle = ctypes.c_void_p()
    if not winspool.OpenPrinterW(ctypes.c_wchar_p(printer), ctypes.byref(handle), None):
        return False
    try:
        needed = ctypes.c_ulong(0)
        winspool.GetPrinterW(handle, 2, None, 0, ctypes.byref(needed))
        if not needed.value:
            return False
        buf = ctypes.create_string_buffer(needed.value)
        if not winspool.GetPrinterW(handle, 2, buf, needed.value, ctypes.byref(needed)):
            return False
        info = ctypes.cast(buf, ctypes.POINTER(PRINTER_INFO_2)).contents
        return bool(info.Status & 0x00000400)
    finally:
        winspool.ClosePrinter(handle)


def wait_for_printer(printer: str, timeout: float = 120.0, log=print) -> bool:
    """等到打印机队列清空并且不再打印, 才算真的出纸了。

    为什么必须等: 把任务交给驱动只是一瞬间的事, 后面还要排队、取纸、出纸。
    提前告诉患者"请取走发票", 人会伸手去掏一张还没出来的纸。
    """
    start = time.time()
    while time.time() - start < timeout:
        jobs = printer_job_count(printer)
        printing = printer_is_printing(printer)
        if jobs == 0 and not printing:
            return True
        if jobs < 0:
            log('  读不到打印队列, 按已完成处理')
            return True
        time.sleep(0.5)
    log('  等待打印机超时 (%d 秒), 可能卡纸或缺纸' % int(timeout))
    return False


def do_print(url: str, printer: str, dry_run: bool, log=print) -> bool:
    dest = os.path.join(os.environ.get('TEMP', '.'), 'kiosk_invoice.pdf')
    try:
        final, size = download(url, dest)
    except Exception as exc:
        log(f'  下载失败: {exc}')
        return False
    sizes = page_sizes(dest)
    log(f'  {size // 1024} KB, {len(sizes)} 页')
    try:
        log('  ' + print_pdf(dest, printer, dry_run))
    except Exception as exc:
        log(f'  打印失败: {exc}')
        return False
    if dry_run:
        return True
    return wait_for_printer(printer, log=log)


def run_once(scanned: str, printer: str, dry_run: bool, log=print,
             on_phase=None) -> bool:
    """处理一次扫码。on_phase('resolve'|'print') 用来切界面上的文案。"""
    def phase(name: str) -> None:
        if on_phase:
            try:
                on_phase(name)
            except Exception:
                pass

    log(f'收到: {scanned[:70]}')

    # 老票重扫: 缓存里有 id, 立刻就能判重, 不用再花十几秒去解析
    known = cached_id(scanned)
    if known:
        dup, when = already_printed(known)
        if dup:
            log(f'{when} 已经打印过这张发票了。')
            return False
        url = 'http://einvoice.jsczt.cn/d/' + known
        log(f'发票地址（缓存）: {url}')
    else:
        phase('resolve')
        url, reason = resolve_url(scanned, log=log)
        if not url:
            log(f'⚠ {reason}' if reason else '⚠ 没拿到发票地址')
            return False
        log(f'发票地址: {url}')
        remember_id(scanned, url.rsplit('/', 1)[-1])

    invoice_id = url.rsplit('/', 1)[-1]
    dup, when = already_printed(invoice_id)
    if dup:
        log(f'{when} 已经打印过这张发票了。')
        return False

    phase('print')
    ok = do_print(url, printer, dry_run, log=log)
    if ok and not dry_run:
        mark_printed(invoice_id)
    return ok



def gui(printer: str, dry_run: bool) -> int:
    import tkinter as tk
    from tkinter import font as tkfont

    root = tk.Tk()
    root.title('发票自助打印')
    root.geometry('760x420')
    root.attributes('-topmost', True)
    root.configure(bg='#1e1e1e')

    big = tkfont.Font(family='Microsoft YaHei', size=16)
    mid = tkfont.Font(family='Microsoft YaHei', size=11)

    tk.Label(root, text='请扫描发票二维码', font=big, fg='#ffffff', bg='#1e1e1e').pack(pady=(18, 8))
    entry = tk.Entry(root, font=big, justify='center', bg='#2d2d2d', fg='#00ff88',
                     insertbackground='#00ff88', relief='flat')
    entry.pack(fill='x', padx=26, ipady=10)
    entry.focus_set()

    status = tk.Text(root, height=11, font=mid, bg='#141414', fg='#cccccc',
                     relief='flat', state='disabled')
    status.pack(fill='both', expand=True, padx=26, pady=14)

    def log(msg: str) -> None:
        status.configure(state='normal')
        status.insert('end', msg + '\n')
        status.see('end')
        status.configure(state='disabled')

    busy = {'flag': False}

    def worker(scanned: str) -> None:
        # 处理期间必须让开: 自助机窗口是 topmost, 会盖住浏览器页面上
        # 显示发票地址的那一块, OCR 读到的全是自己的字 (真踩过)
        try:
            root.attributes('-topmost', False)
            root.withdraw()
        except Exception:
            pass
        try:
            ok = run_once(scanned, printer, dry_run, log=log)
        finally:
            try:
                root.deiconify()
                root.attributes('-topmost', True)
            except Exception:
                pass
        log('✅ 打印完成' if ok else '❌ 失败')
        log('')
        busy['flag'] = False
        entry.configure(state='normal')
        entry.delete(0, 'end')
        entry.focus_force()
        entry.focus_set()
        force_foreground(root)
        last_text['value'] = ''

    def on_scan(_event=None):
        if busy['flag']:
            return
        scanned = entry.get().strip()
        if not scanned:
            return
        busy['flag'] = True
        entry.configure(state='disabled')
        threading.Thread(target=worker, args=(scanned,), daemon=True).start()
        return 'break'

    # 扫码枪常常不发回车 —— 那就靠"输入停顿"自动触发。
    # 每 300ms 看一眼输入框, 内容非空且 0.8 秒没变化, 就当成扫完了。
    last_text = {'value': '', 'changed_at': 0.0}

    def watch_input():
        try:
            now = time.time()
            current = entry.get().strip()
            if current != last_text['value']:
                last_text['value'] = current
                last_text['changed_at'] = now
            elif (current and not busy['flag']
                  and now - last_text['changed_at'] > 0.8):
                on_scan()
        except Exception:
            pass
        root.after(300, watch_input)

    entry.bind('<Return>', on_scan)
    root.bind('<Escape>', lambda _e: root.destroy())
    root.after(300, watch_input)

    # 抢前台: 扫码枪的字符进的是系统活动窗口, 窗口不在前台就全丢别处了
    def keep_front():
        if not busy['flag']:
            force_foreground(root)
            # 抢完前台把输入焦点还给输入框, 否则字符进不来
            try:
                if root.focus_get() is not entry:
                    entry.focus_set()
            except Exception:
                pass
        root.after(2500, keep_front)

    root.after(300, keep_front)
    if dry_run:
        log('*** dry-run: 不会真的打印 ***')
    log('就绪。扫码枪扫一下即可，按 Esc 退出。')
    root.mainloop()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='发票自助打印')
    ap.add_argument('--printer', default=DEFAULT_PRINTER)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--test', metavar='SCANNED', help='不开界面, 直接跑一遍')
    args = ap.parse_args()

    if args.test:
        ok = run_once(args.test, args.printer, args.dry_run)
        return 0 if ok else 1
    return gui(args.printer, args.dry_run)


if __name__ == '__main__':
    raise SystemExit(main())