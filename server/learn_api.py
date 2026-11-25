# -*- coding: utf-8 -*-
"""
learn_api · v9 自学习接口
====================================================================
把 tools/self_learn.py 的执行结果暴露给前端。

设计：
  · run() 是异步的（LLM 跑一次 1-5 分钟），起线程 + 轮询，不阻塞 HTTP
  · 同一时刻只允许一个学习任务（用模块级锁），避免重复烧 token
  · status() 只读文件，极快，前端可以频繁轮询
  · 技能数分「内置」和「自动生成」两类统计 —— 老师要的就是看自动生成在涨
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import threading
import time

HOME = os.path.expanduser("~")
APP_DIR = os.path.join(HOME, "DGX-Agent-Studio")
DATA_DIR = os.path.join(HOME, ".dgx_studio")
LEARN_DIR = os.path.join(DATA_DIR, "learning")
SEEN = os.path.join(LEARN_DIR, "seen.json")
LATEST = os.path.join(LEARN_DIR, "latest.json")
AUTO_SKOPS = os.path.join(DATA_DIR, "skops", "autolearn")
BUILTIN_SKOPS = os.path.join(APP_DIR, "skops")
KB_DIR = os.path.join(HOME, "DGX-Notes", "kb")
SCRIPT = os.path.join(APP_DIR, "tools", "self_learn.py")

_LOCK = threading.Lock()
_JOB = {"running": False, "started": 0, "finished": 0, "result": None,
        "log": [], "error": ""}


def _read_json(p, fb):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fb


def _count_yaml(d):
    if not os.path.isdir(d):
        return 0
    return len([f for f in os.listdir(d) if f.endswith((".yaml", ".yml"))])


def _count_kb():
    if not os.path.isdir(KB_DIR):
        return 0, 0
    files = [f for f in os.listdir(KB_DIR) if f.endswith(".md")]
    total = 0
    for f in files:
        try:
            total += os.path.getsize(os.path.join(KB_DIR, f))
        except Exception:
            pass
    return len(files), total


def _next_run():
    """推算下次定时时间（每天 00:00 Asia/Shanghai）"""
    try:
        nm = time.localtime(time.time() + 86400)
        target = time.mktime((nm.tm_year, nm.tm_mon, nm.tm_mday, 0, 0, 0, 0, 0, -1))
        return int(target)
    except Exception:
        return 0


def status():
    latest = _read_json(LATEST, {}) or {}
    seen = _read_json(SEEN, {}) or {}
    n_kb, kb_bytes = _count_kb()
    auto = _count_yaml(AUTO_SKOPS)
    builtin = _count_yaml(BUILTIN_SKOPS)
    auto_list = []
    if os.path.isdir(AUTO_SKOPS):
        for f in sorted(os.listdir(AUTO_SKOPS)):
            if not f.endswith((".yaml", ".yml")):
                continue
            p = os.path.join(AUTO_SKOPS, f)
            name = sid = ""
            trig = []
            try:
                with open(p, encoding="utf-8") as fh:
                    txt = fh.read()
                m = re.search(r"^id:\s*(\S+)", txt, re.M)
                sid = m.group(1) if m else os.path.splitext(f)[0]
                m = re.search(r"^name:\s*(.+)$", txt, re.M)
                name = (m.group(1).strip().strip('"') if m else sid)
                m = re.search(r"^trigger:\s*\[(.*?)\]", txt, re.M)
                if m:
                    trig = [x.strip().strip('"') for x in m.group(1).split(",")
                            if x.strip()]
            except Exception:
                pass
            auto_list.append({"id": sid, "name": name, "file": f,
                              "trigger": trig[:6],
                              "mtime": time.strftime(
                                  "%Y-%m-%d %H:%M",
                                  time.localtime(os.path.getmtime(p)))})
    return {
        "ok": True,
        "last": latest,
        "learned_messages": len(seen.get("fps") or []),
        "seen_updated": seen.get("updated") or 0,
        "kb_files": n_kb,
        "kb_bytes": kb_bytes,
        "skills_builtin": builtin,
        "skills_auto": auto,
        "auto_skills": auto_list,
        "next_run": _next_run(),
        "running": _JOB["running"],
        "job": {"running": _JOB["running"], "started": _JOB["started"],
                "finished": _JOB["finished"], "error": _JOB["error"],
                "tail": _JOB["log"][-30:]},
        "paths": {"kb": KB_DIR, "auto_skops": AUTO_SKOPS,
                  "reports": LEARN_DIR, "script": SCRIPT},
    }


def _run_thread(args):
    global _JOB
    t0 = time.time()
    try:
        p = subprocess.Popen(["/usr/bin/python3", SCRIPT] + list(args),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="ignore",
                             cwd=APP_DIR, bufsize=1)
        # ⚠️ 不能 `for line in p:` —— Popen 对象不可迭代（那是 subprocess.run 的行为），
        #    会抛 TypeError: 'Popen' object is not iterable。
        #    而子进程其实已经起来了，于是「日志全空、任务看着秒结束、实际后台还在跑」，
        #    极难排查。必须显式迭代 p.stdout。
        if p.stdout is not None:
            for line in p.stdout:
                line = (line or "").rstrip()
                if line:
                    _JOB["log"].append(line)
                    if len(_JOB["log"]) > 400:
                        _JOB["log"] = _JOB["log"][-400:]
        p.wait()
        _JOB["result"] = _read_json(LATEST, {}) or {}
        if p.returncode != 0:
            _JOB["error"] = "退出码 %d（详见日志）" % p.returncode
    except Exception as e:
        _JOB["error"] = str(e)[:300]
    finally:
        _JOB["running"] = False
        _JOB["finished"] = int(time.time())
        _JOB["elapsed"] = round(time.time() - t0, 1)


def run(force=False, dry_run=False, min_chars=120):
    if not os.path.isfile(SCRIPT):
        return {"ok": False, "error": "缺 tools/self_learn.py"}
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有学习任务在跑，等它结束"}
    try:
        if _JOB["running"]:
            return {"ok": False, "error": "已有学习任务在跑"}
        args = []
        if force:
            args.append("--force")
        if dry_run:
            args.append("--dry-run")
        if min_chars:
            args += ["--min-chars", str(int(min_chars))]
        _JOB.update({"running": True, "started": int(time.time()),
                     "finished": 0, "result": None, "log": [], "error": ""})
        th = threading.Thread(target=_run_thread, args=(args,), daemon=True)
        th.start()
        return {"ok": True, "started": int(time.time()),
                "note": "已启动，轮询 /api/learn/status 看进度"}
    finally:
        _LOCK.release()


def history(limit=30):
    """学习历史。注意要排除 latest.json 和 seen.json —— 后者是去重指纹，
    不是报告，混进来会显示成一条 date=none 的空记录。"""
    items = []
    SKIP = {"latest.json", "seen.json"}
    if os.path.isdir(LEARN_DIR):
        for f in sorted(os.listdir(LEARN_DIR), reverse=True):
            if not f.endswith(".json") or f in SKIP:
                continue
            if not re.match(r"^\d{4}-\d{2}-\d{2}\.json$", f):
                continue        # 只认 YYYY-MM-DD.json
            p = os.path.join(LEARN_DIR, f)
            d = _read_json(p, {}) or {}
            runs = d.get("runs") or []
            last = runs[-1] if runs else {}
            items.append({
                "date": d.get("date") or f.replace(".json", ""),
                "runs": len(runs),
                "insights": last.get("insights", 0),
                "skills": len(last.get("skills") or []),
                "kb": len(last.get("kb") or []),
                "status": last.get("status", ""),
                "elapsed": last.get("elapsed", 0),
                "mtime": time.strftime("%Y-%m-%d %H:%M",
                                       time.localtime(os.path.getmtime(p))),
            })
            if len(items) >= limit:
                break
    return {"ok": True, "history": items}


def report(date=None):
    if not date:
        return {"ok": True, "report": _read_json(LATEST, {}) or {}}
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {"ok": False, "error": "date 格式应为 YYYY-MM-DD"}
    p = os.path.join(LEARN_DIR, date + ".json")
    return {"ok": True, "report": _read_json(p, {}) or {}}


def kb_list(limit=50):
    items = []
    if os.path.isdir(KB_DIR):
        for f in sorted(os.listdir(KB_DIR), reverse=True):
            if not f.endswith(".md"):
                continue
            p = os.path.join(KB_DIR, f)
            items.append({"name": f, "path": p,
                          "bytes": os.path.getsize(p),
                          "mtime": time.strftime(
                              "%Y-%m-%d %H:%M",
                              time.localtime(os.path.getmtime(p)))})
            if len(items) >= limit:
                break
    return {"ok": True, "items": items, "dir": KB_DIR}


def read_kb(name_or_path, max_bytes=400_000):
    """读知识库文件。

    ⚠️ 不能复用 fs_api.read_text —— 那个把路径约束在「工作区」内，
    而知识库在 ~/DGX-Notes/kb，默认不在任何工作区里，会直接报"路径不在工作区内"。
    这里用白名单：只允许 KB_DIR 直接子目录下的 .md。
    """
    if not name_or_path:
        return {"ok": False, "error": "缺少文件"}
    name = os.path.basename(str(name_or_path))
    if not name.endswith(".md"):
        return {"ok": False, "error": "只允许读 .md"}
    p = os.path.realpath(os.path.join(KB_DIR, name))
    root = os.path.realpath(KB_DIR)
    if not p.startswith(root + os.sep):
        return {"ok": False, "error": "路径越界"}
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % name}
    size = os.path.getsize(p)
    if size > max_bytes:
        return {"ok": False, "error": "文件太大（%.1f KB）" % (size / 1024)}
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return {"ok": True, "name": name, "path": p,
                    "content": f.read(), "bytes": size}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
