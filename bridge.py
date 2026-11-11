"""dsh-screen-kit 的 Python 引擎: 截图 / OCR / 精确找字 / 点击 / 窗口。

平台差异全部关在 backend 里, 这一层只管"看什么、点哪里"。

为什么 OCR 在 Python 而不在 node:
  rapidocr 只有 Python 版, 模型加载要 1.5 秒 —— 放在一个进程里跑完一整批点击,
  比每次调用都重新加载快一个数量级。
"""
from __future__ import annotations

import json
import os
import sys
import time

if __package__ in (None, ""):                      # 以脚本方式直接运行时
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screen import ocr as ocr_mod                       # noqa: E402
from screen.base import BackendError, load_backend       # noqa: E402

_BACKEND = None


def backend():
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = load_backend(os.environ.get("SCREEN_KIT_BACKEND", ""))
    return _BACKEND


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---- 坐标换算 ------------------------------------------------------------

def region_px(image, region: str):
    """归一化 'x1,y1,x2,y2' (0..1000) -> (裁剪图, 偏移x, 偏移y)。"""
    width, height = image.size
    parts = [float(p) for p in str(region).replace(" ", "").split(",")]
    if len(parts) != 4:
        raise ValueError(f"region 要写成 'x1,y1,x2,y2', 收到 {region!r}")
    x1, y1, x2, y2 = parts
    px1, py1 = int(x1 / 1000 * width), int(y1 / 1000 * height)
    px2, py2 = int(x2 / 1000 * width), int(y2 / 1000 * height)
    px1, py1 = max(0, px1), max(0, py1)
    px2, py2 = min(width, px2), min(height, py2)
    if px2 <= px1 or py2 <= py1:
        raise ValueError(f"region {region!r} 换算成像素是空的 ({px1},{py1})-({px2},{py2})")
    return image.crop((px1, py1, px2, py2)), px1, py1


def grab(region: str = ""):
    """截一张图; 给了归一化 region 就只截那块 (更快, 有些平台还能省一次解码)。"""
    be = backend()
    if region:
        bx, by, bw, bh = be.screen_bounds()
        probe = None
        parts = [float(p) for p in region.replace(" ", "").split(",")]
        if len(parts) == 4:
            x1, y1, x2, y2 = parts
            rx = bx + int(x1 / 1000 * bw)
            ry = by + int(y1 / 1000 * bh)
            rw = max(1, int((x2 - x1) / 1000 * bw))
            rh = max(1, int((y2 - y1) / 1000 * bh))
            try:
                probe = be.capture((rx, ry, rw, rh))
            except BackendError:
                probe = None
        if probe is not None:
            # 必须把区域原点带回去 —— 否则裁剪图里的坐标会被当成全屏坐标,
            # 点出去就偏到左上角 (真的点错过一次, 想点 (1661,88) 点到了 (101,39))
            return probe, rx, ry
    img = be.capture()
    if not region:
        return img, 0, 0
    crop, ox, oy = region_px(img, region)
    return crop, ox, oy


def normalize(x: int, y: int):
    _bx, _by, width, height = backend().screen_bounds()
    return round(x / width * 1000), round(y / height * 1000)


# ---- 动作 ----------------------------------------------------------------

def click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> None:
    be = backend()
    # 先把目标窗口顶到前台: 有的应用在后台时根本不响应控件点击,
    # 表现是"找到了按钮但点了没反应" (批量跑到一半用户切窗口就会这样)。
    try:
        be.ensure_active(int(x), int(y))
    except BackendError:
        pass
    be.move(int(x), int(y))
    time.sleep(0.02)
    for index in range(max(1, clicks)):
        be.button(button, True)
        time.sleep(0.01)
        be.button(button, False)
        if index + 1 < clicks:
            time.sleep(0.06)


def press(keys) -> None:
    be = backend()
    for name in keys:
        be.key(name, True)
        time.sleep(0.02)
        be.key(name, False)
        time.sleep(0.02)


def press_esc() -> None:
    press(["esc"])


# ---- 找字 ----------------------------------------------------------------

def find_hits(image, needle: str, region: str = "", exact: bool = True,
              max_tier: int = 2, offset: tuple[int, int] = (0, 0)):
    ox, oy = offset
    hits = []
    for hit in ocr_mod.search_blocks(ocr_mod.ocr(image), needle, exact=exact):
        if hit.tier > max_tier:
            continue
        hits.append({
            "text": hit.text,
            "block": hit.block.text,
            "x": hit.center[0] + ox,
            "y": hit.center[1] + oy,
            "w": hit.rect[2],
            "h": hit.rect[3],
            "tier": hit.tier,
            "fuzzy": bool(hit.fuzzy),
            "segments": [s[0] for s in hit.block.segments],
        })
    # 先看档位, 同档取更靠左上的; 模糊匹配永远排最后 (点击路径会拒绝它)
    hits.sort(key=lambda r: (r["fuzzy"], r["tier"], r["y"], r["x"]))
    return hits


