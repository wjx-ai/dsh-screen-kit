"""坐标换算的回归测试 —— 锁住"区域截图丢了原点"那个 bug。

真实事故: 想点工具栏 (1661, 88) 的「开票」, 结果点到了 (101, 39)。
原因是 grab() 走"只截那一块"的快路径时把区域原点返回成 (0,0),
裁剪图内的坐标被当成了全屏坐标。
"""
import unittest

from PIL import Image

import bridge
from screen.base import Backend, BackendError, Capabilities


class FakeBackend(Backend):
    """可以正常截取区域的假后端: 1920x1080, 区域截图返回对应大小的图。"""

    name = "fake"
    supports_region = True

    def __init__(self, width=1920, height=1080, region_ok=True):
        self.width = width
        self.height = height
        self.region_ok = region_ok
        self.calls = []

    def screen_bounds(self):
        return (0, 0, self.width, self.height)

    def capture(self, region=None):
        self.calls.append(region)
        if region and not self.region_ok:
            raise BackendError("这个后端不支持区域截图")
        if region:
            _x, _y, w, h = region
            return Image.new("RGB", (int(w), int(h)), "white")
        return Image.new("RGB", (self.width, self.height), "white")

    def capabilities(self):
        return Capabilities(capture=True)


class GrabOffsetTest(unittest.TestCase):
    def setUp(self):
        self._saved = bridge._BACKEND

    def tearDown(self):
        bridge._BACKEND = self._saved

    def test_region_capture_returns_the_region_origin(self):
        """区域原点必须原样返回, 否则所有坐标都会偏。"""
        fake = FakeBackend()
        bridge._BACKEND = fake
        image, ox, oy = bridge.grab("500,500,600,600")
        # 1920*0.5=960, 1080*0.5=540
        self.assertEqual((ox, oy), (960, 540))
        self.assertEqual(image.size, (192, 108))

    def test_zero_origin_region_is_still_correct(self):
        bridge._BACKEND = FakeBackend()
        _image, ox, oy = bridge.grab("0,0,1000,280")
        self.assertEqual((ox, oy), (0, 0))

    def test_region_capture_starts_at_the_right_pixel(self):
        fake = FakeBackend()
        bridge._BACKEND = fake
        bridge.grab("813,46,917,120")
        self.assertEqual(len(fake.calls), 1, "应该只截一次")
        rx, ry, rw, rh = fake.calls[0]
        self.assertEqual((rx, ry), (1560, 49))
        self.assertEqual((rw, rh), (199, 79))

    def test_falls_back_to_full_capture_when_region_unsupported(self):
        fake = FakeBackend(region_ok=False)
        bridge._BACKEND = fake
        image, ox, oy = bridge.grab("500,500,600,600")
        self.assertEqual((ox, oy), (960, 540), "退回全屏裁剪时偏移也要对")
        self.assertEqual(image.size, (192, 108))

    def test_full_screen_grab_has_no_offset(self):
        bridge._BACKEND = FakeBackend()
        image, ox, oy = bridge.grab("")
        self.assertEqual((ox, oy), (0, 0))
        self.assertEqual(image.size, (1920, 1080))


class RegionPxTest(unittest.TestCase):
    def test_region_px_crops_and_reports_offset(self):
        image = Image.new("RGB", (1920, 1080), "white")
        crop, ox, oy = bridge.region_px(image, "500,500,600,600")
        self.assertEqual((ox, oy), (960, 540))
        self.assertEqual(crop.size, (192, 108))

    def test_region_px_rejects_malformed_input(self):
        image = Image.new("RGB", (100, 100), "white")
        with self.assertRaises(ValueError):
            bridge.region_px(image, "1,2,3")

    def test_region_px_rejects_empty_box(self):
        image = Image.new("RGB", (100, 100), "white")
        with self.assertRaises(ValueError):
            bridge.region_px(image, "500,500,500,600")


if __name__ == "__main__":
    unittest.main()