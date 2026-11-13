"""看一眼现场: 自助机窗口文字 + 模式。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bridge  # noqa: E402

for label, region in [('自助机窗口', '140,215,510,540')]:
    image, ox, oy = bridge.grab(region)
    blocks = bridge.ocr_mod.ocr(image)
    print('===', label, '===')
    for b in blocks:
        print('   %4d %4d  %s' % (b.y + oy, b.x + ox, b.text[:76]))

print()
print('=== 窗口列表 ===')
for w in bridge.backend().windows():
    t = (w.title or '')[:44]
    if t.strip():
        print('  ', t, '|', w.as_dict()['rect'])