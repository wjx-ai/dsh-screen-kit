import sys
sys.path.insert(0, r'D:\dsh\dsh-screen-kit')
import bridge

print('=== ABC 窗口 ===')
for w in bridge.backend().windows():
    t = (w.title or '')
    if 'ABC' in t:
        print('  ', t[:34], w.as_dict()['rect'])

print('=== 关键文字 ===')
for needle in ('开票', '暂无患者', '暂无可开发票'):
    hits = [h for h in bridge.locate(needle, '', exact=True, max_tier=1) if not h['fuzzy']]
    print('  %s -> %d 处' % (needle, len(hits)))
    for h in hits[:3]:
        print('     档%d (%d,%d) %s' % (h['tier'], h['x'], h['y'], h['block'][:40]))
