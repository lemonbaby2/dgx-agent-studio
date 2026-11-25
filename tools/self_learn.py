# -*- coding: utf-8 -*-
"""
self_learn.py · 自学习引擎（每天 00:00 由 systemd timer 触发）
====================================================================
干什么：
  读自己的聊天记录 → 用 LLM 提炼可复用知识 → 两路落地
    ① 知识点 Markdown  → ~/DGX-Notes/kb/YYYY-MM-DD-<topic>.md（自动 git commit）
    ② 候选技能 YAML    → ~/.dgx_studio/skops/autolearn/<id>.yaml（热加载，无需重启）
  最后写一份学习报告 → ~/.dgx_studio/learning/YYYY-MM-DD.json

为什么这么设计（踩过的坑 / 硬约束）：
  · **必须增量**：sessions.json 每天都会变，全量重学既烧 token 又会产生重复技能。
    做法：对每轮 (user+assistant) 算 sha1 指纹，存 seen.json，只处理新指纹。
  · **LLM 只吐 JSON，YAML 由本脚本生成**。让模型直接写 YAML 一定会有缩进/引号问题，
    而且中文冒号会把 key: value 切坏。生成后还要用 skills._parse_yaml 回读自检。
  · **单次最多 3 个新技能**：防止一次跑出几十个垃圾技能把技能库污染。
  · **id 必须带 auto- 前缀**，且不能与内置 skops 撞名 —— 撞名会静默丢弃（skills.py
    是先扫内置目录，重复 id 直接 continue），表现为「生成了但没生效」，极难查。
  · **不覆盖**：目标文件已存在就跳过，只追加不重写。
  · 聊天记录里可能有口令/内网 IP，写进笔记前做一次脱敏（sk-/ghp_/密码/token 形式）。

用法：
    python3 self_learn.py                # 正常跑（定时器调这个）
    python3 self_learn.py --force        # 忽略 seen 全量重学
    python3 self_learn.py --dry-run      # 只打印，不落盘
    python3 self_learn.py --min-chars 200  # 只学长度够的对话（默认 120）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

# ---- 路径 ----
HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME, ".dgx_studio")
SESSIONS = os.path.join(DATA_DIR, "sessions.json")
LEARN_DIR = os.path.join(DATA_DIR, "learning")
SEEN_PATH = os.path.join(LEARN_DIR, "seen.json")
LATEST = os.path.join(LEARN_DIR, "latest.json")
USER_SKOPS = os.path.join(DATA_DIR, "skops", "autolearn")
KB_DIR = os.path.join(HOME, "DGX-Notes", "kb")
APP_SKOPS = os.path.join(HOME, "DGX-Agent-Studio", "skops")

OLLAMA = os.environ.get("DGX_OLLAMA") or "http://127.0.0.1:11434"
MODEL = os.environ.get("DGX_LEARN_MODEL") or "qwen3-coder:30b"

MAX_SKILLS_PER_RUN = 3
MAX_MSGS_PER_RUN = 60
MAX_INPUT_CHARS = 24000

# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def _ensure_dirs():
    for d in (LEARN_DIR, USER_SKOPS, KB_DIR):
        os.makedirs(d, exist_ok=True)


def _read_json(p, fallback):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def _write_text(p, s):
    """原子写：先 .tmp 再 rename，避免中断留下半个文件"""
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(s)
    os.replace(tmp, p)


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
SECRET_PATTERNS = [
    (re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,})"), "***GITHUB_TOKEN***"),
    (re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"), "***GITHUB_PAT***"),
    (re.compile(r"(sk-[A-Za-z0-9]{16,})"), "***API_KEY***"),
    (re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*\S+"), r"\1=***"),
    (re.compile(r"(?i)(token)\s*[=:]\s*[A-Za-z0-9_\-]{12,}"), r"\1=***"),
    (re.compile(r"(?i)(nvidia|admin)\s*/\s*(\S{4,})"), r"\1/***"),
]


def redact(s):
    for pat, rep in SECRET_PATTERNS:
        s = pat.sub(rep, s)
    return s


# ---------------------------------------------------------------------------
# 收集对话
# ---------------------------------------------------------------------------
def collect(min_chars=120, force=False):
    """返回 [{fp, session, q, a, ts}]，只含没学过的"""
    d = _read_json(SESSIONS, {}) or {}
    sessions = d.get("sessions") or []
    seen = set() if force else set((_read_json(SEEN_PATH, {}) or {}).get("fps") or [])

    picked, new_fps = [], []
    for s in sessions:
        sid = s.get("id") or "?"
        title = (s.get("title") or "")[:40]
        msgs = s.get("messages") or []
        # 按 user -> assistant 配对
        i = 0
        while i < len(msgs):
            m = msgs[i]
            if (m.get("role") != "user"):
                i += 1
                continue
            q = (m.get("content") or "").strip()
            a = ""
            if i + 1 < len(msgs) and msgs[i + 1].get("role") == "assistant":
                a = (msgs[i + 1].get("content") or "").strip()
                i += 2
            else:
                i += 1
            if len(q) + len(a) < min_chars:
                continue
            # 跳过纯寒暄/纯报错
            if len(a) < 40 and "错误" not in a:
                continue
            fp = hashlib.sha1((q + "\x00" + a[:2000]).encode("utf-8")).hexdigest()[:16]
            new_fps.append(fp)
            if fp in seen:
                continue
            picked.append({"fp": fp, "session": sid, "title": title,
                           "q": redact(q)[:4000], "a": redact(a)[:6000],
                           "ts": s.get("updated") or 0})
            if len(picked) >= MAX_MSGS_PER_RUN:
                break
        if len(picked) >= MAX_MSGS_PER_RUN:
            break
    return picked, new_fps


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
SYS = """你是「知识提炼引擎」，负责把一个 AI 助手的历史对话蒸馏成可复用资产。

