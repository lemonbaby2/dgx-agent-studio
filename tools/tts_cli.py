#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tts_cli.py · 单段文本 → mp3（edge-tts）
=========================================================
被 paper_reader.py 通过 subprocess 调用，跑在 ~/.ttsvenv 里。
单独成文件而不是 python -c，是因为：
  · 长文本内联进 -c 会撞 shell 长度与转义问题
  · 出错时能拿到干净的 traceback

用法：
    python tts_cli.py --text-file /tmp/x.txt --voice zh-CN-XiaoxiaoNeural \
                      --rate 0 --out /tmp/x.mp3
退出码：0 成功；2 参数错；3 合成失败
"""
import argparse
import asyncio
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-file", required=True)
    ap.add_argument("--voice", default="zh-CN-XiaoxiaoNeural")
    ap.add_argument("--rate", type=int, default=0, help="语速百分比，如 +10 / -10")
    ap.add_argument("--volume", type=int, default=0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    try:
        with open(a.text_file, encoding="utf-8") as f:
            text = f.read().strip()
    except Exception as e:
        print("读取文本失败：%s" % e, file=sys.stderr)
        return 2
    if not text:
        print("文本为空", file=sys.stderr)
        return 2

    try:
        import edge_tts
    except Exception as e:
        print("edge_tts 不可用：%s" % e, file=sys.stderr)
        return 3

    rate = "%+d%%" % a.rate if a.rate else "+0%"
    volume = "%+d%%" % a.volume if a.volume else "+0%"

    async def _go():
        comm = edge_tts.Communicate(text, a.voice, rate=rate, volume=volume)
        await comm.save(a.out)

    try:
        asyncio.run(_go())
    except Exception as e:
        print("合成失败：%s" % e, file=sys.stderr)
        return 3
    if not os.path.isfile(a.out) or os.path.getsize(a.out) == 0:
        print("输出为空", file=sys.stderr)
        return 3
    print("OK %d" % os.path.getsize(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
