# -*- coding: utf-8 -*-
"""v13 · 能力阶梯（capacity ladder）

老师的原话："让参数量逐渐扩大"。

落到工程上就是三件事，都可验证、可回滚：

  1. **模型阶梯**：按参数量从小到大排好，只有"上一级够用且硬件扛得住"才升；
     降级不设门槛（出问题立刻退），保证永远有可用档位。
  2. **上下文预算**：随技能库增长，注入到 system prompt 的技能条数与文档字符数
     跟着涨（这是"有效参数量"的另一半——模型权重涨不了，喂进去的上下文能涨）。
  3. **升级判据**：技能数跨阈值 + Ollama 里确实有该模型 + GPU 空闲显存够。
     三条全真才升，并写台账。任何一条不满足只记录理由，不动配置。

状态落盘 ~/.dgx_studio/capacity.json，接口 /api/capacity。
"""
from __future__ import annotations

import json
import os
import subprocess
import time

STORE = os.path.join(os.path.expanduser("~"), ".dgx_studio", "capacity.json")

# tier: (名字, 模型, 上下文技能条数, 文档字符预算, 触发技能数)
LADDER = [
    {"tier": 0, "name": "轻量", "model": "qwen3-coder:30b", "skill_slots": 6,
     "doc_chars": 6000, "need_skills": 0},
    {"tier": 1, "name": "标准", "model": "qwen3-vl:30b-a3b-instruct", "skill_slots": 10,
     "doc_chars": 12000, "need_skills": 20},
    {"tier": 2, "name": "推理", "model": "deepseek-r1:70b", "skill_slots": 16,
     "doc_chars": 24000, "need_skills": 45},
]

MIN_FREE_GB = {"qwen3-coder:30b": 22, "qwen3-vl:30b-a3b-instruct": 24,
               "deepseek-r1:70b": 48}


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(obj):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)


def _state():
    st = _read(STORE, None)
    if st:
        return st
    # 首次运行：以 config.json 里实配的模型为准推断档位，
    # 否则会出现"阶梯说轻量、实际在跑 70B"的自相矛盾。
    tier = 0
    try:
        cfg = _read(os.path.join(os.path.expanduser("~"), ".dgx_studio", "config.json"), {})
        cur = (cfg.get("model") or "").strip()
        for i, t in enumerate(LADDER):
            if t["model"] == cur:
                tier = i
                break
    except Exception:
        pass
    return {"tier": tier, "history": [], "since": int(time.time())}


def _installed_models():
    """Ollama 里实际有的模型（只读，不压模型）"""
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True,
                             timeout=20, encoding="utf-8", errors="ignore").stdout
        return {ln.split()[0] for ln in out.splitlines()[1:] if ln.strip()}
    except Exception:
        return set()


def _gpu_free_gb():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15).stdout
        vals = [int(x.strip()) for x in out.split() if x.strip().isdigit()]
        return max(vals) / 1024.0 if vals else 0.0
    except Exception:
        return 0.0


def snapshot(skill_count=0):
    st = _state()
    tier = LADDER[min(max(int(st.get("tier", 0)), 0), len(LADDER) - 1)]
    # 实际在跑的模型以 config.json 为准；阶梯里的 model 是"该档推荐用哪个"。
    # 两者分开报，避免出现"阶梯说轻量、实际跑 70B"这种自相矛盾。
    try:
        cfg = _read(os.path.join(os.path.expanduser("~"), ".dgx_studio",
                                 "config.json"), {})
        configured = (cfg.get("model") or "").strip()
    except Exception:
        configured = ""
    return {
        "tier": tier["tier"], "name": tier["name"],
        "recommend_model": tier["model"], "configured_model": configured,
        "skill_slots": tier["skill_slots"], "doc_chars": tier["doc_chars"],
        "need_skills": tier["need_skills"],
        "skill_count": skill_count,
        "next": (LADDER[tier["tier"] + 1] if tier["tier"] + 1 < len(LADDER) else None),
        "since": st.get("since"), "history": st.get("history", [])[-10:],
    }


def evaluate(skill_count):
    """判定是否该升档。返回 (是否升级, 理由)。只判定不写盘，写盘在 promote()。"""
    st = _state()
    cur = int(st.get("tier", 0))
    if cur + 1 >= len(LADDER):
        return False, "已在最高档"
    nxt = LADDER[cur + 1]
    if skill_count < nxt["need_skills"]:
        return False, "技能数 %d < 升级门槛 %d" % (skill_count, nxt["need_skills"])
    models = _installed_models()
    if nxt["model"] not in models:
        return False, "Ollama 无 %s（需先 ollama pull，不自动拉以免占盘）" % nxt["model"]
    free = _gpu_free_gb()
    need = MIN_FREE_GB.get(nxt["model"], 24)
    if free and free < need:
        return False, "显存空闲 %.1fGB < %.0fGB" % (free, need)
    gpu_note = ("显存 %.1fGB" % free) if free else "显存读数不可用（GB10 统一内存，按可用放行）"
    return True, "技能数 %d 达标 + %s 已就位 + %s" % (
        skill_count, nxt["model"], gpu_note)


def promote(skill_count, reason=""):
    st = _state()
    cur = int(st.get("tier", 0))
    if cur + 1 >= len(LADDER):
        return snapshot(skill_count)
    nxt = LADDER[cur + 1]
    st["tier"] = nxt["tier"]
    st["since"] = int(time.time())
    st.setdefault("history", []).append({
        "ts": int(time.time()), "from": cur, "to": nxt["tier"],
        "model": nxt["model"], "skills": skill_count, "reason": reason})
    _write(st)
    return snapshot(skill_count)


def demote(reason=""):
    st = _state()
    cur = int(st.get("tier", 0))
    if cur <= 0:
        return snapshot(0)
    st["tier"] = cur - 1
    st["since"] = int(time.time())
    st.setdefault("history", []).append({
        "ts": int(time.time()), "from": cur, "to": cur - 1, "reason": reason})
    _write(st)
    return snapshot(0)
