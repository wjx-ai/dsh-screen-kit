"""OCR 层的脱屏测试 —— 不需要真截图, 任何平台都能跑。

这些用例锁的是"点错按钮"那类真实事故, 不是为了凑覆盖率:
  · 一排相邻按钮被读成一整块时, 必须能定位到其中某一段
  · 状态文字('开票状态：待开票')不能排在真按钮前面
  · 模糊匹配必须标记出来, 否则点击路径会点到不存在的文字
"""
import unittest

from screen import ocr as O


def block(text, x=0, y=0, w=100, h=20, segments=None, confidence=0.9):
    return O.TextBlock(text, x, y, w, h, confidence,
                       segments=tuple(segments) if segments else ())


def raw(box_x, box_y, box_w, text, score=0.9):
    """拼一份 rapidocr 风格的原始输出: [box(四点), text, score]。"""
    return (
        [(box_x, box_y), (box_x + box_w, box_y),
         (box_x + box_w, box_y + 20), (box_x, box_y + 20)],
        text,
        score,
    )


class MergeAndSegmentsTest(unittest.TestCase):
    """一排按钮被读成一整块时, 必须还能算出其中某一段在哪。"""

    def test_adjacent_buttons_become_one_block_with_segments(self):
        # 真实收费界面上量到的坐标 (合并阈值是 gap*3 = 42px, 相邻按钮间距都在这个范围内)
        result = [
            raw(1288, 95, 52, "医保"),
            raw(1361, 95, 52, "退费"),
            raw(1438, 95, 68, "追溯码"),
            raw(1535, 95, 52, "开票"),
            raw(1619, 95, 52, "打印"),
            raw(1695, 95, 26, "中"),
        ]
        blocks = O.parse_boxes(result)
        self.assertEqual(len(blocks), 1, "同一行相邻的块应该合成为一个")
        joined = blocks[0]
        self.assertEqual(joined.text, "医保退费追溯码开票打印中")
        self.assertEqual([s[0] for s in joined.segments],
                         ["医保", "退费", "追溯码", "开票", "打印", "中"])

    def test_rect_of_points_at_the_matched_words_not_the_block_center(self):
        """这就是"想点开票却点到追溯码"那个 bug 的回归测试。"""
        segments = [
            ("医保", 1288, 95, 44, 28),
            ("退费", 1340, 95, 44, 28),
            ("追溯码", 1390, 95, 50, 28),
            ("开票", 1490, 95, 40, 28),
            ("打印", 1560, 95, 44, 28),
            ("中", 1610, 95, 30, 28),
        ]
        joined = block("医保退费追溯码开票打印中", 1288, 95, 352, 28,
                       segments=segments)
        start = joined.text.index("开票")
        x, y, w, h = joined.rect_of(start, start + 2)
        self.assertEqual(x, 1490, "「开票」的左边应该在 1490")
        self.assertEqual(w, 40)
        # 块中心会落在「追溯码」附近, 跟 1490 差很远 —— 正是当初点错的原因
        self.assertNotAlmostEqual(joined.center[0], x + w // 2, delta=20)

    def test_missing_segments_falls_back_to_proportional(self):
        only = block("abcdefgh", 100, 10, 80, 20)
        x, _y, w, _h = only.rect_of(0, 4)
        self.assertEqual(x, 100)
        self.assertGreater(w, 0)

    def test_words_far_apart_are_not_merged(self):
        blocks = O.parse_boxes([
            raw(10, 10, 40, "左"),
            raw(600, 10, 40, "右"),
        ])
        self.assertEqual(len(blocks), 2, "间距太大不该合并")


class TierTest(unittest.TestCase):
    """档位决定"这段文字像不像一个能点的控件"。"""

    def test_whole_block_equals_needle_is_tier_0(self):
        hit = O.search_blocks([block("开票")], "开票")[0]
        self.assertEqual(hit.tier, 0)

    def test_independent_segment_is_tier_0(self):
        segments = [("￥227.00", 1200, 95, 90, 28), ("医保", 1300, 95, 44, 28),
                    ("开票", 1490, 95, 40, 28)]
        joined = block("￥227.00医保开票", 1200, 95, 330, 28, segments=segments)
        hit = O.search_blocks([joined], "开票")[0]
        self.assertEqual(hit.tier, 0, "分段里独立的一段应当算作按钮")

    def test_prefix_hit_is_tier_1(self):
        hit = O.search_blocks([block("开票￥227.00")], "开票")[0]
        self.assertEqual(hit.tier, 1)

    def test_status_text_buried_in_prose_is_tier_2(self):
        hit = O.search_blocks([block("挂号开票状态：待开票")], "开票")[0]
        self.assertEqual(hit.tier, 2, "状态里的「开票」不能当成按钮")

    def test_button_beats_status_label(self):
        """真实界面: 同一屏既有按钮又有状态文字, 必须选按钮。"""
        segments = [("￥227.00", 1200, 95, 90, 28), ("?医保", 1300, 95, 60, 28),
                    ("退费", 1360, 95, 44, 28), ("追溯码", 1404, 95, 50, 28),
                    ("开票", 1490, 95, 40, 28), ("打印", 1560, 95, 44, 28)]
        merged = block("￥227.00 ?医保退费追溯码开票打印中", 1200, 95, 404, 28,
                       segments=segments)
        hits = O.search_blocks([merged, block("挂号开票状态：待开票", 665, 204, 200, 20)],
                               "开票")
        ranked = sorted(hits, key=lambda h: (h.tier, h.block.y))
        self.assertEqual(ranked[0].tier, 0)
        self.assertIn("追溯码开票", ranked[0].block.text)

    def test_length_is_not_a_ranking_signal(self):
        """按长度排会选错: 金额前缀让按钮那一行变长了。"""
        segments = [("￥227.00", 1200, 95, 90, 28), ("开票", 1490, 95, 40, 28)]
        long_button_row = block("￥227.00医保退费追溯码开票打印中", 1200, 95, 404, 28,
                                segments=segments)
        short_status = block("挂号开票状态：待开票", 665, 204, 200, 20)
        self.assertGreater(len(long_button_row.text), len(short_status.text))
        hits = O.search_blocks([long_button_row, short_status], "开票")
        best = sorted(hits, key=lambda h: (h.tier,))[0]
        self.assertIs(best.block, long_button_row)


class FuzzyTest(unittest.TestCase):
    """模糊匹配的位置是猜的, 点击路径必须能拒掉它。"""

    def test_wrong_characters_do_not_match_exactly(self):
        hits = O.search_blocks([block("医保退费追")], "修改追溯码", exact=True)
        self.assertEqual(hits, [], "exact=True 时不该蒙出命中")

    def test_fuzzy_fallback_is_marked(self):
        """真实事故: 找「修改追溯码」, OCR 认错字后在「医保退费追溯码」上蒙出命中。"""
        hits = O.search_blocks([block("医保退费追溯码")], "修改追溯码", exact=False)
        self.assertTrue(hits, "关闭 exact 后应该给出模糊候选")
        self.assertTrue(all(h.fuzzy for h in hits), "模糊候选必须带 fuzzy 标记")

    def test_too_little_overlap_is_not_fuzzy_matched(self):
        """重叠太少时连模糊候选都不该有 —— 否则会点到完全无关的地方。"""
        hits = O.search_blocks([block("医保退费追")], "修改追溯码", exact=False)
        self.assertEqual(hits, [])

    def test_real_substring_beats_fuzzy(self):
        hits = O.search_blocks([block("医保退费追"), block("修改追溯码")], "修改追溯码")
        real = [h for h in hits if not h.fuzzy]
        self.assertEqual(len(real), 1)
        self.assertEqual(real[0].block.text, "修改追溯码")


class WhitespaceTest(unittest.TestCase):
    def test_space_inside_ocr_text_still_matches(self):
        hits = O.search_blocks([block("登 录")], "登录")
        self.assertEqual(len(hits), 1, "OCR 把字拆开时也要能匹配上")

    def test_needle_with_space_still_matches(self):
        hits = O.search_blocks([block("登录")], "登 录")
        self.assertEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main()