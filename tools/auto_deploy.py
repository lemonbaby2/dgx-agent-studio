# -*- coding: utf-8 -*-
"""
auto_deploy.py · 每天 00:05 自动部署上线
====================================================================
两种模式，同一条流水线：

  A) 有新代码（把文件投到 inbox/staged/）
     → 备份 → 应用 staged → 校验 → 重启 → 健康检查 → 失败回滚

  B) 没新代码（大多数日子）
     → 备份 → 校验现有代码 → 重启 → 健康检查
       （目的：保证每天有一个"已知良好"的干净启动，清掉长期运行的隐性状态）

为什么必须"备份 + 校验 + 失败回滚"，不能只 restart：
  老师要求"保证网页还能用"。**部署是唯一能让它彻底挂掉的动作**，
  所以每个能失败的地方都要有退路：
    · 备份放 _deploy_history/<时间戳>/，保留最近 7 份
    · 校验分三层：py_compile（语法）→ 关键文件存在性 → 前端标记字符串
    · 重启后三层健康检查：/api/health → /api/capabilities → 静态页含 v8ui.js
    · 任一层失败：立刻从备份回滚 + 重启 + 再检查；仍失败写 ALERT.txt 停止折腾

投递方式（老师在任意机器上）：
  把改好的文件按相同相对路径放到
    ~/.dgx_studio/deploy_inbox/staged/<相对路径>
  再放开 ~/.dgx_studio/deploy_inbox/READY 空文件即可。
  处理完会自动改名为 applied_<时间戳>/，可以回查。

用法：
  python3 auto_deploy.py            # 正式执行
  python3 auto_deploy.py --dry-run  # 只校验，不重启
"""
from __future__ import annotations

import argparse
import json
import os
import py_compile
import shutil
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
APP = os.path.join(HOME, "DGX-Agent-Studio")
DATA = os.path.join(HOME, ".dgx_studio")
HIST = os.path.join(APP, "_deploy_history")
INBOX = os.path.join(DATA, "deploy_inbox")
STAGED = os.path.join(INBOX, "staged")
READY = os.path.join(INBOX, "READY")
LOG = os.path.join(DATA, "deploy.log")
ALERT = os.path.join(DATA, "ALERT.txt")
STATE = os.path.join(DATA, "deploy_state.json")

UNIT = "dgx-agent-studio.service"
BASE = "http://127.0.0.1:18910"
KEEP_BACKUPS = 7

