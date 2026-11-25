# -*- coding: utf-8 -*-
"""枚举顶层窗口，找到 DGX 窗口并前置截图"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

u32 = ctypes.windll.user32
SW_RESTORE = 9
GW_OWNER = 4


def list_windows():
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, l):
        if not u32.IsWindowVisible(h):
            return True
        n = u32.GetWindowTextLengthW(h)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(h, buf, n + 1)
        title = buf.value
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if u32.GetWindow(h, GW_OWNER):
            return True
        r = wt.RECT()
        u32.GetWindowRect(h, ctypes.byref(r))
        if r.right - r.left < 400 or r.bottom - r.top < 300:
            return True
        out.append((h, pid.value, title, r.left, r.top,
                    r.right - r.left, r.bottom - r.top))
        return True
    u32.EnumWindows(cb, 0)
    return out


if __name__ == "__main__":
    ws = list_windows()
    print("可见顶层窗口 %d 个：" % len(ws))
    for h, pid, t, x, y, w, hh in ws:
        print("  pid=%-6d (%4d,%4d) %4dx%-4d  %s" % (pid, x, y, w, hh, t[:60]))

    tgt = [w for w in ws if "DGX" in w[2].upper()]
    if not tgt:
        print("\n未找到标题含 DGX 的窗口")
        sys.exit(1)
    h = tgt[0][0]
    print("\n目标窗口: %s" % tgt[0][2])
    # 还原（若最小化）+ 前置
    u32.ShowWindow(h, SW_RESTORE)
    time.sleep(0.4)
    u32.SetForegroundWindow(h)
    u32.BringWindowToTop(h)
    time.sleep(0.6)
    print("已前置，坐标 %d,%d %dx%d" % (tgt[0][3], tgt[0][4], tgt[0][5], tgt[0][6]))
