"""一条命令开票: 自动探出区域, 然后连续开票。

用法 (在新电脑上, clone 下来装好依赖就能直接用):

    python invoice.py                # 校准 + 开票, 开到最后一张
    python invoice.py --dry-run      # 只校准, 不开票 (先确认区域对不对)
    python invoice.py --limit 20     # 最多开 20 张
    python invoice.py --interval 2   # 每张之间停 2 秒 (默认 2)

为什么要有这个文件:
  区域是绝对像素, 换台电脑分辨率/缩放/窗口位置全变。让人手填坐标是
  "新机器上跑不起来"的头号原因, 所以把"校准"和"开票"合成一条命令。

校准会真的点一下工具栏按钮并等弹窗出现, 然后按 Esc 取消 —— 不会开出发票。
如果连一张都开不出来 (比如队列已经空了), 它会直接说清楚, 不会瞎点。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bridge  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="自动校准区域 + 连续开票",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--top-text", default="开票",
                        help="工具栏上那个按钮的文字 (默认 开票)")
    parser.add_argument("--popup-text", default=None,
                        help="弹窗里那个按钮的文字 (默认与 top-text 相同)")
    parser.add_argument("--empty-text", action="append", default=None,
                        help="看到这个就说明没得开了, 可重复; "
                             "默认 暂无可开发票 + 暂无患者")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="每张之间停几秒 (默认 2)")
    parser.add_argument("--limit", type=int, default=1000,
                        help="最多开几张 (默认 1000)")
    parser.add_argument("--wait-limit", type=float, default=45.0,
                        help="等弹窗出现最多几秒 (默认 45; 电子票据联网取数据会慢)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校准并报告区域, 不开票")
    parser.add_argument("--json", action="store_true",
                        help="只输出 JSON (给程序用)")
    args = parser.parse_args()

    empty = args.empty_text or ["暂无可开发票", "暂无患者"]

    calibration = bridge.cmd_calibrate({
        "top_text": args.top_text,
        "popup_text": args.popup_text or args.top_text,
        "empty_text": empty,
        "wait_limit": args.wait_limit,
    })
    if args.json:
        print(json.dumps(calibration, ensure_ascii=False))
    else:
        print(json.dumps(calibration, ensure_ascii=False, indent=2))

    if not calibration.get("calibrated"):
        # 队列空是正常结果 (退出码 0), 找不到按钮才是问题 (退出码 1)
        return 0 if calibration.get("ok") else 1

    if args.dry_run:
        print("dry-run: 只校准, 没有开票", file=sys.stderr)
        return 0

    batch_args = dict(calibration["batch_args"])
    batch_args["times"] = args.limit
    batch_args["interval"] = args.interval
    batch_args["wait_limit"] = args.wait_limit

    result = bridge.cmd_batch(batch_args)
    summary = {"done": result.get("done"), "stopped": result.get("stopped")}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
