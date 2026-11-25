# -*- coding: utf-8 -*-
"""窗口端到端验证：启动 → 前置 → 截图 → 关闭，全在一个进程内完成"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = r"C:\Users\lzp\AppData\Local\Programs\Python\Python313\python.exe"
LOG = os.path.join(HERE, "_win_e2e.log")
SHOT = os.path.join(HERE, "_win_e2e.png")

u32 = ctypes.windll.user32
SW_RESTORE = 9
GW_OWNER = 4


def find_windows(title_kw):
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
        t = buf.value
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if u32.GetWindow(h, GW_OWNER):
            return True
        r = wt.RECT()
        u32.GetWindowRect(h, ctypes.byref(r))
        if r.right - r.left < 300 or r.bottom - r.top < 200:
            return True
        out.append((h, pid.value, t, r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True
    u32.EnumWindows(cb, 0)
    return [w for w in out if title_kw.upper() in w[2].upper()]


def class_of(h):
    buf = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(h, buf, 256)
    return buf.value


def occluders(my_rect, my_pid, min_overlap=0.25):
    """找出与我的窗口明显重叠的可见顶层窗口（用于临时最小化后截图）。
    严格排除：我自己的窗口、桌面(Progman/WorkerW)、系统壳窗口。"""
    mx, my, mw, mh = my_rect
    out = []
    SKIP_CLS = ("progman", "workerw", "windows.ui.core", "applicationframewindow",
                "windowsforms10", "msedgewebview2", "chrome_widgetwin",
                "ime", "msctfimeui")

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, l):
        if not u32.IsWindowVisible(h) or u32.IsIconic(h):
            return True
        if u32.GetWindow(h, GW_OWNER):
            return True
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == my_pid:
            return True
        cls = class_of(h)
        if any(s in cls.lower() for s in SKIP_CLS):
            return True
        r = wt.RECT()
        u32.GetWindowRect(h, ctypes.byref(r))
        w, hh = r.right - r.left, r.bottom - r.top
        if w < 300 or hh < 200:
            return True
        ox = max(0, min(mx + mw, r.right) - max(mx, r.left))
        oy = max(0, min(my + mh, r.bottom) - max(my, r.top))
        if ox * oy > mw * mh * min_overlap:
            out.append((h, cls, (r.left, r.top, w, hh)))
        return True
    u32.EnumWindows(cb, 0)
    return out


def main():
    # 与 main.py 保持同一 DPI 感知，坐标才是物理像素
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("ELECTRON_RUN_AS_NODE", None)

    # TARGET 指定启动目标：默认 main.py，也可指向打包后的 exe
    target = os.environ.get("TARGET") or os.path.join(HERE, "main.py")
    if target.endswith(".exe"):
        cmd = [target]
    else:
        cmd = [PY, target]

    with open(LOG, "w", encoding="utf-8") as lf:
        p = subprocess.Popen(cmd, stdout=lf, stderr=lf, env=env,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    print("已启动 %s PID=%d" % (os.path.basename(target), p.pid))

    # 等窗口出现（最多 25 秒）
    win = None
    for i in range(50):
        time.sleep(0.5)
        ws = find_windows("DGX Agent Studio")
        if ws:
            win = ws[0]
            break
    if not win:
        print("[FAIL] 25 秒内未出现标题含 'DGX Agent Studio' 的窗口")
        print("--- 日志 ---")
        print(open(LOG, encoding="utf-8", errors="ignore").read()[-2000:])
        p.kill()
        return 1

    h, pid, title, x, y, w, hh = win
    print("[OK] 窗口已创建：%s" % title)
    print("     pid=%d  位置=(%d,%d)  尺寸=%dx%d" % (pid, x, y, w, hh))

    u32.ShowWindow(h, SW_RESTORE)
    time.sleep(0.3)

    # Windows 不允许非前台进程抢焦点；按一下 Alt 可绕过该限制
    if not os.environ.get("SKIP_ALT"):
        VK_MENU = 0x12
        u32.keybd_event(VK_MENU, 0, 0, 0)
        u32.keybd_event(VK_MENU, 0, 2, 0)
        print("     （已按 Alt 绕过焦点限制）")
    else:
        print("     （SKIP_ALT：未按 Alt）")
    u32.SetForegroundWindow(h)
    u32.BringWindowToTop(h)
    # 临时置顶，确保不被其它窗口盖住
    HWND_TOPMOST = -1
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
    u32.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    time.sleep(0.5)

    # 等 WebView2 把页面渲染完（后台窗口会暂停合成，必须等可见后）
    from PIL import ImageGrab
    im = None
    for i in range(24):
        time.sleep(0.5)
        im = ImageGrab.grab(bbox=(x, y, x + w, y + hh))
        px = im.convert("RGB").load()
        top = px[im.width // 2, 8]
        main_area = px[min(900, im.width - 1), 200]
        # 顶栏白 + 主区浅 = 页面已合成
        if sum(top) > 600 and sum(main_area) > 600:
            break
    print("[OK] 页面渲染完成（等待 %.1fs）" % (1.0 + i * 0.5))

    # 临时最小化遮挡窗口，只为截图干净；拍完立刻恢复原位
    SW_MINIMIZE = 6
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    SWP_FRAMECHANGED = 0x0020
    hidden = []
    for oh, cls, orect in occluders((x, y, w, hh), pid):
        try:
            top = u32.GetWindowLongW(oh, -20)   # GWL_EXSTYLE
            ex_top = bool(top & 0x00000008)     # WS_EX_TOPMOST
            u32.SetWindowPos(oh, HWND_NOTOPMOST, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            u32.ShowWindow(oh, SW_MINIMIZE)
            hidden.append((oh, cls, orect, ex_top))
            print("     临时最小化遮挡窗口：%s %s" % (cls, orect))
        except Exception as e:
            print("     跳过窗口 %s：%s" % (cls, e))
    time.sleep(1.2)

    im = ImageGrab.grab(bbox=(x, y, x + w, y + hh))
    im.save(SHOT)
    print("[OK] 已截图：%s  (%dx%d)" % (SHOT, im.width, im.height))

    # 恢复
    for oh, cls, orect, ex_top in hidden:
        try:
            u32.ShowWindow(oh, SW_RESTORE)
            u32.SetWindowPos(oh, HWND_TOPMOST if ex_top else HWND_NOTOPMOST,
                             orect[0], orect[1], orect[2], orect[3],
                             SWP_SHOWWINDOW)
            print("     已恢复：%s" % cls)
        except Exception as e:
            print("     恢复失败 %s：%s" % (cls, e))

    px = im.convert("RGB").load()
    samples = {
        "侧栏(应深色)": px[min(30, im.width - 1), im.height // 2],
        "顶栏(应白色)": px[im.width // 2, 6],
        "主区(应浅色)": px[min(900, im.width - 1), 200],
    }
    for k, v in samples.items():
        print("     %s -> RGB%s" % (k, v))

    p.kill()
    time.sleep(1)
    print("[OK] 已关闭测试实例（PID=%d）" % p.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
