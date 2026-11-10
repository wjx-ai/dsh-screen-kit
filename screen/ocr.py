"""跨平台 OCR: 把截图变成带坐标的文字块, 并且能定位到"块里的某一段"。

这块是从 fishAI (https://github.com/wjx-ai) 的 OCR 层搬过来的, 三段逻辑
是反复点错按钮之后才修对的, 不要为了"简洁"简化掉:

1. `TextBlock.segments` + `rect_of()`
   OCR 常把一排相邻按钮粘成一个块, 例如 '医保退费追溯码开票打印中'。
   块中心可能正好落在「追溯码」上, 而你要点的是「开票」。
   保留分段坐标, 才能算出那两个字真正在哪。

2. `_merge_blocks()` 的合并规则
   "登 录" 要能匹配到 "登录"。分段坐标就是从这一步来的。

3. 模糊匹配必须单独标 `fuzzy=True`
   三级回退的最后一级是"按字符重叠猜", 位置是猜出来的。
   点击类调用必须拒绝它, 否则会点到不存在的文字
   (真实事故: 找「修改追溯码」匹配到了「医保退费追」)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from PIL import Image

_BACKEND_CACHE: dict[str, Any] = {}


@dataclass
class TextBlock:
    text: str
    x: int
    y: int
    width: int
    height: int
    confidence: float = 0.0
    # 这个块是几个更小的块拼起来的? 保留每段自己的矩形, 才能定位到"块里的某一段"。
    segments: tuple[tuple[str, int, int, int, int], ...] = ()

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def rect_of(self, start: int, end: int) -> tuple[int, int, int, int]:
        """text 里 [start, end) 这段文字在截图上的矩形。

        有分段坐标就按分段取 (精确); 没有就按字符比例估 (近似)。
        """
        if not self.segments:
            return self._proportional_rect(start, end)
        # 用和 _join 完全一样的方式把分段拼回去, 才能让字符下标对得上
        picked: list[tuple[int, int, int, int]] = []
        rebuilt = ""
        for seg_text, sx, sy, sw, sh in self.segments:
            if rebuilt and _is_ascii_edge(rebuilt, seg_text):
                rebuilt += " "
            seg_start = len(rebuilt)
            rebuilt += seg_text
            seg_end = len(rebuilt)
            if seg_end > start and seg_start < end:
                picked.append((sx, sy, sw, sh))
        if not picked:
            return self._proportional_rect(start, end)
        x1 = min(r[0] for r in picked)
        y1 = min(r[1] for r in picked)
        x2 = max(r[0] + r[2] for r in picked)
        y2 = max(r[1] + r[3] for r in picked)
        return (x1, y1, x2 - x1, y2 - y1)

    def _proportional_rect(self, start: int, end: int) -> tuple[int, int, int, int]:
        """按字符宽度比例估子串位置。中日韩字符算 1 份, 其它算 0.5 份。"""
        text = self.text
        if not text:
            return (self.x, self.y, self.width, self.height)
        weights = [0.5 if ch.isascii() and ch.strip() else 1.0 for ch in text]
        total = sum(weights) or 1.0
        before = sum(weights[:max(0, start)])
        span = sum(weights[start:end])
        x = self.x + int(before / total * self.width)
        width = max(1, int(span / total * self.width))
        return (x, self.y, min(width, self.x + self.width - x), self.height)

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "rect": [self.x, self.y, self.width, self.height],
            "center": list(self.center),
            "confidence": round(self.confidence, 3),
            "segments": [s[0] for s in self.segments],
        }


def available_backend() -> str | None:
    """返回当前可用的 OCR 引擎名, 没有就 None。"""
    if "backend" in _BACKEND_CACHE:
        return _BACKEND_CACHE["backend"]
    name: str | None = None
    try:
        import rapidocr_onnxruntime  # noqa: F401

        name = "rapidocr"
    except ImportError:
        try:
            import pytesseract  # noqa: F401

            name = "pytesseract"
        except ImportError:
            name = None
    _BACKEND_CACHE["backend"] = name
    return name


def parse_boxes(result) -> list[TextBlock]:
    """把 rapidocr 的原始输出变成 TextBlock 列表 (已合并同一行)。

    单独抽出来是为了能脱屏测试 —— 不需要真跑 OCR 就能验证合并和分段。
    """
    blocks: list[TextBlock] = []
    for item in result or []:
        try:
            box, text, score = item
        except (TypeError, ValueError):
            continue
        clean = (text or "").strip()
        if not clean:
            continue
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except (TypeError, ValueError, IndexError):
            continue
        if not xs or not ys:
            continue
        left, top = min(xs), min(ys)
        blocks.append(TextBlock(
            text=clean,
            x=int(left), y=int(top),
            width=max(1, int(max(xs) - left)), height=max(1, int(max(ys) - top)),
            confidence=float(score or 0.0),
        ))
    return merge_blocks(blocks)


def ocr(image: Image.Image, *, lang: str = "", max_side: int = 2200) -> list[TextBlock]:
    backend = available_backend()
    if backend is None:
        raise RuntimeError(
            "没有可用的 OCR 引擎。装一个:\n"
            "  pip install rapidocr_onnxruntime     # 推荐, 纯 pip, 自带中文模型\n"
            "  pip install pytesseract             # 或者这个 + 系统装 tesseract-ocr"
        )
    work = image
    scale = 1.0
    if max(image.size) > max_side:
        scale = max_side / max(image.size)
        work = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
    if backend == "rapidocr":
        blocks = _ocr_rapidocr(work)
    else:
        blocks = _ocr_tesseract(work, lang)
    # 缩放过就还原坐标
    if scale != 1.0 and blocks:
        inverse = 1.0 / scale
        blocks = [
            TextBlock(b.text, int(b.x * inverse), int(b.y * inverse),
                      int(b.width * inverse), int(b.height * inverse), b.confidence,
                      segments=tuple(
                          (s[0], int(s[1] * inverse), int(s[2] * inverse),
                           int(s[3] * inverse), int(s[4] * inverse))
                          for s in b.segments
                      ))
            for b in blocks
        ]
    return blocks


def _ocr_rapidocr(image: Image.Image) -> list[TextBlock]:
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR  # type: ignore

    engine = _BACKEND_CACHE.get("rapidocr_engine")
    if engine is None:
        engine = RapidOCR()
        _BACKEND_CACHE["rapidocr_engine"] = engine
    result, _ = engine(np.asarray(image.convert("RGB")))
    return parse_boxes(result)


def _ocr_tesseract(image: Image.Image, lang: str) -> list[TextBlock]:
    import pytesseract  # type: ignore
    from pytesseract import Output

    language = lang or ("chi_sim+eng" if _has_chinese_lang() else "eng")
    data = pytesseract.image_to_data(image, lang=language, output_type=Output.DICT)
    blocks: list[TextBlock] = []
    for index, text in enumerate(data.get("text", [])):
        clean = (text or "").strip()
        if not clean:
            continue
        try:
            confidence = float(data["conf"][index])
        except (KeyError, ValueError):
            confidence = 0.0
        if confidence and confidence < 30:
            continue
        blocks.append(TextBlock(
            text=clean,
            x=int(data["left"][index]), y=int(data["top"][index]),
            width=int(data["width"][index]), height=int(data["height"][index]),
            confidence=(confidence / 100.0) if confidence else 0.0,
        ))
    return merge_blocks(blocks)


def _has_chinese_lang() -> bool:
    try:
        import pytesseract  # type: ignore

        return "chi_sim" in pytesseract.get_languages(config="")
    except Exception:
        return False


def merge_blocks(blocks: list[TextBlock], gap: int = 14) -> list[TextBlock]:
    """把同一行相邻的单词块合并, 否则"登 录"永远匹配不到"登录"。"""
    if not blocks:
        return []
    ordered = sorted(blocks, key=lambda b: (b.y, b.x))
    lines: list[list[TextBlock]] = []
    for block in ordered:
        placed = False
        for line in lines:
            head = line[0]
            same_line = abs(block.y - head.y) <= gap and abs(block.height - head.height) <= gap
            if same_line:
                line.append(block)
                placed = True
                break
        if not placed:
            lines.append([block])
    merged: list[TextBlock] = []
    for line in lines:
        line.sort(key=lambda b: b.x)
        current: list[TextBlock] = []
        prev_right: int | None = None
        for block in line:
            if prev_right is not None and block.x - prev_right > gap * 3:
                merged.append(_join(current))
                current = []
            current.append(block)
            prev_right = block.x + block.width
        if current:
            merged.append(_join(current))
    return [m for m in merged if m.text]


def _join(blocks: list[TextBlock]) -> TextBlock:
    x1 = min(b.x for b in blocks)
    y1 = min(b.y for b in blocks)
    x2 = max(b.x + b.width for b in blocks)
    y2 = max(b.y + b.height for b in blocks)
    text = ""
    segments: list[tuple[str, int, int, int, int]] = []
    for index, block in enumerate(blocks):
        if index and _is_ascii_edge(text, block.text):
            text += " "
        text += block.text
        # 保留每一段自己的矩形 —— 合并后还要能定位回"这一段"
        segments.append((block.text, block.x, block.y, block.width, block.height))
    return TextBlock(text, x1, y1, x2 - x1, y2 - y1,
                     sum(b.confidence for b in blocks) / len(blocks),
                     segments=tuple(segments))


def _is_ascii_edge(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left[-1].isascii() and right[0].isascii()


@dataclass
class TextHit:
    """一次文字命中: 是"块里的某一段", 不是整个块。"""

    block: TextBlock
    start: int            # 命中在 block.text 里的起止下标
    end: int
    fuzzy: bool = False   # 是否靠"字符重叠"蒙出来的 (不是真包含)

    @property
    def text(self) -> str:
        return self.block.text[self.start:self.end]

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return self.block.rect_of(self.start, self.end)

    @property
    def center(self) -> tuple[int, int]:
        x, y, w, h = self.rect
        return (x + w // 2, y + h // 2)

    @property
    def tier(self) -> int:
        """这段文字有多像一个独立控件 (0 最像)。

        0 = 整块就是这几个字, 或者它是分段里独立的一段  -> 按钮
        1 = 命中在块开头                                -> 多半是标签
        2 = 夹在别的词中间                              -> 状态文字/正文

        真实事故: 找「开票」时屏幕上同时有
          '待开票'(状态) 和 '开票￥116.43'(确认按钮), 只看长短会选错;
          '挂号开票状态：待开票' 里的「开票」更是纯状态文字, 点上去什么也不会发生。
        """
        needle = self.text.strip().lower()
        if self.block.text.strip().lower() == needle:
            return 0
        for seg in self.block.segments:
            if seg[0].strip().lower() == needle:
                return 0
        return 1 if self.start == 0 else 2

    def as_dict(self) -> dict:
        x, y, w, h = self.rect
        return {
            "text": self.text,
            "whole": self.block.text,
            "rect": [x, y, w, h],
            "center": list(self.center),
            "confidence": round(self.block.confidence, 3),
            "fuzzy": self.fuzzy,
            "tier": self.tier,
            "segments": [s[0] for s in self.block.segments],
        }


def search_blocks(blocks: list[TextBlock], needle: str, *, exact: bool = False) -> list[TextHit]:
    """在已经 OCR 好的块里找文字 —— 跟 ocr() 分开, 方便脱屏测试。"""
    needle = (needle or "").strip()
    if not needle:
        return []
    target = needle.lower()
    compact = re.sub(r"\s+", "", target)

    hits: list[TextHit] = []
    for block in blocks:
        lowered = block.text.lower()
        start = lowered.find(target)
        if start >= 0:
            hits.append(TextHit(block, start, start + len(target)))
            continue
        # 退一步: 去掉空白再比 (OCR 常把"登 录"识别成两段)
        squashed = re.sub(r"\s+", "", lowered)
        squashed_start = squashed.find(compact)
        if squashed_start >= 0:
            # 把"去掉空白后的下标"映射回原文下标
            mapping = [i for i, ch in enumerate(lowered) if not ch.isspace()]
            if squashed_start + len(compact) <= len(mapping):
                raw_start = mapping[squashed_start]
                raw_end = mapping[squashed_start + len(compact) - 1] + 1
                hits.append(TextHit(block, raw_start, raw_end))
    if hits or exact:
        return hits

    # 兜底: OCR 认错个别字时按字符重叠猜。**只用于"看看有没有可能"**,
    # fuzzy=True 明确标出来, 点击类工具会拒绝它。
    scored = []
    for block in blocks:
        overlap = len(set(compact) & set(re.sub(r"\s+", "", block.text.lower())))
        if overlap >= max(2, int(len(compact) * 0.7)):
            scored.append((overlap, block))
    scored.sort(key=lambda item: -item[0])
    out: list[TextHit] = []
    for _score, block in scored[:3]:
        start = max(0, block.text.lower().find(compact[:1]))
        out.append(TextHit(block, start, min(len(block.text), start + len(compact)), fuzzy=True))
    return out


def find_text(image: Image.Image, needle: str, *, exact: bool = False,
              lang: str = "") -> list[TextHit]:
    """按文本查找, 返回**命中子串自己的位置**。

    exact=True 时完全关闭容错 (只剩真包含), 点击类调用应该用它。
    """
    return search_blocks(ocr(image, lang=lang), needle, exact=exact)


def ocr_text(image: Image.Image, lang: str = "") -> str:
    """整屏文字, 给模型当"便宜的观察"用 (不需要图片时省 token)。"""
    return "\n".join(b.text for b in ocr(image, lang=lang))