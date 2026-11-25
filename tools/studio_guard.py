# -*- coding: utf-8 -*-
"""
studio_guard.py · 网页自愈守护（每 2 分钟由 systemd timer 触发）
====================================================================
保证 http://<dgx>:18910 一直能用。

判定顺序（照抄本机排障铁律：磁盘 → 进程 → 服务逻辑 → 前端取流）：
  1. 磁盘：/ 可用 < 2GB → 只告警不重启（重启也写不进日志）
  2. 端口：18910 有没有 LISTEN
  3. HTTP：/api/health 是否 200 且返回 ver
  4. 静态：/ 能否取到 index.html 且含 v8 标记（防「后端活着但前端被删/改坏」）

动作：
  · 连续 2 次失败才重启（避免偶发抖动导致频繁重启）
  · 重启后再探 15 秒，仍失败则计数 +1
  · 10 分钟内重启 3 次仍失败 → 写 ALERT.txt，并停止自动重启（防重启风暴）
  · 每次事件追加到 guard.log（保留 500 行）

为什么不用 sop-autorepair：那是生产脚本，管 CVAT/隧道；动它风险大。
这里独立一套，只管 18910，互不影响。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request

HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME, ".dgx_studio")
LOG = os.path.join(DATA_DIR, "guard.log")
STATE = os.path.join(DATA_DIR, "guard_state.json")
ALERT = os.path.join(DATA_DIR, "ALERT.txt")

UNIT = "dgx-agent-studio.service"
PORT = 18910
BASE = "http://127.0.0.1:%d" % PORT

FAIL_THRESHOLD = 2          # 连续失败几次才动手
MIN_FREE_GB = 2.0
RESTART_WINDOW = 600        # 10 分钟窗口
MAX_RESTARTS_IN_WINDOW = 3


# ---------------------------------------------------------------------------
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_json(p, fb):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fb


def _write_json(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def log(msg):
    line = "[%s] %s" % (_now(), msg)
    print(line, flush=True)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # 保留最近 500 行
        with open(LOG, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > 500:
            with open(LOG, "w", encoding="utf-8") as f:
                f.writelines(lines[-500:])
    except Exception:
        pass


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------
def check_disk():
    try:
        u, t, free = shutil.disk_usage("/")
        gb = free / (1024 ** 3)
        return {"ok": gb >= MIN_FREE_GB, "free_gb": round(gb, 2)}
    except Exception as e:
        return {"ok": True, "error": str(e)[:100]}


def check_port():
    try:
        p = subprocess.run(["ss", "-lnt"], capture_output=True, text=True,
                           timeout=10)
        return {"ok": (":%d" % PORT) in (p.stdout or "")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


def check_health(timeout=6):
    try:
        with _opener().open(BASE + "/api/health", timeout=timeout) as r:
            if r.status != 200:
                return {"ok": False, "http": r.status}
            d = json.loads(r.read().decode("utf-8", "ignore"))
            return {"ok": bool(d.get("ok")), "ver": d.get("ver"),
                    "app": d.get("app")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


def check_static(timeout=8):
    """后端活着但前端坏掉是最隐蔽的故障。

    v12 起 "/" 对未登录者返回登录页（约 4.9KB），旧探针（找 v8ui.js 字样）
    必然失败并触发重启风暴。改为直接取免鉴权的静态资源做探针。
    """
    try:
        with _opener().open(BASE + "/v8ui.js", timeout=timeout) as r:
            js = r.read().decode("utf-8", "ignore")
        ok = len(js) > 20000 and ("function" in js)
        return {"ok": ok, "bytes": len(js), "probe": "/v8ui.js"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "probe": "/v8ui.js"}


def systemctl(*args, timeout=60):
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    try:
        p = subprocess.run(["systemctl", "--user"] + list(args),
                           capture_output=True, text=True, timeout=timeout,
                           env=env, encoding="utf-8", errors="ignore")
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return -1, str(e)


def restart_unit():
    log("执行重启 %s" % UNIT)
    c1, o1 = systemctl("restart", UNIT, timeout=90)
    time.sleep(6)
    c2, o2 = systemctl("is-active", UNIT, timeout=20)
    log("重启返回 code=%d %s | is-active=%s" % (c1, o1[:120], o2))
    return c1 == 0 and o2.strip() == "active"


# ---------------------------------------------------------------------------
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    st = _read_json(STATE, {"fails": 0, "restarts": [], "alerts": 0})

    disk = check_disk()
    port = check_port()
    health = check_health()
    static = check_static() if health.get("ok") else {"ok": False, "skipped": True}

    all_ok = disk["ok"] and port["ok"] and health["ok"] and static["ok"]

    if all_ok:
        if st.get("fails") or st.get("degraded"):
            log("恢复正常（ver=%s）" % health.get("ver"))
        if os.path.exists(ALERT):
            try:
                os.remove(ALERT)
            except Exception:
                pass
        _write_json(STATE, {"fails": 0, "restarts": st.get("restarts") or [],
                            "alerts": 0, "last_ok": int(time.time()),
                            "last_ver": health.get("ver")})
        return 0

    # 失败：累积
    st["fails"] = int(st.get("fails") or 0) + 1
    detail = "disk=%s port=%s health=%s static=%s" % (
        disk, port, health, static)
    log("探测失败 #%d | %s" % (st["fails"], detail[:300]))

    if not disk["ok"]:
        log("磁盘可用仅 %.2fGB —— 不重启（写了日志也存不下），只告警"
            % disk.get("free_gb", 0))
        st["degraded"] = "disk_low"
        _write_json(STATE, st)
        return 2

    if st["fails"] < FAIL_THRESHOLD:
        _write_json(STATE, st)
        log("未达重启阈值（%d/%d），下轮再看" % (st["fails"], FAIL_THRESHOLD))
        return 0

    # 重启风暴保护
    now = time.time()
    rl = [t for t in (st.get("restarts") or []) if now - t < RESTART_WINDOW]
    if len(rl) >= MAX_RESTARTS_IN_WINDOW:
        msg = ("%s  %s 在 %d 分钟内已重启 %d 次仍不可用。\n"
               "最后探测：%s\n\n人工排查顺序：\n"
               "  1) systemctl --user status %s\n"
               "  2) tail -50 ~/DGX-Agent-Studio/run.log\n"
               "  3) tail -30 ~/.dgx_studio/guard.log\n"
               "  4) cat ~/.dgx_studio/server.json   # 端口是否被改\n"
               "  5) df -h /                          # 磁盘\n"
               % (_now(), UNIT, RESTART_WINDOW // 60, len(rl), detail, UNIT))
        try:
            with open(ALERT, "w", encoding="utf-8") as f:
                f.write(msg)
        except Exception:
            pass
        log("已停止自动重启（重启风暴保护），写 ALERT.txt")
        st["degraded"] = "restart_storm"
        st["restarts"] = rl
        _write_json(STATE, st)
        return 3

    ok = restart_unit()
    st["restarts"] = rl + [now]
    if ok:
        h2 = check_health()
        log("重启后 health=%s" % h2)
        if h2.get("ok"):
            st["fails"] = 0
            st.pop("degraded", None)
        else:
            log("重启成功但 health 仍失败")
    else:
        log("重启失败")
    _write_json(STATE, st)
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
