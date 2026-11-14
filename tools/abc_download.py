"""从 ABC 取出发票并打印。

关键发现 (2026-11-14 实测):
  点 ABC 预览右上角的「下载」后, 浏览器会弹出一个下载提示, 提示里
  **文件名就是发票 id**, 例如 4730125ecbb1791be9 后面跟着「保留」。
  那个「保留」之所以出现, 是因为 ABC 给的是 http:// 链接, Chrome 判定为
  不安全下载。https:// 同样能取到 PDF, 所以根本不用点「保留」:

      点下载 -> 从提示里读出 id -> 自己用 https 下载 -> A5 打印

另外: 卡住的 .crdownload 文件其实是完整 PDF (开头 %PDF), Chrome 只是没改名。

用法:
    python tools/abc_download.py --current          # 用当前打开的预览
    python tools/abc_download.py --current --print  # 顺便打印
    python tools/abc_download.py --id 4730125ecbb178cbaa7 --print
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bridge  # noqa: E402
import print_invoice as P  # noqa: E402

ID_RE = re.compile(r'([0-9a-f]{17,24})')
PROMPT_REGION = '0,0,1000,320'      # 下载提示出现的位置 (左上)
DOWNLOAD_BTN_REGION = '500,90,1000,220'


def find_download_button():
    hits = [h for h in bridge.locate('下载', DOWNLOAD_BTN_REGION,
                                     exact=True, max_tier=1) if not h['fuzzy']]
    return hits[0] if hits else None


def grab_id_from_prompt(timeout: float = 12.0):
    """点「下载」, 然后从下载提示里把发票 id 读出来。

    提示会自动消失, 所以点完要快速轮询。
    """
    btn = find_download_button()
    if not btn:
        return None, '没找到「下载」按钮 (预览是不是没打开?)'
    bridge.click_at(btn['x'], btn['y'])

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.35)
        hits = [h for h in bridge.locate('保留', PROMPT_REGION,
                                         exact=True, max_tier=1) if not h['fuzzy']]
        for h in hits:
            m = ID_RE.search(h['block'].lower())
            if m:
                return m.group(1), ''
    return None, '下载提示里没读到发票 id'


def download_pdf(invoice_id: str, dest: str | None = None) -> tuple[str, int]:
    """用 https 直接下载, 不经过浏览器, 所以不会被「保留」拦住。"""
    url = 'https://einvoice.jsczt.cn/d/' + invoice_id
    dest = dest or os.path.join(os.environ.get('TEMP', '.'), 'abc_%s.pdf' % invoice_id)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if data[:4] != b'%PDF':
        raise ValueError('返回的不是 PDF (%s)' % resp.headers.get('Content-Type'))
    with open(dest, 'wb') as fh:
        fh.write(data)
    return dest, len(data)


HISTORY_DB = os.path.expandvars(
    r'%LOCALAPPDATA%\Google\Chrome\User Data\Default\History')


def latest_from_chrome_history(since_seconds: float = 600.0):
    """从 Chrome 下载历史里取最近一条发票 URL。

    为什么可靠: 下载提示里的文件名是截断的, 而 Chrome 的 downloads 表
    存着完整 URL (实测一位不差)。只读本地数据库, 不碰网络。

    注意: 只有**下载完成**的记录才会进这个表 —— 卡在"保留"的不算。
    """
    if not os.path.exists(HISTORY_DB):
        return None, '找不到 Chrome 历史数据库'
    copy = os.path.join(tempfile.gettempdir(), 'chrome_history_copy.db')
    try:
        if os.path.exists(copy):
            os.remove(copy)
        shutil.copyfile(HISTORY_DB, copy)      # 直接查会被锁, 复制一份
    except Exception as exc:
        return None, '复制数据库失败: %s' % exc

    con = sqlite3.connect(copy)
    try:
        rows = con.execute(
            "SELECT d.target_path, d.start_time, u.url "
            "FROM downloads d JOIN downloads_url_chains u ON u.id = d.id "
            "WHERE u.chain_index = 0 ORDER BY d.start_time DESC LIMIT 60"
        ).fetchall()
    except Exception as exc:
        con.close()
        return None, '查询失败: %s' % exc
    con.close()

    now = time.time()
    for path, start, url in rows:
        if 'einvoice.jsczt.cn/d/' not in (url or ''):
            continue
        when = (start / 1e6 - 11644473600) if start else 0
        if when and now - when > since_seconds:
            continue
        m = ID_RE.search((url or '').rsplit('/', 1)[-1].lower())
        if m:
            return m.group(1), ''
    return None, '最近没有新的发票下载记录'


def main() -> int:
    ap = argparse.ArgumentParser(description='从 ABC 取发票并打印')
    ap.add_argument('--current', action='store_true', help='用当前打开的发票预览')
    ap.add_argument('--from-history', action='store_true',
                    help='从 Chrome 下载历史里取最近一张发票的完整 id (最可靠)')
    ap.add_argument('--id', help='直接给发票 id')
    ap.add_argument('--print', dest='do_print', action='store_true', help='下载后打印')
    ap.add_argument('--printer', default=P.DEFAULT_PRINTER)
    args = ap.parse_args()

    if args.id:
        invoice_id = args.id
    elif args.from_history:
        invoice_id, why = latest_from_chrome_history()
        if not invoice_id:
            print('失败:', why)
            return 1
        print('历史里最新发票 id:', invoice_id)
    elif args.current:
        invoice_id, why = grab_id_from_prompt()
        if not invoice_id:
            print('失败:', why)
            return 1
        print('读到发票 id:', invoice_id)
    else:
        ap.error('需要 --current 或 --id')

    dest, size = download_pdf(invoice_id)
    print('下载:', dest, size // 1024, 'KB')
    print('页面(毫米):', P.page_sizes(dest))

    if args.do_print:
        print(P.print_pdf(dest, args.printer, False))
    else:
        print('(没打印, 加 --print 才打)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