你要输出**严格 JSON**（不要 ```json 包裹，不要任何解释文字），结构：

{
  "insights": [
    {"topic": "短标题（≤20字，可用于文件名）",
     "kind": "fact|命令|坑|流程|参数",
     "content": "Markdown 正文。必须具体：命令要能直接粘贴，数字要给值，坑要写现象→根因→修法"}
  ],
  "new_skills": [
    {"id": "auto-xxx",
     "name": "技能名（中文，≤12字）",
     "description": "一句话说明什么时候用",
     "trigger": ["触发词1", "触发词2"],
     "keywords": ["关键词1", "关键词2"],
     "system": "给 LLM 的角色提示词。要包含：身份、必须遵守的硬约束、输出结构。200-600字。"}
  ]
}

铁律：
· insights 只保留**有长期价值**的（可复现的命令、定位到的根因、量化结论）。
  寒暄、客套、"我会继续努力"这类一律不要。
· 纯一次性的东西（"今天装了 XX"）不要，除非里面有关键参数/坑。
· new_skills 必须能对应到**反复出现的同类任务**。只做过一次的不要造技能。
  没有合适的就给空数组 []，这很正常。
· id 必须以 auto- 开头，只用小写字母/数字/连字符。
· trigger 和 keywords 用用户真实会说的中文词。
· 不许编造对话里没有的技术细节。数字必须来自对话。
"""


def call_llm(dialog_text, timeout=600):
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYS},
            {"role": "user", "content": "以下是最近的对话记录（已脱敏），请提炼：\n\n"
                                        + dialog_text},
        ],
        "stream": False,
        "options": {"temperature": 0.3, "num_ctx": 16384},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA.rstrip("/") + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        obj = json.loads(r.read().decode("utf-8", "ignore"))
    if obj.get("error"):
        raise RuntimeError(obj["error"])
    return ((obj.get("message") or {}).get("content") or "").strip()


def extract_json(s):
    s = (s or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 落地：知识库
# ---------------------------------------------------------------------------
def slug(s, limit=28):
    t = re.sub(r"[\\/:*?\"<>|\s]+", "-", (s or "").strip())
    t = re.sub(r"-+", "-", t).strip("-")
    return (t[:limit] or "note")


def write_kb(insights, date, dry=False):
    """把知识点写成当天的 KB 文件（一个 topic 一段，追加到同一天的文件）"""
    if not insights:
        return []
    written = []
    by_topic = {}
    for it in insights[:20]:
        if not isinstance(it, dict):
            continue
        topic = (it.get("topic") or "").strip()
        content = (it.get("content") or "").strip()
        if not topic or len(content) < 20:
            continue
        by_topic.setdefault(topic, []).append(
            (it.get("kind") or "fact", content))
    for topic, items in by_topic.items():
        name = "%s_%s.md" % (date, slug(topic))
        path = os.path.join(KB_DIR, name)
        parts = ["# %s" % topic, "",
                 "> 由 DGX Agent 自学习生成 · %s · 来源：本地对话记录" % date, ""]
        for kind, content in items:
            parts.append("## [%s]" % kind)
            parts.append("")
            parts.append(content)
            parts.append("")
        text = "\n".join(parts)
        if dry:
            log("[dry] 会写 KB: %s (%d 字)" % (name, len(text)))
        else:
            _write_text(path, text)
            log("KB 已写: %s (%d 字)" % (name, len(text)))
        written.append({"name": name, "path": path, "topic": topic,
                        "chars": len(text)})
    return written


# ---------------------------------------------------------------------------
# 落地：技能 YAML
# ---------------------------------------------------------------------------
def existing_skill_ids():
    ids = set()
    for d in (APP_SKOPS, os.path.join(DATA_DIR, "skops")):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith((".yaml", ".yml")):
                ids.add(os.path.splitext(f)[0])
    # 再读一遍 id: 字段（文件名可能与 id 不同）
    for d in (APP_SKOPS, os.path.join(DATA_DIR, "skops")):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.endswith((".yaml", ".yml")):
                continue
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    m = re.search(r"^id:\s*(\S+)", fh.read(), re.M)
                if m:
                    ids.add(m.group(1).strip())
            except Exception:
                pass
    return ids


def _yaml_list(items):
    return "[" + ", ".join(json.dumps(str(x), ensure_ascii=False)
                           for x in items) + "]"


def _yaml_block(text):
    """把多行文本写成 YAML 块标量（每行缩进 2 空格）"""
    lines = (text or "").replace("\r", "").split("\n")
    return "\n".join("  " + l.rstrip() if l.strip() else "" for l in lines)


def build_skill_yaml(sk):
    sid = sk["id"]
    return (
        "id: %s\n"
        "name: %s\n"
        "description: %s\n"
        "trigger: %s\n"
        "keywords: %s\n"
        "type: prompt\n"
        "system: |\n%s\n"
        % (sid,
           json.dumps(sk["name"], ensure_ascii=False),
           json.dumps(sk["description"], ensure_ascii=False),
           _yaml_list(sk["trigger"]),
           _yaml_list(sk["keywords"]),
           _yaml_block(sk["system"]))
    )


def write_skills(cands, date, dry=False):
    """写用户级技能（autolearn 子目录），带三重自检"""
    if not cands:
        return []
    existing = existing_skill_ids()
    out = []
    made = 0
    for sk in cands:
        if made >= MAX_SKILLS_PER_RUN:
            break
        if not isinstance(sk, dict):
            continue
        sid = (sk.get("id") or "").strip()
        name = (sk.get("name") or "").strip()
        system = (sk.get("system") or "").strip()
        if not re.match(r"^auto-[a-z0-9][a-z0-9-]{1,40}$", sid):
            log("跳过技能：id 不合规 %r" % sid)
            continue
        if sid in existing:
            log("跳过技能：id 已存在 %s" % sid)
            continue
        if len(name) < 2 or len(system) < 60:
            log("跳过技能：内容不足 %s (name=%d system=%d)"
                % (sid, len(name), len(system)))
            continue
        trig = [t for t in (sk.get("trigger") or []) if t][:8]
        kw = [t for t in (sk.get("keywords") or []) if t][:10]
        if not trig:
            log("跳过技能：没有 trigger %s" % sid)
            continue
        sk2 = {"id": sid, "name": name,
               "description": (sk.get("description") or name)[:80],
               "trigger": trig, "keywords": kw, "system": system}
        text = build_skill_yaml(sk2)

        # 自检 1：能被项目自带的 YAML 解析器读回来
        ok_parse = False
        try:
            sys.path.insert(0, os.path.join(HOME, "DGX-Agent-Studio"))
            from server import skills as _sk  # noqa
            y = _sk._parse_yaml(text)
            ok_parse = (isinstance(y, dict) and y.get("name")
                        and y.get("id") == sid and y.get("system"))
        except Exception as e:
            log("自检解析器不可用（%s），退化为正则校验" % e)
            ok_parse = bool(re.search(r"^system:\s*\|", text, re.M)
                            and re.search(r"^id:\s*auto-", text, re.M))
        if not ok_parse:
            log("跳过技能：YAML 自检失败 %s" % sid)
            continue

        path = os.path.join(USER_SKOPS, sid + ".yaml")
        if os.path.exists(path):
            log("跳过技能：文件已存在 %s" % path)
            continue
        if dry:
            log("[dry] 会写技能: %s (%d 字)" % (sid, len(text)))
        else:
            _write_text(path, text)
            log("技能已写: %s.yaml (%d 字)" % (sid, len(text)))
        existing.add(sid)
        made += 1
        out.append({"id": sid, "name": name, "path": path,
                    "trigger": trig, "chars": len(text)})
    return out


# ---------------------------------------------------------------------------
# git 提交（走 git_api 的同一套规则）
# ---------------------------------------------------------------------------
def git_commit_kb(date):
    if not os.path.isdir(os.path.join(HOME, "DGX-Notes", ".git")):
        return "（笔记目录不是 git 仓库，跳过提交）"
    def g(args, cfg=None):
        cmd = ["git"]
        for k, v in (cfg or []):
            cmd += ["-c", "%s=%s" % (k, v)]
        cmd += args
        p = subprocess.run(cmd, cwd=os.path.join(HOME, "DGX-Notes"),
                           capture_output=True, text=True, timeout=120,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                           encoding="utf-8", errors="ignore")
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    g(["add", "-A"])
    c, out = g(["commit", "-m", "kb: 自学习 %s" % date])
    return out[-400:] or ("commit rc=%d" % c)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def write_report(date, res):
    """写日报 + latest.json。

    ⚠️ latest.json 只在「本次真有产出」时覆盖。
    否则一次 nothing_new 的空跑会把上一次成功的统计冲掉，
    前端就显示成"上次学了 0 条"，看着像坏了。空跑仍写进当日 json 备查。
    """
    path = os.path.join(LEARN_DIR, "%s.json" % date)
    hist = _read_json(path, None)
    if hist and isinstance(hist, dict):
        hist.setdefault("runs", []).append(res)
    else:
        hist = {"date": date, "runs": [res]}
    _write_text(path, json.dumps(hist, ensure_ascii=False, indent=1))

    has_output = (res.get("status") == "ok"
                  and ((res.get("insights") or 0) > 0
                       or (res.get("skills") or [])
                       or (res.get("kb") or [])))
    if has_output:
        _write_text(LATEST, json.dumps(res, ensure_ascii=False, indent=1))
    elif not os.path.exists(LATEST):
        _write_text(LATEST, json.dumps(res, ensure_ascii=False, indent=1))
    return path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略 seen 全量重学")
    ap.add_argument("--dry-run", action="store_true", help="只打印不落盘")
    ap.add_argument("--min-chars", type=int, default=120)
    ap.add_argument("--no-llm", action="store_true", help="不发 LLM，只统计")
    a = ap.parse_args()

    _ensure_dirs()
    date = time.strftime("%Y-%m-%d")
    t0 = time.time()
    log("=== 自学习开始 %s ===" % date)

    picked, all_fps = collect(a.min_chars, a.force)
    log("待学对话 %d 轮（本次扫描 %d 轮）" % (len(picked), len(all_fps)))
    if not picked:
        res = {"ts": int(time.time()), "date": date, "status": "nothing_new",
               "scanned": len(all_fps), "picked": 0, "insights": 0,
               "skills": [], "kb": [], "elapsed": 0,
               "note": "没有新的、够长的对话可学"}
        write_report(date, res)
        log("没有新内容，结束")
        return res

    # 拼对话（控制长度）
    buf, used = [], 0
    for p in picked:
        blk = ("### 会话《%s》\n[问] %s\n[答] %s\n"
               % (p["title"] or p["session"], p["q"], p["a"]))
        if used + len(blk) > MAX_INPUT_CHARS:
            break
        buf.append(blk)
        used += len(blk)
    dialog = "\n".join(buf)
    log("送入模型 %d 字（%d 轮）" % (len(dialog), len(buf)))

    res = {"ts": int(time.time()), "date": date, "status": "ok",
           "scanned": len(all_fps), "picked": len(picked),
           "sent_chars": len(dialog), "model": MODEL,
           "insights": 0, "skills": [], "kb": [], "elapsed": 0}

    if a.no_llm:
        res["status"] = "dry_no_llm"
        write_report(date, res)
        return res

    try:
        raw = call_llm(dialog)
    except Exception as e:
        log("LLM 调用失败：%s" % e)
        res.update(status="llm_failed", error=str(e)[:300],
                   elapsed=round(time.time() - t0, 1))
        write_report(date, res)
        return res

    obj = extract_json(raw)
    if not obj:
        log("模型输出不是 JSON，原样存档备查")
        _write_text(os.path.join(LEARN_DIR, "%s_raw.txt" % date), raw[:20000])
        res.update(status="bad_json", raw_len=len(raw),
                   elapsed=round(time.time() - t0, 1))
        write_report(date, res)
        return res

    insights = obj.get("insights") or []
    cands = obj.get("new_skills") or []
    log("模型给出：%d 条知识 / %d 个候选技能" % (len(insights), len(cands)))

    kb = write_kb(insights, date, a.dry_run)
    sks = write_skills(cands, date, a.dry_run)
    commit = git_commit_kb(date) if (kb and not a.dry_run) else ""

    res.update(insights=len(insights), kb=kb, skills=sks,
               candidates=len(cands), commit=commit,
               elapsed=round(time.time() - t0, 1))

    # 更新 seen（只标记本次真正送进模型的）
    if not a.dry_run:
        seen = (_read_json(SEEN_PATH, {}) or {})
        fps = set(seen.get("fps") or [])
        for p in picked[:len(buf)]:
            fps.add(p["fp"])
        _write_text(SEEN_PATH, json.dumps(
            {"fps": sorted(fps)[-5000:],        # 只留最近 5000 条，防无限涨
             "updated": int(time.time())},
            ensure_ascii=False, indent=0))

    rp = write_report(date, res)
    log("=== 完成：知识 %d 条 / 新技能 %d 个 / 耗时 %ss ==="
        % (len(kb), len(sks), res["elapsed"]))
    log("报告: %s" % rp)
    return res


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            d = time.strftime("%Y-%m-%d")
            os.makedirs(LEARN_DIR, exist_ok=True)
            _write_text(os.path.join(LEARN_DIR, "latest.json"), json.dumps(
                {"ts": int(time.time()), "date": d, "status": "crash",
                 "error": str(e)[:500]}, ensure_ascii=False, indent=1))
        except Exception:
            pass
        sys.exit(1)
