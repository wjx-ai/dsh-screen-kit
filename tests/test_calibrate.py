"""calibrate 的两条失败路径必须说清原因, 不能让新电脑上的人无从下手。"""
import unittest

from PIL import Image

import bridge
from screen import ocr as ocr_mod
from screen.base import Backend, Capabilities


class FakeBackend(Backend):
    name = "fake"

    def screen_bounds(self):
        return (0, 0, 1920, 1080)

    def capture(self, region=None):
        if region:
            _x, _y, w, h = region
            return Image.new("RGB", (int(w), int(h)), "white")
        return Image.new("RGB", (1920, 1080), "white")

    def key(self, name, down):
        pass                    # Esc 是可选的清理步骤, 没实现也不该炸

    def capabilities(self):
        return Capabilities(capture=True, input_mouse=True, input_keys=True)


class CalibrateTest(unittest.TestCase):
    def setUp(self):
        self._backend = bridge._BACKEND
        self._ocr = ocr_mod.ocr
        self._click = bridge.click_at
        bridge._BACKEND = FakeBackend()
        self.clicked = []
        bridge.click_at = lambda *a, **k: self.clicked.append(a)

    def tearDown(self):
        bridge._BACKEND = self._backend
        ocr_mod.ocr = self._ocr
        bridge.click_at = self._click

    def test_reports_empty_queue_without_clicking(self):
        """队列空的时候不能去点任何东西, 而且要说出看到了什么。"""
        ocr_mod.ocr = lambda image: [ocr_mod.TextBlock("暂无患者", 800, 400, 80, 22, 0.9)]
        result = bridge.cmd_calibrate({})
        self.assertTrue(result.get("ok"))
        self.assertFalse(result.get("calibrated"))
        self.assertIn("暂无患者", result["reason"])
        self.assertEqual(self.clicked, [], "队列空时一次都不能点")

    def test_explains_when_the_button_is_missing(self):
        """找不到按钮要说清楚找的是什么, 否则新电脑上的人不知道改哪里。"""
        ocr_mod.ocr = lambda image: [ocr_mod.TextBlock("门诊收费", 100, 300, 80, 22, 0.9)]
        result = bridge.cmd_calibrate({"top_text": "开票"})
        self.assertFalse(result.get("ok"))
        self.assertIn("开票", result["error"])
        self.assertIn("hint", result)
        self.assertEqual(self.clicked, [])


if __name__ == "__main__":
    unittest.main()
