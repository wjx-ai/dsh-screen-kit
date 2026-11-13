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
import os
import re
import subprocess
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


# 页面上出现这些字样, 说明票还没开 / 开不出来, 不要傻等
NOT_INVOICED = ('未开票', '待开票', '未开具', '暂无', '未找到', '不存在', '已作废', '已红冲')


def resolve_url(scanned: str, wait_limit: float = 24.0, log=print):
    """把扫码得到的东西变成发票 PDF 地址。

    返回 (地址 or None, 原因文字)。原因要能直接给人看:
    自助机前面站着患者, 不能只丢一句"失败"。
    """
    cannonical = normalize_scan(scanned)
    if not cannonical:
        log(f'  认不出这是什么: {scanned[:60]}')
        return None, '扫码内容认不出来'
    if cannonical != scanned.strip():
        log(f'  已整理为: {cannonical}')
    scanned = cannonical
    if 'einvoice.jsczt.cn/d/' in scanned:
        return scanned, ''

    log('  打开页面 ...')
    before = {int(w.handle) for w in bridge.backend().windows()}
    subprocess.Popen([CHROME, '--new-window', scanned], close_fds=True)

    start = time.time()
    deadline = start + wait_limit
    url = None

    def fresh_candidates():
        """本次新开的浏览器窗口, 非最小化的排前面。

        不能用 find_chrome_window() 直接取第一个: 用户自己开着的发票窗口,
        或者上一次没关掉的残留窗口, 都会排在前面 —— 结果 OCR 读的是旧页面,
        一直等到超时。这个坑真踩过。
        """
        out = []
        for w in bridge.backend().windows():
            d = w.as_dict()
            if int(d['handle']) in before:
                continue
            t = (d.get('title') or '')
            if ('view-invoice' in t or '查看发票' in t
                    or 'abcyun' in t or '发票' in t):
                out.append(d)
        out.sort(key=lambda d: d['rect'][0] <= -30000)
        return out

    while time.time() < deadline:
        time.sleep(1.5)
        cands = fresh_candidates()
        if not cands:
            continue
        win = cands[0]
        # 把浏览器顶到前台再 OCR —— 自助机窗口是 topmost, 不这么做会读到自己的字
        try:
            bridge.backend().focus(win['handle'])
            time.sleep(0.4)
        except Exception:
            pass
        url, page_text = read_page(win)
        if url:
            close_window(win['handle'])
            return url, ''
        # 页面上已经有内容但没有下载地址 —— 多半是票还没开, 不必再等
        flat = page_text.replace(' ', '')
        if flat:
            for word in NOT_INVOICED:
                if word in flat:
                    close_window(win['handle'])
                    log(f'  页面提示: {word}')
                    return None, f'这张票现在不能打印（页面显示「{word}」）'
            # 必须等到「发票下载」这一块渲染出来, 才能断定确实没有地址。
            # 只看抬头就下结论会误判: 页面是分步渲染的, 抬头先出来,
            # 下载地址晚一两帧 (实测因此把已开票的单错判成未开票)。
            if ('发票下载' in flat and 'einvoice' not in flat
                    and time.time() - start > 5.0):
                close_window(win['handle'])
                return None, '页面上没有发票下载地址 —— 这张票可能还没开'
    log('  等了 %.0f 秒没读到发票地址' % wait_limit)
    for d in fresh_candidates():          # 收尾: 把本次开的窗口都关掉, 不留残页
        close_window(d['handle'])
    return None, '页面一直没出现发票地址（网络或系统可能有问题）'


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
        return True
    except Exception as exc:
        log(f'  打印失败: {exc}')
        return False


def run_once(scanned: str, printer: str, dry_run: bool, log=print) -> bool:
    log(f'收到: {scanned[:70]}')
    url, reason = resolve_url(scanned, log=log)
    if not url:
        log(f'⚠ {reason}' if reason else '⚠ 没拿到发票地址')
        return False
    log(f'发票地址: {url}')
    return do_print(url, printer, dry_run, log=log)


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