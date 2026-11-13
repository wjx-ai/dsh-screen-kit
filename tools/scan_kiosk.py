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
from read_invoice_url import find_chrome_window, read_invoice_url  # noqa: E402

CHROME = r'C:\Users\Administrator\AppData\Local\Google\Chrome\Application\chrome.exe'
WM_CLOSE = 0x0010


def close_window(handle: int) -> None:
    """关掉我们开的那个浏览器窗口, 不留残页。"""
    try:
        ctypes.windll.user32.PostMessageW(int(handle), WM_CLOSE, 0, 0)
    except Exception:
        pass


def resolve_url(scanned: str, wait_limit: float = 30.0, log=print) -> str | None:
    """把扫码得到的东西变成发票 PDF 地址。"""
    scanned = scanned.strip()
    if 'einvoice.jsczt.cn/d/' in scanned:
        return scanned
    if not scanned.startswith('http'):
        log(f'  不是网址: {scanned[:60]}')
        return None

    log('  打开页面 ...')
    before = {int(w.handle) for w in bridge.backend().windows()}
    subprocess.Popen([CHROME, '--new-window', scanned], close_fds=True)

    deadline = time.time() + wait_limit
    url = None
    while time.time() < deadline:
        time.sleep(1.5)
        win = find_chrome_window()
        if not win or win['handle'] in before:
            continue
        url = read_invoice_url(win)
        if url:
            close_window(win['handle'])
            return url
    log('  等了 %.0f 秒没读到发票地址' % wait_limit)
    win = find_chrome_window()
    if win and win['handle'] not in before:
        close_window(win['handle'])
    return None


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
    url = resolve_url(scanned, log=log)
    if not url:
        log('失败: 没拿到发票地址')
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
        ok = run_once(scanned, printer, dry_run, log=log)
        log('✅ 打印完成' if ok else '❌ 失败')
        log('')
        busy['flag'] = False
        entry.configure(state='normal')
        entry.delete(0, 'end')
        entry.focus_set()

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

    entry.bind('<Return>', on_scan)
    root.bind('<Escape>', lambda _e: root.destroy())
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