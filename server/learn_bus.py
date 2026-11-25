# -*- coding: utf-8 -*-
"""
learn_bus · v10 实时学习总线（"每时每刻都在学"）
====================================================================
和 self_learn.py 的分工：
  self_learn.py  = 批量/兜底（每天 00:00 跑一次，全量扫 sessions.json，查漏补缺）
  learn_bus.py   = 实时/增量（每轮对话结束就入队，攒够一批立刻消化）

为什么要「攒批 + 静默窗口」而不是每轮对话都调 LLM：
  · DGX 上 qwen3-coder:30b 一次推理要 20-40 秒，同事同时聊天时每轮都调
    会把 Ollama 打满，对话本身变卡 —— 这是最不能接受的劣化。
  · 所以：**静默 25 秒 或 积压 8 条**才触发一次消化，单次最多 6 条。
  · 两次 LLM 调用之间至少隔 30 秒（MIN_INTERVAL），做硬限流。

可靠性：
  · 入队立刻追加写 pending.jsonl —— 服务重启/崩溃都不丢
  · 只有"成功消化"的条目才从 pending 里移除
  · 单条失败重试 3 次，超过就丢到 dead.jsonl（不无限重试烧资源）

安全性：
  · 入队即脱敏（ghp_/sk-/password=）
  · 提炼出的技能仍走 self_learn.write_skills 的三重自检
  · 线程是 daemon，任何异常都在内部吞掉并记日志，**绝不影响 HTTP 服务**

对外：
  start()            启动后台线程（app.py 启动时调一次，幂等）
  publish(record)    对话结束后入队（不阻塞）
  status()           pending 数 / 上次消化 / 计数 / 最近日志
  poke()             手动催一次（前端"立即学习"用）
  flush_status()     只读，给 /api/capabilities
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME, ".dgx_studio")
LEARN_DIR = os.path.join(DATA_DIR, "learning")
PENDING = os.path.join(LEARN_DIR, "pending.jsonl")
DEAD = os.path.join(LEARN_DIR, "dead.jsonl")
TOOLS = os.path.join(HOME, "DGX-Agent-Studio", "tools")

# 节奏参数（改这里就够）
QUIET_SEC = 25          # 最后一次入队后静默多久才动手
BATCH_MAX = 8           # 或积压到这么多条也立刻动手
BATCH_TAKE = 6          # 单次最多消化几条（控制 LLM 输入长度）
MIN_INTERVAL = 30       # 两次 LLM 调用最小间隔（硬限流）
TICK = 15               # 后台线程轮询间隔
MAX_PENDING = 500       # pending 上限，超了丢最旧的
MAX_RETRY = 3

_LOCK = threading.RLock()
_started = False
_thread = None

_state = {
    "pending": 0,
    "last_publish": 0.0,
    "last_run": 0.0,
    "last_ok": 0.0,
    "runs": 0,
    "ok_runs": 0,
    "fail_runs": 0,
    "insights_total": 0,
    "skills_total": 0,
    "log": [],
    "last_error": "",
    "busy": False,
}


def _log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    with _LOCK:
        _state["log"].append(line)
        if len(_state["log"]) > 200:
            _state["log"] = _state["log"][-200:]
    print("[learn_bus] " + msg, flush=True)


def _ensure():
    os.makedirs(LEARN_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# pending 持久化
# ---------------------------------------------------------------------------
def _read_pending():
    _ensure()
    out = []
    if not os.path.isfile(PENDING):
        return out
    try:
        with open(PENDING, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _write_pending(items):
    _ensure()
    tmp = PENDING + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        os.replace(tmp, PENDING)
    except Exception as e:
        _log("写 pending 失败：%s" % e)


def _append_pending(rec):
    _ensure()
    try:
        with open(PENDING, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        _log("追加 pending 失败：%s" % e)


def _append_dead(rec, why):
    _ensure()
    try:
        with open(DEAD, "a", encoding="utf-8") as f:
            f.write(json.dumps({**rec, "_dead": why,
                                "_ts": int(time.time())},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 入队
# ---------------------------------------------------------------------------
def _fp(q, a):
    import hashlib
    return hashlib.sha1((q + "\x00" + (a or "")[:2000])
                        .encode("utf-8")).hexdigest()[:16]


def publish(record):
    """对话结束后调用。只做内存+落盘，**绝不调 LLM**，因此极快、不阻塞。"""
    try:
        q = (record.get("q") or "").strip()
        a = (record.get("a") or "").strip()
        if len(q) + len(a) < 80:
            return {"ok": False, "skipped": "too_short"}
        rec = {
            "fp": _fp(q, a),
            "q": q[:4000],
            "a": a[:6000],
            "session": record.get("session") or "",
            "title": (record.get("title") or "")[:40],
            "owner": record.get("owner") or "",
            "owner_name": (record.get("owner_name") or "")[:24],
            "ts": record.get("ts") or time.time(),
            "tries": 0,
        }
        # 去重：pending 里已有同指纹就跳过
        with _LOCK:
            for it in _read_pending():
                if it.get("fp") == rec["fp"]:
                    return {"ok": False, "skipped": "dup_pending"}
        _append_pending(rec)
        with _LOCK:
            n = len(_read_pending())
            if n > MAX_PENDING:                 # 防无限膨胀
                items = _read_pending()[-(MAX_PENDING):]
                _write_pending(items)
                n = len(items)
            _state["pending"] = n
            _state["last_publish"] = time.time()
        return {"ok": True, "pending": n, "fp": rec["fp"]}
    except Exception as e:
        _log("publish 异常：%s" % e)
        return {"ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# 消化
# ---------------------------------------------------------------------------
def _load_sl():
    """按需导入 self_learn（复用它的提炼/落地逻辑，避免两份实现漂移）"""
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    import importlib
    import self_learn
    importlib.reload(self_learn)      # 让它在跑动中也能拿到最新常量
    return self_learn


def _digest(items):
    """调 LLM 提炼一批对话，落地 KB + 技能。返回 (ok, info)"""
    sl = _load_sl()
    date = time.strftime("%Y-%m-%d")

    buf, used = [], 0
    for it in items:
        # 多用户来源标注进标题，方便日后追溯"这是谁聊出来的"
        who = it.get("owner_name") or "访客"
        blk = ("### 会话《%s》（%s）\n[问] %s\n[答] %s\n"
               % (it.get("title") or it.get("session") or "?", who,
                  it.get("q") or "", it.get("a") or ""))
        if used + len(blk) > 24000:
            break
        buf.append(blk)
        used += len(blk)
    if not buf:
        return False, {"error": "无有效内容"}

    dialog = "\n".join(buf)
    try:
        raw = sl.call_llm(dialog, timeout=420)
    except Exception as e:
        return False, {"error": "LLM 失败：%s" % str(e)[:200]}

    obj = sl.extract_json(raw)
    if not obj:
        return False, {"error": "输出非 JSON", "raw_len": len(raw or "")}

    insights = obj.get("insights") or []
    cands = obj.get("new_skills") or []
    kb = sl.write_kb(insights, date, dry=False)
    sks = sl.write_skills(cands, date, dry=False)

    # 实时学习也要落 git，否则知识只在本地、没有版本记录
    commit = ""
    if kb:
        try:
            commit = sl.git_commit_kb(date)
        except Exception as e:
            _log("git 提交失败（忽略）：%s" % str(e)[:150])

    # 把消化过的指纹写进 seen，避免 00:00 批处理重复学
    try:
        sp = sl.SEEN_PATH
        seen = sl._read_json(sp, {}) or {}
        fps = set(seen.get("fps") or [])
        for it in items[:len(buf)]:
            fps.add(it.get("fp"))
        sl._write_text(sp, json.dumps(
            {"fps": sorted(fps)[-5000:], "updated": int(time.time())},
            ensure_ascii=False, indent=0))
    except Exception as e:
        _log("写 seen 失败：%s" % e)

    # 记一份实时学习报告，和每天的报告放一起（同一天 append 到 runs）
    try:
        rp = os.path.join(LEARN_DIR, "%s.json" % date)
        hist = sl._read_json(rp, None) or {"date": date, "runs": []}
        hist.setdefault("runs", []).append({
            "ts": int(time.time()), "date": date, "status": "ok",
            "mode": "realtime", "picked": len(items[:len(buf)]),
            "sent_chars": len(dialog), "insights": len(insights),
            "kb": kb, "skills": sks, "candidates": len(cands),
            "owners": sorted({(it.get("owner_name") or "访客")
                              for it in items[:len(buf)]}),
        })
        sl._write_text(rp, json.dumps(hist, ensure_ascii=False, indent=1))
        sl._write_text(os.path.join(LEARN_DIR, "latest.json"), json.dumps({
            "ts": int(time.time()), "date": date, "status": "ok",
            "mode": "realtime", "insights": len(insights),
            "kb": kb, "skills": sks, "picked": len(items[:len(buf)]),
            "owners": sorted({(it.get("owner_name") or "访客")
                              for it in items[:len(buf)]}),
        }, ensure_ascii=False, indent=1))
    except Exception as e:
        _log("写报告失败：%s" % e)

    return True, {"insights": len(insights), "kb": kb, "skills": sks,
                  "took": len(items[:len(buf)]), "commit": commit[-120:]}


def _maybe_run(force=False):
    """判断该不该动手，该就动手"""
    now = time.time()
    with _LOCK:
        items = _read_pending()
        n = len(items)
        _state["pending"] = n
        if not items:
            return
        if _state["busy"]:
            return
        if not force:
            quiet = now - (_state["last_publish"] or 0)
            if n < BATCH_MAX and quiet < QUIET_SEC:
                return
            if now - (_state["last_run"] or 0) < MIN_INTERVAL:
                return
        _state["busy"] = True
        _state["last_run"] = now

    ok = False
    info = {}
    try:
        batch = items[:BATCH_TAKE]
        _log("开始消化 %d 条（队列 %d）" % (len(batch), n))
        ok, info = _digest(batch)
        if ok:
            done = {it.get("fp") for it in batch[:info.get("took", len(batch))]}
            rest = [it for it in _read_pending() if it.get("fp") not in done]
            _write_pending(rest)
            with _LOCK:
                _state["runs"] += 1
                _state["ok_runs"] += 1
                _state["last_ok"] = time.time()
                _state["insights_total"] += info.get("insights", 0)
                _state["skills_total"] += len(info.get("skills") or [])
                _state["pending"] = len(rest)
                _state["last_error"] = ""
            _log("消化成功：知识 %d 条 / 新技能 %d 个 / 剩余 %d"
                 % (info.get("insights", 0), len(info.get("skills") or []),
                    len(rest)))
        else:
            # 失败：给这批 +1 重试计数，超限丢 dead
            err = info.get("error") or "未知"
            allitems = _read_pending()
            keep, dead = [], []
            for it in allitems:
                if it.get("fp") in {x.get("fp") for x in batch}:
                    it["tries"] = int(it.get("tries") or 0) + 1
                    it["last_error"] = err[:200]
                    if it["tries"] >= MAX_RETRY:
                        dead.append(it)
                        continue
                keep.append(it)
            _write_pending(keep)
            for d in dead:
                _append_dead(d, d.get("last_error") or err)
            with _LOCK:
                _state["runs"] += 1
                _state["fail_runs"] += 1
                _state["pending"] = len(keep)
                _state["last_error"] = err[:200]
            _log("消化失败（%s），队列 %d，丢弃 %d" % (err, len(keep), len(dead)))
    except Exception as e:
        with _LOCK:
            _state["fail_runs"] += 1
            _state["last_error"] = str(e)[:200]
        _log("消化异常：%s" % e)
    finally:
        with _LOCK:
            _state["busy"] = False


# ---------------------------------------------------------------------------
# 后台线程
# ---------------------------------------------------------------------------
def _loop():
    _log("实时学习线程已启动（静默 %ds / 批满 %d / 限流 %ds）"
         % (QUIET_SEC, BATCH_MAX, MIN_INTERVAL))
    while True:
        try:
            time.sleep(TICK)
            _maybe_run()
        except Exception as e:
            _log("线程异常（已忽略）：%s" % e)
            time.sleep(30)


def start():
    """幂等启动。app.py 启动时调一次。"""
    global _started, _thread
    with _LOCK:
        if _started:
            return {"ok": True, "already": True}
        _started = True
        _state["pending"] = len(_read_pending())
        _thread = threading.Thread(target=_loop, daemon=True,
                                   name="learn_bus")
        _thread.start()
    return {"ok": True, "pending": _state["pending"]}


def poke():
    """手动催一次（前端"立即学习"）。异步，不阻塞。"""
    def _go():
        try:
            _maybe_run(force=True)
        except Exception as e:
            _log("poke 异常：%s" % e)
    threading.Thread(target=_go, daemon=True).start()
    return {"ok": True, "note": "已催，稍候看 status"}


def status():
    with _LOCK:
        items = _read_pending()
        st = dict(_state)
        st["pending"] = len(items)
        st["log"] = _state["log"][-40:]
        st["config"] = {"quiet_sec": QUIET_SEC, "batch_max": BATCH_MAX,
                        "batch_take": BATCH_TAKE, "min_interval": MIN_INTERVAL,
                        "tick": TICK}
        st["running"] = bool(_started)
        # 队列里有哪些人聊的（展示"同事都在贡献"）
        st["pending_owners"] = sorted({(it.get("owner_name") or "访客")
                                       for it in items})
        return st


def summary():
    """给 /api/capabilities 的轻量字段"""
    try:
        with _LOCK:
            return {"pending": len(_read_pending()),
                    "runs": _state["runs"],
                    "ok_runs": _state["ok_runs"],
                    "running": bool(_started)}
    except Exception:
        return {"pending": 0, "runs": 0, "ok_runs": 0, "running": False}