def locate(needle: str, region: str = "", exact: bool = True, max_tier: int = 2):
    """截一次图, 在一个区域里找 needle, 返回候选列表 + 区域偏移。"""
    image, ox, oy = grab(region)
    return find_hits(image, needle, "", exact=exact, max_tier=max_tier, offset=(ox, oy))


# ---- 命令 ----------------------------------------------------------------

def cmd_info(_args):
    be = backend()
    info = be.describe()
    info.update({
        "ok": True,
        "ocr": ocr_mod.available_backend(),
        "platform": sys.platform,
    })
    try:
        bx, by, bw, bh = be.screen_bounds()
        info["screen"] = {"x": bx, "y": by, "width": bw, "height": bh}
    except BackendError as exc:
        info["screen_error"] = str(exc)
    if not info.get("ocr"):
        info["hint"] = "没有 OCR 引擎: pip install rapidocr_onnxruntime"
    return info


def cmd_read(args):
    region = args.get("region", "")
    image, ox, oy = grab(region)
    blocks = []
    for b in ocr_mod.ocr(image):
        if not b.text.strip():
            continue
        nx, ny = normalize(b.x + ox, b.y + oy)
        blocks.append({"text": b.text, "x": b.x + ox, "y": b.y + oy,
                       "w": b.width, "h": b.height, "nx": nx, "ny": ny})
    blocks.sort(key=lambda r: (r["y"], r["x"]))
    return {"ok": True, "count": len(blocks), "blocks": blocks,
            "text": "\n".join(b["text"] for b in blocks)}


def cmd_find(args):
    needle = args.get("text") or args.get("needle")
    if not needle:
        return {"ok": False, "error": "需要 text 参数"}
    hits = locate(needle, args.get("region", ""),
                  exact=args.get("exact", True),
                  max_tier=int(args.get("max_tier", 2)))
    for h in hits:
        h["nx"], h["ny"] = normalize(h["x"], h["y"])
    return {"ok": True, "found": bool(hits), "count": len(hits),
            "best": hits[0] if hits else None,
            "hits": hits[: int(args.get("limit", 20))]}


def cmd_click(args):
    x, y = args.get("x"), args.get("y")
    if x is None or y is None:
        return {"ok": False, "error": "需要 x 和 y (像素坐标)"}
    click_at(int(x), int(y), args.get("button", "left"), int(args.get("clicks", 1)))
    return {"ok": True, "clicked": [int(x), int(y)],
            "normalized": list(normalize(int(x), int(y)))}


def cmd_click_text(args):
    needle = args.get("text") or args.get("needle")
    if not needle:
        return {"ok": False, "error": "需要 text 参数"}
    hits = locate(needle, args.get("region", ""),
                  exact=args.get("exact", True),
                  max_tier=int(args.get("max_tier", 1)))
    solid = [h for h in hits if not h["fuzzy"]]
    if not solid:
        return {"ok": False,
                "error": f"屏幕上找不到 {needle!r}, 所以没有点",
                "near_misses": [h["block"] for h in hits],
                "hint": "用 screen_read 看看屏幕上实际有什么字"}
    best = solid[0]
    # 给了区域就再认一遍: 从找到到点击之间界面可能已经变了
    # (同一个位置上的「开票」可能已经变成「红冲」), 认不出目标文字就不点。
    region = args.get("region", "")
    if region:
        fresh, why = relook(region, needle, best)
        if fresh is None:
            return {"ok": False,
                    "error": f"拒绝点击: {why}, 所以没有点",
                    "expected": needle,
                    "hint": "屏幕在两帧之间变了; 用 screen_read 重新看一遍再决定"}
        best = fresh
    click_at(best["x"], best["y"], args.get("button", "left"), int(args.get("clicks", 1)))
    for h in solid:
        h["nx"], h["ny"] = normalize(h["x"], h["y"])
    # relook() 返回的是 locate() 新建的字典, 不在 solid 里, 上面那个循环碰不到它,
    # 所以这里单独算: 否则点完会在返回值上抛 KeyError (按钮其实已经点出去了)。
    best["nx"], best["ny"] = normalize(best["x"], best["y"])
    others = [{"text": h["text"], "nx": h["nx"], "ny": h["ny"], "block": h["block"]}
              for h in solid[1:]]
    return {"ok": True, "clicked": [best["x"], best["y"]],
            "normalized": [best["nx"], best["ny"]],
            "text": best["text"], "block": best["block"], "tier": best["tier"],
            "position": "独立分段" if best["tier"] == 0 else "块首",
            "others_same_name": others,
            "note": (f"屏上还有 {len(others)} 处同名, 点的是最靠上/最像按钮的那个"
                     if others else "")}


