"""后端层的脱屏测试: 平台选择、按键白名单、错误信息。

这些在任何平台上都能跑 —— macOS/Linux 的具体实现没在真机上验证过,
所以这里的测试只锁"接口契约"和"出错时的提示是否可读", 不假装验证了那些平台。
"""
import sys
import unittest

from screen import base
from screen.base import Backend, BackendError, Capabilities


class PlatformDetectionTest(unittest.TestCase):
    def test_current_platform_is_known(self):
        self.assertIn(base.current_platform(), {"windows", "macos", "linux"})

    def test_unknown_platform_is_rejected_loudly(self):
        with self.assertRaises(BackendError) as ctx:
            base.load_backend("solaris")
        message = str(ctx.exception)
        self.assertIn("solaris", message)
        self.assertIn("windows", message)

    @unittest.skipUnless(sys.platform == "win32", "只在 Windows 上验证")
    def test_windows_backend_loads_and_reports_capabilities(self):
        be = base.load_backend("windows")
        caps = be.capabilities()
        self.assertTrue(caps.capture)
        self.assertTrue(caps.input_mouse)
        self.assertTrue(caps.windows)
        info = be.describe()
        self.assertEqual(info["backend"], "windows")

    @unittest.skipUnless(sys.platform == "win32", "只在 Windows 上验证")
    def test_screen_bounds_are_sane(self):
        be = base.load_backend("windows")
        x, y, width, height = be.screen_bounds()
        self.assertGreater(width, 100)
        self.assertGreater(height, 100)


class KeyWhitelistTest(unittest.TestCase):
    """只允许功能键。能打任意字符等于把往任何窗口输入的权限交出去。"""

    def test_known_aliases_normalize(self):
        self.assertEqual(base.normalize_key("ESC"), "esc")
        self.assertEqual(base.normalize_key("escape"), "esc")
        self.assertEqual(base.normalize_key("Return"), "enter")
        self.assertEqual(base.normalize_key(" Del "), "delete")

    def test_letters_and_text_are_rejected(self):
        for bad in ["a", "hello", "ctrl", "f5", ""]:
            with self.assertRaises(BackendError):
                base.normalize_key(bad)

    def test_error_lists_what_is_supported(self):
        with self.assertRaises(BackendError) as ctx:
            base.normalize_key("ctrl")
        self.assertIn("esc", str(ctx.exception))


class BaseContractTest(unittest.TestCase):
    def test_unimplemented_methods_explain_themselves(self):
        be = Backend()
        for call in (be.screen_bounds, be.capture, lambda: be.move(1, 2),
                     lambda: be.button("left", True), lambda: be.key("esc", True)):
            with self.assertRaises(BackendError):
                call()

    def test_base_reports_no_capabilities(self):
        self.assertFalse(Backend().capabilities().capture)

    def test_window_info_rect(self):
        win = base.WindowInfo(title="t", x=1, y=2, width=3, height=4)
        self.assertEqual(win.rect, (1, 2, 3, 4))
        self.assertEqual(win.as_dict()["rect"], [1, 2, 3, 4])

    def test_capabilities_round_trip(self):
        caps = Capabilities(capture=True, notes=["n"])
        self.assertEqual(caps.as_dict()["notes"], ["n"])


if __name__ == "__main__":
    unittest.main()