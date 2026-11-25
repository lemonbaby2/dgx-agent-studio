# -*- coding: utf-8 -*-
"""夜间技能自学习循环（v12）—— 吸收 → 批判 → 内化 → 反馈

设计原则：
  · 零第三方依赖（urllib + json），DGX 上不用装任何包
  · 宁可少收，不可污染：任何一维打分不及格就丢弃，写进账本记原因
  · 有记忆：ledger.jsonl 里同一个源连续 3 次被丢就不再爬
  · 可回滚：新技能只写进用户技能目录 ~/.dgx_studio/skops/，删掉即恢复原样

用法：
  python3 tools/harvest_nightly.py            # 正式跑
  python3 tools/harvest_nightly.py --dry-run  # 只看候选，不落盘
  python3 tools/harvest_nightly.py --limit 5  # 只处理 5 条
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
APP = os.path.join(HOME, "DGX-Agent-Studio")
SKILLS_CORE = os.path.join(APP, "skops")
SKILLS_USER = os.path.join(HOME, ".dgx_studio", "skops")
LEARN_DIR = os.path.join(HOME, ".dgx_studio", "learn")
LEDGER = os.path.join(LEARN_DIR, "ledger.jsonl")
OLLAMA = "http://127.0.0.1:11434"
MODEL = os.environ.get("HARVEST_MODEL") or "qwen3-coder:30b"

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
UA = {"User-Agent": "dgx-agent-studio-harvester/1.0"}

# 候选源：GitHub 上的技能/提示词仓库（星标门槛过滤垃圾）
SOURCES = [
    ("anthropics/skills", "main"),
    ("obra/superpowers", "main"),
    ("microsoft/skills", "main"),
]

SCORE_KEYS = ["executable", "verifiable", "reusable", "non_conflict", "low_risk"]


# ---------------------------------------------------------------------------
# 基础 IO
# ---------------------------------------------------------------------------
def http_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def http_text(url, timeout=25, limit=60000):
    req = urllib.request.Request(url, headers=UA)
    with OPENER.open(req, timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "ignore")


def ollama_json(prompt, timeout=300):
    body = json.dumps({
        "model": MODEL, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA + "/api/generate", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with OPENER.open(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    return d.get("response") or ""


def ledger_rows():
    if not os.path.isfile(LEDGER):
        return []
    rows = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def ledger_add(rec):
    os.makedirs(LEARN_DIR, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def existing_ids():
    """已存在的技能 id（去重基线）"""
    ids = set()
    for d in (SKILLS_CORE, SKILLS_USER):
        if not os.path.isdir(d):
            continue
        for n in os.listdir(d):
            if n.endswith(".yaml"):
                ids.add(n[:-5])
    return ids


# ---------------------------------------------------------------------------
# ① 吸收
# ---------------------------------------------------------------------------
def fetch_repo_skills(owner_repo, branch):
    """只取 README + 技能定义文件清单，不 clone 整个仓库"""
    out = []
    tree = None
    for br in ([branch] if branch else []) + ["main", "master"]:
        try:
            tree = http_json("https://api.github.com/repos/%s/git/trees/%s?recursive=1"
                             % (owner_repo, br))
            branch = br
            break
        except Exception:
            tree = None
    if tree is None:
        # 最后试一次：问 API 默认分支是什么
        try:
            info = http_json("https://api.github.com/repos/%s" % owner_repo)
            branch = info.get("default_branch") or "main"
            tree = http_json("https://api.github.com/repos/%s/git/trees/%s?recursive=1"
                             % (owner_repo, branch))
        except Exception as e:
            print("[吸收] %s 目录树取不到：%s" % (owner_repo, e))
            return out
    for node in tree.get("tree") or []:
        path = node.get("path") or ""
        if node.get("type") != "blob":
            continue
        low = path.lower()
        if not (low.endswith("skill.md") or low.endswith("/readme.md")
                or low.endswith(".skill") or low == "readme.md"):
            continue
        if node.get("size", 0) > 120000:
            continue
        out.append({"repo": owner_repo, "branch": branch, "path": path,
                    "size": node.get("size", 0)})
    return out


def fetch_candidate_text(cand, limit=12000):
    url = ("https://raw.githubusercontent.com/%s/%s/%s"
           % (cand["repo"], cand["branch"], cand["path"]))
    try:
        return http_text(url, limit=limit)
    except Exception as e:
        print("[吸收] 拉取失败 %s：%s" % (url, e))
        return ""


# ---------------------------------------------------------------------------
# ② 批判：五维打分
# ---------------------------------------------------------------------------
SCORE_PROMPT = """你是一个严格的技能评审员。下面是从开源社区抓到的一份技能描述。
请判断它值不值得内化进一个「工业视觉 / 嵌入式 / 边缘计算」方向的 agent 技能库。