def cmd_key(args):
    keys = args.get("keys") or []
    if isinstance(keys, str):
        keys = [keys]
    if not keys:
        return {"ok": False, "error": "需要 keys, 例如 ['esc']"}
    press([str(k).strip().lower() for k in keys])
    return {"ok": True, "pressed": keys}


def cmd_windows(args):
    action = args.get("action", "list")
    be = backend()
    if action == "focus":
        # 优先按句柄: 同一个应用开了两个同标题窗口时, 标题根本区分不了
        handle = args.get("handle")
        if handle is not None:
            for win in be.windows():
                if int(win.handle) == int(handle):
                    done = be.focus(win.handle)
                    time.sleep(0.4)
                    return {"ok": bool(done), "focused": win.title,
                            "handle": win.handle}
            return {"ok": False, "error": f"没有句柄为 {handle} 的窗口"}
        title = args.get("title", "")
        if not title:
            return {"ok": False, "error": "需要 title 或 handle"}
        for win in be.windows():
            if title.lower() in (win.title or "").lower():
                done = be.focus(win.handle)
                time.sleep(0.4)
                return {"ok": bool(done), "focused": win.title, "handle": win.handle}
        return {"ok": False, "error": f"没有标题包含 {title!r} 的窗口"}
    items = [w.as_dict() for w in be.windows() if (w.title or "").strip()]
    return {"ok": True, "count": len(items),
            "foreground": be.foreground(), "windows": items}


def relook(region: str, needle: str, expected: dict,
           tolerance: int = 40) -> tuple[dict | None, str]:
    """按下鼠标之前, 用同一套识别再看一眼: 还在、而且还在原地, 才允许点。

    为什么需要: 从"认出按钮"到"按下鼠标"之间有一段间隔 —— 批处理里是 1.5 秒。
    这段时间界面可能已经变了。最典型的就是开票成功后, 同一个位置上的
    「开票￥17.88」变成了「红冲」: 照着旧坐标点下去就是一次冲销操作。

    为什么不用"截一个很小的小方块再 OCR": 试过, 不可靠 —— 125x62 的框里
    真机上只读出一个「口」, 结果是连正确的按钮都被拒绝(假阴性)。
    这里复用 locate(): 同样的区域、同样的档位规则, 就是白天一直在跑的那条路。

    返回 (可点的最新位置 or None, 拒绝的原因)。
    """
    hits = locate(needle, region, exact=True, max_tier=1)
    solid = [h for h in hits if not h["fuzzy"]]
    if not solid:
        # 拒绝时把那一带实际写着什么报出来, 让人一眼看出发生了什么
        blocks = ocr_mod.ocr(grab(region)[0])
        seen = " / ".join(b.text for b in blocks if b.text.strip())
        return None, f"那里现在是 {seen!r}, 已经不是 {needle!r}"
    best = min(solid, key=lambda h: (abs(h["x"] - expected["x"])
                                     + abs(h["y"] - expected["y"])))
    moved = abs(best["x"] - expected["x"]) + abs(best["y"] - expected["y"])
    if moved > tolerance:
        return None, (f"{needle!r} 从 ({expected['x']},{expected['y']}) "
                      f"移到了 ({best['x']},{best['y']}), 位置变化过大")
    return best, ""


def pick_empty(blocks, needles) -> str:
    """在已经识别好的文字块里, 找出第一个"没得开"的标志。

    同一个系统的"没得开"可能有几种长相, 例如 ABC数字医疗云 既有
    「暂无可开发票」的提示框, 待开票列表空的时候又会显示「暂无患者」。
    调用方传一串候选, 这里按顺序返回先撞到的那个。
    """
    for needle in needles or []:
        if not needle:
            continue
        if any(not h.fuzzy for h in ocr_mod.search_blocks(blocks, needle,
                                                          exact=True)):
            return needle
    return ""


