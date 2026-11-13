"""扫码枪扫发票二维码 -> 下载 PDF -> A5 打印。

为什么不用浏览器: einvoice.jsczt.cn/d/<id> 直接返回 application/pdf。
浏览器那条路(扫码 -> 短链落地页 -> JS 跳转)只是"拿到 id"的手段。

为什么用 SumatraPDF 而不是 Foxit:
  Foxit 的 /t 用自己记住的纸张(A4), 实测打出来 A4 排版 -> A5 纸两边被裁。
  SumatraPDF 支持 -print-settings "paper=A5,fit", 纸张和缩放都按任务指定,
  实测 A5 出纸内容完整。

用法:
    python print_invoice.py --once <URL>          # 处理一个地址
    python print_invoice.py --once <URL> --dry-run  # 只下载, 不打印
    python print_invoice.py --from-screen          # 从浏览器地址栏 OCR 取地址
    python print_invoice.py                        # 读标准输入, 扫码枪逐行扫
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

SUMATRA_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\SumatraPDF\SumatraPDF.exe"),
    os.path.expandvars(r"%ProgramFiles%\SumatraPDF\SumatraPDF.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\SumatraPDF\SumatraPDF.exe"),
]
FOXIT = r"E:\soft\Foxit Reader.exe"
DEFAULT_PRINTER = "HP LaserJet Professional M1136 MFP"


def find_sumatra() -> str | None:
    for path in SUMATRA_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def page_sizes(pdf_path: str):
    """从 PDF 里读 MediaBox 换算成毫米。不依赖任何 PDF 库。"""
    raw = open(pdf_path, "rb").read()
    boxes = re.findall(
        rb"/MediaBox\s*\[\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)", raw)
    out = []
    for b in boxes:
        x0, y0, x1, y1 = (float(v) for v in b)
        out.append((round((x1 - x0) * 25.4 / 72), round((y1 - y0) * 25.4 / 72)))
    return out


def download(url: str, dest: str) -> tuple[str, int]:
    """下载并跟随跳转。返回 (最终地址, 字节数)。不是 PDF 就报错 ——
    这一步同时起到"地址没识别错"的校验作用。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        ctype = resp.headers.get("Content-Type", "")
        data = resp.read()
        final = resp.geturl()
    if not data.startswith(b"%PDF"):
        raise ValueError(f"不是 PDF (Content-Type={ctype}, {len(data)} 字节), 可能地址不对")
    with open(dest, "wb") as fh:
        fh.write(data)
    return final, len(data)


def print_pdf(pdf_path: str, printer: str, dry_run: bool) -> str:
    """A5 + 适合页面。返回实际用的打印方式, 便于核对。"""
    if dry_run:
        return "dry-run (没有打印)"
    sumatra = find_sumatra()
    if sumatra:
        subprocess.run(
            [sumatra, "-print-to", printer,
             "-print-settings", "paper=A5,fit", pdf_path],
            check=False, timeout=180)
        return "SumatraPDF (paper=A5,fit)"
    if os.path.exists(FOXIT):
        # 退路: Foxit 用自己记住的纸张, 可能是 A4 -> A5 纸上会被裁边。
        subprocess.run([FOXIT, "/t", pdf_path, printer], check=False, timeout=180)
        return "Foxit (警告: 纸张和缩放不受控, 可能裁边)"
    raise FileNotFoundError("没找到 SumatraPDF 也没找到 Foxit")


URL_PATTERNS = [
    r"(?:https?://)?(einvoice\.[a-z0-9.\-]+/d/[0-9a-zA-Z]+)",
    r"(?:https?://)?(abcyun\.[a-z0-9.\-]+/s/[0-9a-zA-Z]+)",
]


def url_from_screen() -> str | None:
    """从屏幕顶部(浏览器地址栏)读出发票地址。

    OCR 会把 URL 读成带空格的碎片, 所以先去掉所有空格再匹配。
    这条路径有识别风险, 但后面的 download() 会校验返回的是不是 PDF。
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import bridge  # noqa: E402

    image, _ox, _oy = bridge.grab("0,0,1000,90")
    blocks = bridge.ocr_mod.ocr(image)
    flat = "".join(b.text for b in blocks).replace(" ", "")
    for pattern in URL_PATTERNS:
        m = re.search(pattern, flat, re.I)
        if m:
            found = m.group(1)
            return found if found.startswith("http") else "https://" + found
    return None


def handle(url: str, printer: str, dry_run: bool) -> bool:
    url = (url or "").strip()
    if not url or not url.startswith("http"):
        print("  跳过: 不是网址", flush=True)
        return True
    dest = os.path.join(tempfile.gettempdir(), "invoice_scan.pdf")
    try:
        final, size = download(url, dest)
    except Exception as exc:
        print(f"  下载失败: {exc}", flush=True)
        return False
    sizes = page_sizes(dest)
    print(f"  {size // 1024} KB, {len(sizes)} 页, 页面(毫米): {sizes}", flush=True)
    print(f"  最终地址: {final}", flush=True)
    try:
        how = print_pdf(dest, printer, dry_run)
        print(f"  {how}", flush=True)
        return True
    except Exception as exc:
        print(f"  打印失败: {exc}", flush=True)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="扫发票二维码 -> A5 打印")
    ap.add_argument("--printer", default=DEFAULT_PRINTER)
    ap.add_argument("--dry-run", action="store_true", help="只下载并报告, 不打印")
    ap.add_argument("--once", metavar="URL", help="处理这一个地址")
    ap.add_argument("--from-screen", action="store_true",
                    help="从浏览器地址栏 OCR 取地址 (需要业务机屏幕可见)")
    args = ap.parse_args()

    if args.once:
        return 0 if handle(args.once, args.printer, args.dry_run) else 1

    if args.from_screen:
        url = url_from_screen()
        if not url:
            print("屏幕上没找到发票地址 —— 确认浏览器已经跳到 PDF 页面", flush=True)
            return 1
        print(f"地址栏读到: {url}", flush=True)
        return 0 if handle(url, args.printer, args.dry_run) else 1

    print("把光标放在这里, 用扫码枪扫发票二维码 (Ctrl+C 结束)", flush=True)
    try:
        for line in sys.stdin:
            handle(line, args.printer, args.dry_run)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())