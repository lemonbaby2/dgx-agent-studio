#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
isaac_launch.py · 用 pip 版 Isaac Sim 起一个 GUI/headless 仿真会话
=================================================================
被 embodied_api.isaac_start() 通过 subprocess 调用，跑在 ~/isaacsim-venv 里。

为什么单独成文件：Isaac 启动必须先进 SimulationApp 上下文，
无法用 `python -c` 简短表达（且项目铁律禁止远程内联 python -c）。

用法：
    python isaac_launch.py [--headless] [--usd /path/scene.usd] [--seconds 0]
退出：0 正常；4 缺 isaacsim；5 启动异常
"""
import argparse
import os
import sys
import time

# ── Isaac Sim 6.2 在 aarch64 上必须的三件套 ────────────────────────────
# 不设 LD_PRELOAD：import 时只打印一段 WARNING 然后**直接 exit**，
#                 没有异常、没有 traceback，表现像「什么都没发生」
# 不设 EULA：卡在 "Do you accept the EULA? (Yes/No):" 死等 stdin
# 已经在外层设了就保留，不覆盖用户值
os.environ.setdefault("LD_PRELOAD", "/lib/aarch64-linux-gnu/libgomp.so.1")
os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
os.environ.setdefault("ACCEPT_EULA", "Y")
os.environ.setdefault("PRIVACY_CONSENT", "Y")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true", help="不开窗口")
    ap.add_argument("--usd", default="", help="打开指定 USD 场景")
    ap.add_argument("--seconds", type=float, default=0,
                    help="运行 N 秒后自动退出（0=常驻）")
    ap.add_argument("--livestream", type=int, default=0,
                    help="开 WebRTC livestream 端口（0=不开）")
    a = ap.parse_args()

    try:
        from isaacsim import SimulationApp
    except Exception as e:
        print("ISAACSIM_MISSING: %s" % e, flush=True)
        return 4

    launch = {
        "headless": bool(a.headless),
        "renderer": "RayTracedLighting",
        "width": 1280,
        "height": 720,
    }
    if a.livestream:
        launch["livestream"] = 2          # 2 = WebRTC
    print("ISAAC_LAUNCH %s" % launch, flush=True)

    try:
        app = SimulationApp(launch)
    except Exception as e:
        print("ISAAC_BOOT_FAIL: %s" % e, flush=True)
        return 5

    print("ISAAC_READY", flush=True)

    # 打开场景（如果有）
    if a.usd:
        try:
            import omni.usd
            omni.usd.get_context().open_stage(a.usd)
            print("ISAAC_STAGE_OPENED %s" % a.usd, flush=True)
        except Exception as e:
            print("ISAAC_STAGE_FAIL: %s" % e, flush=True)

    t0 = time.time()
    try:
        while True:
            app.update()
            if a.seconds and (time.time() - t0) > a.seconds:
                print("ISAAC_AUTO_EXIT after %.0fs" % a.seconds, flush=True)
                break
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            app.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
