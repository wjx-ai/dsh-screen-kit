"""从 ABC「查看发票」页面上读出发票地址。

为什么不用无头 Chrome: 那个页面加载了 socket.io, 长连接让 --dump-dom 永远等不到
"加载完成", 实测 60 秒超时拿不到 DOM。所以走"受控窗口 + OCR"这条路。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bridge  # noqa: E402

URL_RE = re.compile(r"einvoice\.jsczt\.cn/d/([0-9A-Za-z]+)", re.I)

# OCR 常见的字母数字混淆。发票 id 是十六进制, 字母只可能是 a-f,
# 所以这些替换是安全的: 把可能认错的字掰回十六进制。
CONFUSE = str.maketrans({
    'O': '0', 'o': '0', 'Q': '0', 'D': '0',
    'l': '1', 'I': '1', 'i': '1', '|': '1', 'L': '1',
    'S': '5', 's': '5',
    'B': '8',
    'G': '6',
    'Z': '2', 'z': '2',
    'T': '7',
})
HEX_ONLY = re.compile(r'[0-9a-f]')


def find_chrome_window() -> dict | None:
    """找标题里带 view-invoice / 查看发票 的浏览器窗口。"""
    for win in bridge.backend().windows():
        title = win.title or ''
        if ('view-invoice' in title or '查看发票' in title
                or 'abcyun.cn' in title or 'abcyun' in title):
            return win.as_dict()
    return None


def read_invoice_url(win: dict) -> str | None:
    """OCR 那个窗口, 拼出 einvoice 地址。"""
    rect = win['rect']
    x, y, w, h = rect[0], rect[1], rect[2], rect[3]
    bx, by, sw, sh = bridge.backend().screen_bounds()
    region = ','.join(str(round(v)) for v in (
        (x - bx) / sw * 1000, (y - by) / sh * 1000,
        (x - bx + w) / sw * 1000, (y - by + h) / sh * 1000))
    image, ox, oy = bridge.grab(region)
    blocks = bridge.ocr_mod.ocr(image)
    flat = ''.join(b.text for b in blocks).replace(' ', '')
    m = URL_RE.search(flat)
    if not m:
        return None
    raw_id = m.group(1).translate(CONFUSE)
    hex_id = ''.join(HEX_ONLY.findall(raw_id.lower()))
    if len(hex_id) < 15:
        return None
    return f'http://einvoice.jsczt.cn/d/{hex_id}'


if __name__ == '__main__':
    win = find_chrome_window()
    if not win:
        print('没找到 view-invoice 窗口')
        raise SystemExit(1)
    print('窗口:', win['title'][:60], win['rect'])
    print('读出地址:', read_invoice_url(win))