def cmd_batch(args):
    """连续点击同一套按钮多次。

    每轮两步 (两步点的可能是同名按钮, 例如工具栏一个、弹窗里一个):
      1. 点 top_text (在 top_region 里找)
      2. 等 popup_text 在 popup_region 里真的出现, 点它, 等它消失

    停止条件: 撞到 empty_text / 按钮始终不出现 / 弹窗不关 / 达到 times。

    为什么用 OCR 轮询而不是画面差分: 差分只能说明"画面变了", 不能说明
    "按钮在了"。列表重绘、光标闪烁都会让差分跳变, 于是在弹窗还没渲染完时就去
    OCR, 查不到按钮, 误判成"弹窗里没有按钮"而停住 —— 这个坑真踩过。
    """
    top_text = args.get("top_text") or "开票"
    popup_text = args.get("popup_text") or top_text
    top_region = args.get("top_region", "")
    popup_region = args.get("popup_region", "")
    times = int(args.get("times", 20))
    interval = float(args.get("interval", 2.0))
    empty_text = args.get("empty_text", "")
    # 同一个系统"没得开"可能有几种不同的长相, 所以允许传一串:
    #   ABC数字医疗云就同时存在「暂无可开发票」提示框 和 待开票列表里显示「暂无患者」
    # 一次截图把所有候选都查一遍, 不重复 OCR。
    empty_texts = ([empty_text] if isinstance(empty_text, str)
                   else [t for t in (empty_text or []) if t])
    empty_texts = [t for t in empty_texts if t]
    empty_desc = " / ".join(repr(t) for t in empty_texts) if empty_texts else "''"
    # 有些系统"没得开"不是弹一个提示框, 而是按钮直接消失 (ABC数字医疗云就是这样:
    # 队列空了, 工具栏上的「开票」整个不见, 只留下状态标签)。
    # 打开这个开关, 工具栏上找不到 top_text 就当作 empty_text 一样处理。
    absent_means_empty = bool(args.get("absent_means_empty", False))
    wait_limit = float(args.get("wait_limit", 10.0))
    dry_run = bool(args.get("dry_run", False))
    log_lines: list[str] = []

    def note(msg: str) -> None:
        log_lines.append(msg)
        log(msg)

    def find_in(region: str, needle: str):
        hits = locate(needle, region, exact=True, max_tier=1)
        solid = [h for h in hits if not h["fuzzy"]]
        return solid[0] if solid else None

    def which_empty() -> str:
        """返回屏幕上先撞到的那个"没得开"标志, 都没撞到就返回空串。"""
        if not empty_texts:
            return ""
        image, _ox, _oy = grab("")          # 整屏只截一次、只识别一次
        return pick_empty(ocr_mod.ocr(image), empty_texts)

    def wait_for(region: str, needle: str, present: bool, limit: float):
        deadline = time.time() + limit
        while time.time() < deadline:
            hit = find_in(region, needle)
            if present and hit:
                return hit
            if not present and not hit:
                return True
            time.sleep(0.3)
        return None if present else False

    # ---- 干净起步: 上一轮可能留着弹窗 ----
    clean = False
    for _ in range(4):
        press_esc()
        time.sleep(0.9)
        if not (popup_region and find_in(popup_region, popup_text)):
            clean = True
            break
    if not clean:
        return {"ok": False, "done": 0,
                "stopped": "有弹窗关不掉, 先看一眼屏幕", "log": log_lines}

    top = find_in(top_region, top_text) if top_region else None
    if top is None:
        candidates = locate(top_text, "", exact=True, max_tier=1)
        top = candidates[0] if candidates else None
    if top is None:
        if absent_means_empty:
            return {"ok": True, "done": 0,
                    "stopped": (f"工具栏上没有 {top_text!r} —— 队列已空 "
                                f"(absent_means_empty 已开启, 视同 {empty_text!r})"),
                    "log": log_lines}
        return {"ok": False, "done": 0,
                "stopped": f"找不到起始按钮 {top_text!r}", "log": log_lines}
    note(f"起始按钮 {top_text!r} @pixel({top['x']},{top['y']})")

    if dry_run:
        current = find_in(popup_region, popup_text) if popup_region else None
        return {"ok": True, "dry_run": True, "done": 0,
                "stopped": "dry_run: 只定位, 没有点击",
                "top_button": {"text": top_text, "x": top["x"], "y": top["y"],
                               "tier": top["tier"], "block": top["block"]},
                "popup_open": bool(current),
                "popup_button": ({"text": popup_text, "x": current["x"], "y": current["y"]}
                                 if current else None),
                "log": log_lines}

    done = 0
    stopped = ""
    for i in range(1, times + 1):
        t0 = time.time()
        # 工具栏这一下: 点之前重新认一遍, 认不出「开票」就不点。
        fresh, why = relook(top_region, top_text, top) if top_region else (top, "")
        if fresh is None and absent_means_empty:
            # 按钮消失有两种可能: 真的开完了 / 列表正在刷新, 工具栏短暂空白。
            # 后者在 ABC数字医疗云 上真实发生过: 抢在空白那一瞬间宣布"队列已空",
            # 一秒后按钮又回来了, 于是白停一次。
            # 所以先找文字标志 (最可靠), 找不到再等一会儿重看一次。
            seen_empty = which_empty()
            if seen_empty:
                stopped = f"出现 {seen_empty!r} —— 没有可点的了"
                note(f"[{i}] {stopped}")
                press_esc()
                break
            time.sleep(1.5)
            recovered = find_in(top_region, top_text) if top_region else None
            if recovered is not None:
                fresh = recovered
                note(f"[{i}] 工具栏短暂空白后恢复, 继续")
        if fresh is None:
            if absent_means_empty:
                stopped = (f"工具栏上已经没有 {top_text!r} 了 —— 队列已空 "
                           f"(视同 {empty_desc})")
            else:
                stopped = f"拒绝点击工具栏 ({top['x']},{top['y']}): {why}"
            note(f"[{i}] {stopped}")
            break
        top = fresh
        click_at(top["x"], top["y"])

        hit = wait_for(popup_region, popup_text, True, wait_limit) if popup_region else None
        if hit is None:
            seen_empty = which_empty()
            if seen_empty:
                stopped = f"出现 {seen_empty!r} —— 没有可点的了"
                note(f"[{i}] {stopped}")
                press_esc()          # 别把这种提示框留在用户屏幕上
                break
            # 点可能被吞了 (上一张还在处理), 重新定位再点一次
            retry = find_in(top_region, top_text) if top_region else None
            if retry:
                top = retry
                click_at(top["x"], top["y"])
                hit = wait_for(popup_region, popup_text, True, wait_limit)
            if hit is None:
                seen_empty = which_empty()
                if seen_empty:
                    stopped = f"出现 {seen_empty!r} —— 没有可点的了"
                else:
                    stopped = f"点了 {top_text!r} 但 {popup_text!r} 一直没出现"
                note(f"[{i}] {stopped}")
                press_esc()
                break

        time.sleep(max(0.0, interval - 0.5))
        # 关键一下: 隔了 interval 秒, 那个坐标上早就未必还是「开票」了。
        # 开票成功后同一位置会变成「红冲」—— 盲点就是一次冲销操作。
        fresh, why = relook(popup_region, popup_text, hit) if popup_region else (hit, "")
        if fresh is None:
            stopped = f"拒绝点击 ({hit['x']},{hit['y']}): {why}"
            note(f"[{i}] {stopped}")
            press_esc()
            break
        hit = fresh
        click_at(hit["x"], hit["y"])

        if popup_region and not wait_for(popup_region, popup_text, False, 6.0):
            again = find_in(popup_region, popup_text)
            if again:
                click_at(again["x"], again["y"])
        closed = wait_for(popup_region, popup_text, False, wait_limit) if popup_region else True
        if not closed:
            stopped = f"点了 {popup_text!r} 但弹窗 {wait_limit:.0f} 秒没关"
            note(f"[{i}] {stopped}")
            press_esc()
            break

        done += 1
        note(f"[{i}] 完成 上({top['x']},{top['y']}) 下({hit['x']},{hit['y']}) "
             f"{time.time() - t0:.1f}s")
        if done % 10 == 0 and top_region:
            refresh = find_in(top_region, top_text)
            if refresh:
                top = refresh
        time.sleep(interval)
    else:
        stopped = f"达到上限 {times} 次"

    return {"ok": True, "done": done, "stopped": stopped or "结束", "log": log_lines}


COMMANDS = {
    "info": cmd_info,
    "read": cmd_read,
    "find": cmd_find,
    "click": cmd_click,
    "click_text": cmd_click_text,
    "key": cmd_key,
    "windows": cmd_windows,
    "batch": cmd_batch,
}


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "用法: bridge.py <命令> [json]",
                          "commands": sorted(COMMANDS)}, ensure_ascii=False))
        return
    command = sys.argv[1]
    handler = COMMANDS.get(command)
    if handler is None:
        print(json.dumps({"ok": False, "error": f"未知命令 {command!r}",
                          "commands": sorted(COMMANDS)}, ensure_ascii=False))
        return
    raw = sys.argv[2] if len(sys.argv) > 2 else "{}"
    try:
        args = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"参数不是合法 JSON: {exc}"},
                         ensure_ascii=False))
        return
    try:
        result = handler(args)
    except BackendError as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:                              # pragma: no cover
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()