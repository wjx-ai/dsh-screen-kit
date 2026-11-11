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


if __name__ == "__main__":
    unittest.main()