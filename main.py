# -*- coding: utf-8 -*-
"""
DGX Agent Studio · 程序入口
双击 / 安装后启动：先起本地 HTTP 服务，再开桌面窗口（pywebview）。
"""
import os
import sys


def _dpi_aware():
    """Windows 高分屏：让 tk/webview 拿到真实像素，避免发虚"""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PerMonitorV2
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main():
    _dpi_aware()
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from server.app import main as app_main
    app_main()


if __name__ == "__main__":
    main()
