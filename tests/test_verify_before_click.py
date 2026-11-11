"""按下鼠标之前的"再看一眼"必须真的能拦住。

真实场景: 开票成功后, 同一个位置上的「开票￥17.88」会变成「红冲」。
从认出按钮到按下鼠标之间隔着 1.5 秒, 照着旧坐标点下去就是一次冲销操作。
所以按下之前必须重新识别一次, 认不出目标文字就不点。

这里只测 relook() —— 它是所有点击前的最后一道闸门。
"""
import unittest

from PIL import Image

import bridge
from screen import ocr as ocr_mod
from screen.base import Backend, Capabilities

REGION = "0,0,1000,1000"


class FakeBackend(Backend):
    name = "fake"

    def screen_bounds(self):
        return (0, 0, 1920, 1080)

    def capture(self, region=None):
        if region:
            _x, _y, w, h = region
            return Image.new("RGB", (int(w), int(h)), "white")
        return Image.new("RGB", (1920, 1080), "white")

    def capabilities(self):
        return Capabilities(capture=True)


class RelookTest(unittest.TestCase):
    def setUp(self):
        self._backend = bridge._BACKEND
        self._ocr = ocr_mod.ocr
        bridge._BACKEND = FakeBackend()

    def tearDown(self):
        bridge._BACKEND = self._backend
        ocr_mod.ocr = self._ocr

    def _says(self, *texts_at):
        """texts_at: [(文字, x, y), ...] —— 模拟屏幕上这些位置有这些字。"""
        blocks = [ocr_mod.TextBlock(t, x, y, 60, 22, 0.9) for t, x, y in texts_at]
        ocr_mod.ocr = lambda image: blocks

    def test_allows_click_when_button_is_still_there(self):
        self._says(("开票￥17.88", 1880, 905))
        got, why = bridge.relook(REGION, "开票", {"x": 1899, "y": 914})
        self.assertIsNotNone(got, f"按钮还在原处却拒绝了: {why}")
        self.assertEqual(why, "")

    def test_refuses_when_button_became_red_reversal(self):
        """必须拦住的场景: 那里现在写着「红冲」。"""
        self._says(("红冲", 1880, 905))
        got, why = bridge.relook(REGION, "开票", {"x": 1899, "y": 914})
        self.assertIsNone(got, "读到「红冲」时绝不能放行")
        self.assertIn("红冲", why, "要告诉调用方那里现在到底是什么")

    def test_refuses_when_region_is_empty(self):
        self._says()
        got, why = bridge.relook(REGION, "开票", {"x": 1899, "y": 914})
        self.assertIsNone(got)
        self.assertIn("已经不是", why)

    def test_refuses_when_the_button_jumped_far_away(self):
        """位置突变说明界面重构了, 旧坐标不可信。"""
        self._says(("开票", 100, 100))
        got, why = bridge.relook(REGION, "开票", {"x": 1899, "y": 914})
        self.assertIsNone(got)
        self.assertIn("位置变化过大", why)

    def test_tolerates_a_few_pixels_of_drift(self):
        """OCR 每次给的中心会差几个像素, 不能因此拒绝。"""
        self._says(("开票￥17.88", 1878, 907))
        got, _why = bridge.relook(REGION, "开票", {"x": 1899, "y": 914})
        self.assertIsNotNone(got)

    def test_picks_the_candidate_closest_to_the_expected_point(self):
        self._says(("开票", 400, 400), ("开票", 1885, 910))
        got, _why = bridge.relook(REGION, "开票", {"x": 1899, "y": 914})
        self.assertIsNotNone(got)
        self.assertLess(abs(got["x"] - 1899), 60, "应当选离原位置最近的那个")


class ClickTextRegressionTest(unittest.TestCase):
    """relook() 换掉了 best, 而 best 不在 solid 列表里 —— 曾因此抛 KeyError。

    按钮其实已经点出去了, 错误却发生在拼返回值的时候, 调用方看到的是失败。
    """

    def setUp(self):
        self._backend = bridge._BACKEND
        self._ocr = ocr_mod.ocr
        self._click = bridge.click_at
        bridge._BACKEND = FakeBackend()
        ocr_mod.ocr = lambda image: [ocr_mod.TextBlock("开票￥51.18", 1820, 900, 99, 22, 0.9)]
        self.clicked = []
        bridge.click_at = lambda *a, **k: self.clicked.append((a, k))

    def tearDown(self):
        bridge._BACKEND = self._backend
        ocr_mod.ocr = self._ocr
        bridge.click_at = self._click

    def test_returns_ok_and_normalized_after_relook(self):
        result = bridge.cmd_click_text({"text": "开票", "region": REGION})
        self.assertTrue(result.get("ok"), f"不该失败: {result}")
        self.assertEqual(len(result["normalized"]), 2,
                         "返回的归一化坐标不能是旧的/缺失的")
        self.assertEqual(len(self.clicked), 1, "应该正好点一次")

    def test_refusal_does_not_click(self):
        ocr_mod.ocr = lambda image: [ocr_mod.TextBlock("红冲", 1820, 900, 60, 22, 0.9)]
        result = bridge.cmd_click_text({"text": "开票", "region": REGION})
        self.assertFalse(result.get("ok"))
        self.assertEqual(self.clicked, [], "拒绝时一次都不能点")


class PickEmptyTest(unittest.TestCase):
    """同一个系统的"没得开"有好几种长相, 都要认出来。

    ABC数字医疗云: 队列空时可能弹「暂无可开发票」,
    也可能只是待开票列表里显示「暂无患者」, 或者按钮直接消失。
    """

    def _blocks(self, *texts):
        return [ocr_mod.TextBlock(t, 100, 100, 80, 22, 0.9) for t in texts]

    def test_matches_the_dialog_phrasing(self):
        got = bridge.pick_empty(self._blocks("暂无可开发票"),
                               ["暂无可开发票", "暂无患者"])
        self.assertEqual(got, "暂无可开发票")

    def test_matches_the_list_phrasing(self):
        got = bridge.pick_empty(self._blocks("暂无患者"),
                               ["暂无可开发票", "暂无患者"])
        self.assertEqual(got, "暂无患者")

    def test_returns_empty_when_nothing_matches(self):
        self.assertEqual(bridge.pick_empty(self._blocks("门诊收费", "待开票"), ["暂无"]), "")

    def test_tolerates_no_needles(self):
        self.assertEqual(bridge.pick_empty(self._blocks("暂无患者"), []), "")


if __name__ == "__main__":
    unittest.main()