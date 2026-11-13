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
import json
import os
import re
import subprocess
import sys
import tempfile
import time
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


# ---- 盯 Downloads ------------------------------------------------------
#
# ABC数字医疗云 开完票会把 PDF 直接下载到 Downloads, 文件名就是发票 id。
# 所以"扫码 -> 浏览器 -> 抄地址"是绕远路: 文件本来就在本机。
# 实测文件名形如 4730125ecbb16ca1671.pdf, 和 einvoice.jsczt.cn/d/<id> 的内容一致。

# 发票 id 实测 18~19 位十六进制 (4730125ecbb16ca1671)。
# 下限取 17 是为了排除 Downloads 里常见的 16 位纯数字临时文件名
# (实测混进来一个 1217322539213690.pdf), 那种不该被当成发票打出去。
INVOICE_NAME = re.compile(r"^[0-9a-f]{17,24}\.pdf$", re.I)
STATE_DIR = os.path.expandvars(r"%LOCALAPPDATA%\dsh-screen-kit")
STATE_FILE = os.path.join(STATE_DIR, "printed.json")


def download_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Downloads")


def load_state() -> set:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return set(json.load(fh))
    except Exception:
        return set()


def save_state(seen: set) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(sorted(seen), fh, ensure_ascii=False, indent=1)


def invoice_pdfs(folder: str) -> list:
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(n for n in names if INVOICE_NAME.match(n))


def handle_file(path: str, printer: str, dry_run: bool, after: str) -> bool:
    sizes = page_sizes(path)
    size_kb = os.path.getsize(path) // 1024
    print(f"  新发票: {os.path.basename(path)}  {size_kb} KB  {len(sizes)} 页  {sizes}", flush=True)
    try:
        how = print_pdf(path, printer, dry_run)
        print(f"  {how}", flush=True)
    except Exception as exc:
        print(f"  打印失败: {exc}", flush=True)
        return False
    if after == "move" and not dry_run:
        done_dir = os.path.join(download_dir(), "_printed")
        os.makedirs(done_dir, exist_ok=True)
        try:
            os.replace(path, os.path.join(done_dir, os.path.basename(path)))
            print(f"  已移到 _printed/", flush=True)
        except OSError as exc:
            print(f"  移动失败: {exc}", flush=True)
    return True


def watch(printer: str, dry_run: bool, interval: float, after: str,
          print_existing: bool) -> int:
    folder = download_dir()
    seen = load_state()
    current = invoice_pdfs(folder)
    if not print_existing:
        # 第一次跑不要把历史文件全打一遍 —— 把现状记下来, 只处理之后新增的
        new = [n for n in current if n not in seen]
        if new:
            print(f"忽略 {len(new)} 个已有文件 (要补打加 --print-existing)", flush=True)
        seen.update(current)
        save_state(seen)
        print(f"盯着 {folder} 等新发票 ... (Ctrl+C 结束)", flush=True)
    for name in current if print_existing else []:
        if name in seen and not print_existing:
            continue
        path = os.path.join(folder, name)
        if handle_file(path, printer, dry_run, after):
            seen.add(name)
            save_state(seen)
    try:
        while True:
            time.sleep(interval)
            for name in invoice_pdfs(folder):
                if name in seen:
                    continue
                path = os.path.join(folder, name)
                time.sleep(0.8)          # 等下载写完, 免得读到半个文件
                if not os.path.exists(path):
                    continue
                if handle_file(path, printer, dry_run, after):
                    seen.add(name)
                    save_state(seen)
    except KeyboardInterrupt:
        print("\n停止", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="扫发票二维码 -> A5 打印")
    ap.add_argument("--printer", default=DEFAULT_PRINTER)
    ap.add_argument("--dry-run", action="store_true", help="只下载并报告, 不打印")
    ap.add_argument("--once", metavar="URL", help="处理这一个地址")
    ap.add_argument("--from-screen", action="store_true",
                    help="从浏览器地址栏 OCR 取地址 (需要业务机屏幕可见)")
    ap.add_argument("--watch", action="store_true",
                    help="盯 Downloads: 一出现新发票 PDF 就打印")
    ap.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒 (默认 1)")
    ap.add_argument("--after-print", choices=["keep", "move"], default="keep",
                    help="打完 keep 保留原文件 / move 移到 Downloads/_printed")
    ap.add_argument("--print-existing", action="store_true",
                    help="连已存在的发票一起打 (默认忽略历史, 只打新增)")
    args = ap.parse_args()

    if args.watch:
        return watch(args.printer, args.dry_run, args.interval,
                     args.after_print, args.print_existing)

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