# 备份范围（只备份这些东西，不要把 17GB 的 venv 之类带进来）
BACKUP_DIRS = ["server", "web", "tools", "skops", "deploy"]
KEY_FILES = ["web/index.html", "web/app.js", "web/v8ui.js", "web/v8.css"]
# 前端"没坏"的判据：这些字符串必须还在 index.html 里
FRONT_MARKERS = ["v8ui.js", 'data-view="embodied"', 'data-view="learn"']


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        with open(LOG, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > 800:
            with open(LOG, "w", encoding="utf-8") as f:
                f.writelines(lines[-800:])
    except Exception:
        pass


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(path, timeout=15):
    try:
        with _opener().open(BASE + path, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        return 0, {"error": str(e)[:200]}


def http_text(path, timeout=15):
    try:
        with _opener().open(BASE + path, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except Exception as e:
        return 0, str(e)[:200]


def systemctl(*args, timeout=120):
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    try:
        p = subprocess.run(["systemctl", "--user"] + list(args),
                           capture_output=True, text=True, timeout=timeout,
                           env=env, encoding="utf-8", errors="ignore")
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return -1, str(e)


# ---------------------------------------------------------------------------
# 1. 备份
# ---------------------------------------------------------------------------
def backup():
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(HIST, ts)
    os.makedirs(dst, exist_ok=True)
    n = 0
    for d in BACKUP_DIRS:
        src = os.path.join(APP, d)
        if not os.path.isdir(src):
            continue
        try:
            shutil.copytree(src, os.path.join(dst, d),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                          "*.log", ".venv"))
            n += 1
        except Exception as e:
            log("备份 %s 失败：%s" % (d, e))
    log("备份完成 -> %s（%d 个目录）" % (dst, n))
    _prune_backups()
    return dst


def _prune_backups():
    try:
        items = sorted(os.listdir(HIST))
        items = [x for x in items if os.path.isdir(os.path.join(HIST, x))]
        for old in items[:-KEEP_BACKUPS]:
            shutil.rmtree(os.path.join(HIST, old), ignore_errors=True)
            log("清理旧备份 %s" % old)
    except Exception as e:
        log("清理备份失败（忽略）：%s" % e)


# ---------------------------------------------------------------------------
# 2. 应用 staged
# ---------------------------------------------------------------------------
def has_staged():
    if not os.path.isfile(READY):
        return False
    if not os.path.isdir(STAGED):
        return False
    for root, _dirs, files in os.walk(STAGED):
        if files:
            return True
    return False


def apply_staged():
    """把 staged 下的文件按相对路径覆盖到 APP。返回应用的文件列表"""
    applied = []
    for root, _dirs, files in os.walk(STAGED):
        for fn in files:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, STAGED)
            # 安全：禁止跳出 APP
            if rel.startswith("..") or os.path.isabs(rel):
                log("跳过可疑路径 %s" % rel)
                continue
            dst = os.path.join(APP, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copy2(src, dst)
                applied.append(rel)
            except Exception as e:
                log("应用 %s 失败：%s" % (rel, e))
    log("staged 应用了 %d 个文件：%s" % (len(applied), ", ".join(applied[:12])))
    try:
        done = os.path.join(INBOX, "applied_%s" % time.strftime("%Y%m%d_%H%M%S"))
        os.rename(STAGED, done)
        if os.path.isfile(READY):
            os.remove(READY)
        log("staged 已归档 -> %s" % done)
    except Exception as e:
        log("归档 staged 失败：%s" % e)
    return applied


# ---------------------------------------------------------------------------
# 3. 校验
# ---------------------------------------------------------------------------
def verify_code():
    """返回 (ok, 详情列表)"""
    problems = []
    # 3.1 语法
    py_count = 0
    for root, _dirs, files in os.walk(APP):
        if any(x in root for x in ("_deploy_history", "__pycache__", ".git")):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn)
            try:
                py_compile.compile(p, doraise=True, cfile=p + ".pyc_tmp")
                py_count += 1
            except Exception as e:
                problems.append("语法错误 %s: %s" % (os.path.relpath(p, APP), e))
            finally:
                try:
                    os.remove(p + ".pyc_tmp")
                except Exception:
                    pass
    # 3.2 关键文件
    for rel in KEY_FILES:
        if not os.path.isfile(os.path.join(APP, rel)):
            problems.append("缺文件 %s" % rel)
        elif os.path.getsize(os.path.join(APP, rel)) < 200:
            problems.append("文件过小 %s" % rel)
    # 3.3 前端标记
    idx = os.path.join(APP, "web", "index.html")
    if os.path.isfile(idx):
        try:
            with open(idx, encoding="utf-8") as f:
                html = f.read()
            for mk in FRONT_MARKERS:
                if mk not in html:
                    problems.append("前端标记缺失 %r" % mk)
        except Exception as e:
            problems.append("读 index.html 失败：%s" % e)
    return (not problems), {"py_files": py_count, "problems": problems}


# ---------------------------------------------------------------------------
# 4. 健康检查
# ---------------------------------------------------------------------------
def health_check(wait=8):
    """三层检查，任何一层不过都算失败"""
    time.sleep(wait)
    detail = {}
    code, j = http_json("/api/health")
    detail["health_http"] = code
    detail["health_ok"] = bool(j.get("ok"))
    detail["ver"] = j.get("ver")
    if not (code == 200 and j.get("ok")):
        return False, detail

    code2, j2 = http_json("/api/capabilities")
    caps = (j2.get("caps") or {})
    # 核心能力必须全在（允许个别能力为 false，但 chat/fs/skills 不能丢）
    must = ["chat", "fs", "skills", "learn"]
    missing = [k for k in must if not caps.get(k)]
    detail["caps"] = {k: v for k, v in caps.items() if v}
    detail["caps_missing"] = missing
    if missing:
        return False, detail

    code3, html = http_text("/")
    detail["static_http"] = code3
    detail["static_bytes"] = len(html) if code3 == 200 else 0
    if code3 != 200 or "v8ui.js" not in (html or ""):
        return False, detail

    detail["ok"] = True
    return True, detail


# ---------------------------------------------------------------------------
# 5. 部署主流程
# ---------------------------------------------------------------------------
def restart_and_check():
    log("重启 %s" % UNIT)
    c, o = systemctl("restart", UNIT, timeout=120)
    log("restart rc=%d %s" % (c, o[:160]))
    ok, d = health_check(wait=8)
    log("健康检查：%s %s" % ("通过" if ok else "失败",
                             json.dumps(d, ensure_ascii=False)[:400]))
    return ok, d


def rollback(bak, why):
    log("=== 开始回滚（原因：%s）===" % why)
    for d in BACKUP_DIRS:
        src = os.path.join(bak, d)
        dst = os.path.join(APP, d)
        if not os.path.isdir(src):
            continue
        try:
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
            log("回滚 %s 完成" % d)
        except Exception as e:
            log("回滚 %s 失败：%s" % (d, e))
    ok, d = restart_and_check()
    if ok:
        log("回滚成功，服务已恢复")
        return True
    msg = ("%s  自动部署失败且回滚后仍不可用。\n"
           "原因：%s\n健康检查：%s\n\n人工排查：\n"
           "  1) tail -60 %s\n"
           "  2) systemctl --user status %s\n"
           "  3) tail -40 %s/run.log\n"
           "  4) 备份在 %s\n"
           % (time.strftime("%Y-%m-%d %H:%M:%S"), why,
              json.dumps(d, ensure_ascii=False), LOG, UNIT, APP, bak))
    try:
        with open(ALERT, "w", encoding="utf-8") as f:
            f.write(msg)
        log("已写 ALERT.txt")
    except Exception:
        pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只校验，不重启")
    a = ap.parse_args()

    t0 = time.time()
    os.makedirs(INBOX, exist_ok=True)
    os.makedirs(HIST, exist_ok=True)
    log("=== 自动部署开始%s ===" % ("（干跑）" if a.dry_run else ""))

    try:
        os.remove(ALERT)
    except Exception:
        pass

    res = {"ts": int(time.time()), "dry": a.dry_run, "steps": []}

    # -- 0 现状 --
    code, j = http_json("/api/health")
    res["before_ver"] = j.get("ver")
    res["before_ok"] = bool(j.get("ok"))
    log("部署前：ver=%s ok=%s" % (res["before_ver"], res["before_ok"]))

    # -- 1 备份 --
    bak = backup()
    res["backup"] = bak

    # -- 2 应用 staged（如果有）--
    staged = has_staged()
    res["staged"] = staged
    if staged:
        res["applied"] = apply_staged()
    else:
        log("没有 staged 代码，走「自检式重启」模式")

    # -- 3 校验 --
    ok, vd = verify_code()
    res["verify"] = vd
    log("代码校验：%s（%d 个 py）%s"
        % ("通过" if ok else "失败", vd["py_files"],
           "" if ok else " -> " + "; ".join(vd["problems"][:5])))
    if not ok:
        if staged:
            rollback(bak, "代码校验失败")
            res["result"] = "verify_failed_rolled_back"
        else:
            log("现有代码本身校验不过 —— 不重启，保持原状态")
            res["result"] = "verify_failed_no_change"
        res["elapsed"] = round(time.time() - t0, 1)
        _save_state(res)
        return 2

    if a.dry_run:
        log("干跑结束：校验通过，未重启。耗时 %ss" % round(time.time() - t0, 1))
        res["result"] = "dry_ok"
        res["elapsed"] = round(time.time() - t0, 1)
        _save_state(res)
        return 0

    # -- 4 重启 + 健康检查 --
    hok, hd = restart_and_check()
    res["health"] = hd
    if hok:
        res["result"] = "ok"
        log("=== 部署成功：ver=%s 耗时 %ss ==="
            % (hd.get("ver"), round(time.time() - t0, 1)))
    else:
        okb = rollback(bak, "健康检查失败")
        res["result"] = "rolled_back" if okb else "rollback_failed"
        log("=== 部署失败，%s ===" % ("已回滚" if okb else "回滚也失败"))

    res["elapsed"] = round(time.time() - t0, 1)
    _save_state(res)
    return 0 if res["result"] == "ok" else 3


def _save_state(res):
    try:
        hist = []
        if os.path.isfile(STATE):
            try:
                with open(STATE, encoding="utf-8") as f:
                    old = json.load(f)
                hist = old.get("history") or []
            except Exception:
                pass
        hist.append(res)
        obj = {"last": res, "history": hist[-30:]}
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE)
    except Exception as e:
        log("写部署状态失败：%s" % e)


if __name__ == "__main__":
    sys.exit(main())