五个维度，每项 0–3 分（0=完全没有，3=很好）：
1. executable   可执行性：是否给出具体命令、代码、参数？还是一片形容词？
2. verifiable   可验证性：有没有明确的成功判据（跑出什么/预期输出）？
3. reusable     复用性：是否绑死某个私有系统或特定账号？绑死则低分。
4. non_conflict 不重复：与通用 agent 技能是否高度重复？（越高越不重复）
5. low_risk     低风险：是否需要写系统目录、读密钥、对外发请求？有则低分。

另外请给出：
- title：给这个技能起一个 6 字以内的中文名
- summary：一句话说明它解决什么问题（20 字以内）
- trig：3–6 个中文触发短语，用顿号分隔
- why：若有维度 ≤1，说明为什么（20 字以内）

严格只输出 JSON，不要任何解释文字：
{"executable":0,"verifiable":0,"reusable":0,"non_conflict":0,"low_risk":0,
 "title":"","summary":"","trig":"","why":""}

技能正文（可能被截断）：
---
%s
---
"""


def score(text):
    try:
        raw = ollama_json(SCORE_PROMPT % text)
    except Exception as e:
        return None, "打分调用失败：%s" % e
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None, "模型没输出 JSON"
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None, "JSON 解析失败"
    for k in SCORE_KEYS:
        try:
            d[k] = int(d.get(k) or 0)
        except Exception:
            d[k] = 0
    return d, ""


def verdict(d, total_existing):
    """采纳判定：硬门槛 + 总分门槛"""
    if not d:
        return False, "打分失败"
    weak = [k for k in SCORE_KEYS if d[k] <= 1]
    if "executable" in weak:
        return False, "不可执行（只有形容词）"
    if "verifiable" in weak:
        return False, "没有成功判据"
    if "low_risk" in weak:
        return False, "风险过高（要写系统/读密钥/外发请求）"
    total = sum(d[k] for k in SCORE_KEYS)
    if total < 10:
        return False, "总分 %d/15 未达 10" % total
    if total_existing > 60:
        # 技能库已经很大时提高门槛，避免无脑膨胀
        if total < 12:
            return False, "技能库已 %d 条，总分 %d 未达 12" % (total_existing, total)
    return True, ""


# ---------------------------------------------------------------------------
# ③ 内化：生成 YAML
# ---------------------------------------------------------------------------
def make_yaml(d, cand, body_excerpt):
    # 中文标题经 [^a-z0-9] 过滤会变空 → 曾全部塌缩成 learned-learned 互相覆盖。
    # 修法：ASCII 化后不足 4 字符则改用来源路径哈希，保证唯一。
    title = (d.get("title") or "learned")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(slug) < 4:
        import hashlib
        key = "%s/%s" % (cand["repo"], cand["path"])
        slug = "s" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    sid = "learned-" + slug[:24]
    trig = [t.strip() for t in re.split(r"[、,，;；]", d.get("trig") or "") if t.strip()][:6]
    if not trig:
        trig = ["学到的技能"]
    safe_body = (body_excerpt or "").replace("---", "—").strip()
    if len(safe_body) > 3000:
        safe_body = safe_body[:3000] + "\n...(截断)"
    lines = [
        "id: %s" % sid,
        "name: %s" % (d.get("title") or "社区技能"),
        "description: %s" % (d.get("summary") or "从开源社区吸收"),
        "trigger: [%s]" % ", ".join(trig),
        "keywords: [%s]" % ", ".join(trig[:6]),
        "type: prompt",
        "source: github.com/%s/%s" % (cand["repo"], cand["path"]),
        "system: |",
        "  你是从开源社区吸收来的技能（来源：%s/%s）。" % (cand["repo"], cand["path"]),
        "  回答时优先给出可执行步骤与验证判据，不要停留在概念解释。",
        "  若与本项目已有技能冲突，以本项目铁律为准（工业现场优先、只读优先、改前备份）。",
        "prompt: |",
        "  参考以下社区做法，结合用户问题作答：",
        "",
    ]
    for ln in safe_body.splitlines():
        lines.append("  " + ln)
    return sid, "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--sources", default="")
    args = ap.parse_args()

    os.makedirs(SKILLS_USER, exist_ok=True)
    os.makedirs(LEARN_DIR, exist_ok=True)

    rows = ledger_rows()
    seen_src = {}
    for r in rows:
        if r.get("decision") == "drop":
            seen_src[r.get("source") or ""] = seen_src.get(r.get("source") or "", 0) + 1
    ids = existing_ids()

    sources = SOURCES
    if args.sources:
        sources = [(s, "main") for s in args.sources.split(",") if s.strip()]

    print("[1/4] 吸收：扫描 %d 个源（现有技能 %d 条）" % (len(sources), len(ids)))
    cands = []
    for repo, br in sources:
        got = fetch_repo_skills(repo, br)
        print("  %-28s 候选 %d 条" % (repo, len(got)))
        cands.extend(got)

    # 去重 + 限流
    picked, uniq = [], set()
    for c in cands:
        key = "%s/%s" % (c["repo"], c["path"])
        if key in uniq:
            continue
        uniq.add(key)
        if seen_src.get(key, 0) >= 3:
            continue
        picked.append(c)
    picked = picked[:max(1, args.limit)]
    print("[1/4] 去重后进入评审 %d 条" % len(picked))

    adopted, dropped = [], []
    for i, c in enumerate(picked, 1):
        key = "%s/%s" % (c["repo"], c["path"])
        print("[2/4] (%d/%d) 评审 %s" % (i, len(picked), key))
        text = fetch_candidate_text(c)
        if not text.strip():
            dropped.append((key, "正文为空"))
            ledger_add({"ts": int(time.time()), "source": key,
                        "decision": "drop", "reason": "正文为空"})
            continue
        d, err = score(text[:11000])
        ok, why = verdict(d, len(ids))
        if not d:
            dropped.append((key, err))
            ledger_add({"ts": int(time.time()), "source": key,
                        "decision": "drop", "reason": err})
            continue
        total = sum(d[k] for k in SCORE_KEYS)
        print("     分数 %s 总分 %d/15 → %s" % (
            "/".join(str(d[k]) for k in SCORE_KEYS), total,
            "采纳" if ok else "丢弃：" + why))
        if not ok:
            dropped.append((key, why))
            ledger_add({"ts": int(time.time()), "source": key, "decision": "drop",
                        "reason": why, "scores": d})
            continue

        sid, yml = make_yaml(d, c, text)
        if sid in ids:
            sid2 = sid + "-" + str(int(time.time()))[-4:]
            yml = yml.replace("id: " + sid, "id: " + sid2, 1)
            sid = sid2
        path = os.path.join(SKILLS_USER, sid + ".yaml")
        if args.dry_run:
            print("     [dry-run] 会写入 %s" % path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(yml)
            print("     → 已写入 %s" % path)
        ids.add(sid)
        adopted.append((sid, key, total))
        ledger_add({"ts": int(time.time()), "source": key, "decision": "adopt",
                    "skill_id": sid, "scores": d, "total": total})

    print("\n[3/4] 内化完成：采纳 %d 条，丢弃 %d 条" % (len(adopted), len(dropped)))
    for sid, src, total in adopted:
        print("  + %-28s (%d/15)  %s" % (sid, total, src))
    for src, why in dropped:
        print("  - %-50s %s" % (src, why))

    print("\n[4/4] 账本：%s（共 %d 条记录）" % (LEDGER, len(ledger_rows())))
    n_skills = len(existing_ids())
    print("技能库：%d 条" % n_skills)

    # [5/5] 能力阶梯：技能库长到门槛就升档（模型 + 上下文预算）
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from server import capacity
        ok, why = capacity.evaluate(n_skills)
        print("\n[5/5] 能力阶梯：当前档 %s｜%s" % (
            capacity.snapshot(n_skills)["name"], why))
        if ok and not args.dry_run:
            snap = capacity.promote(n_skills, why)
            print("  ↑ 已升档 → %s（推荐 %s，技能槽 %d，文档预算 %d 字符）" % (
                snap["name"], snap["recommend_model"], snap["skill_slots"],
                snap["doc_chars"]))
            ledger_add({"ts": int(time.time()), "source": "capacity-ladder",
                        "decision": "promote", "to_tier": snap["tier"],
                        "model": snap["recommend_model"], "reason": why})
    except Exception as e:
        print("\n[5/5] 能力阶梯跳过：%s" % e)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
