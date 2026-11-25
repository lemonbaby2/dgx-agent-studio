# -*- coding: utf-8 -*-
"""
skills · 可插拔技能引擎（让 Agent 从「聊天框」变成「会干活的 Agent」）
------------------------------------------------------------------
一个技能 = 一个 YAML 文件。三种类型：

1) type: prompt  —— 不改工具，只给 LLM 换一副"专家脑子"（系统提示词 + 任务模板）
   例：/ model-check  ->  注入"YOLO 类别一致性校验专家"的角色 + 检查清单
2) type: script  —— 执行一串动作（本机 shell / 远端 SSH），把结果喂回 LLM 总结
   例：/ dgx-health ->  ssh 过去跑 nvidia-smi/df/systemctl，回来让 LLM 判读
3) type: hybrid  —— 先跑脚本拿事实，再套 prompt 让 LLM 分析（最有用）

目录约定：
    <app>/skops/*.yaml       内置通用技能
    ~/.dgx_studio/skops/*.yaml   用户自建（优先生效）

触发：用户在输入框打 /<id> 或自然语言命中 trigger 关键词即可。
"""
from __future__ import annotations

import fnmatch
import glob
import os
import re
import shutil
import subprocess
import time

DATA_SKILLS = os.path.join(os.path.expanduser("~"), ".dgx_studio", "skops")

_SIMPLE_TYPES = {"str": str, "int": int, "bool": bool}


# ------------------------------------------------------------ YAML 极简解析
def _parse_yaml(text):
    """
    只支持本项目用到的子集：顶层键、列表、- key: val、| 块。
    目的：避免依赖 pyyaml（打包exe时少一个依赖）。
    若环境里有 pyyaml 则优先用它。
    """
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except Exception:
        pass

    data = {}
    cur_list = None
    cur_key = None
    block_key = None
    block_lines = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if block_key is not None:
            if line.strip() and (line.startswith("    ") or not line.strip()):
                block_lines.append(line[4:] if line.startswith("    ") else line)
                i += 1
                continue
            data[block_key] = "\n".join(block_lines).strip("\n")
            block_key, block_lines = None, []
            # 不 continue，重新处理当前行

        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue

        stripped = line.strip()
        ind = len(line) - len(line.lstrip())

        # 顶层: key: value  或 key: |
        m = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", stripped)
        if m and ind == 0:
            k, v = m.group(1), m.group(2).strip()
            if v == "|" or v == ">":
                block_key, block_lines = k, []
            elif v.startswith("[") and v.endswith("]"):
                data[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            elif v in ("", None):
                data[k] = []
                cur_list = k
            else:
                data[k] = v.strip("'\"")
            cur_key = k
            i += 1
            continue

        # 列表项: - xxx  或 - key: val
        if stripped.startswith("- "):
            item = stripped[2:].strip()
            if cur_list is None:
                i += 1
                continue
            m2 = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", item)
            if m2:
                d = {m2.group(1): m2.group(2).strip("'\"")}
                # 收集同级的 key: val 续行
                j = i + 1
                while j < len(lines) and re.match(r"^\s+[A-Za-z0-9_.-]+:", lines[j]):
                    mm = re.match(r"^\s*([A-Za-z0-9_.-]+):\s*(.*)$", lines[j])
                    if mm:
                        d[mm.group(1)] = mm.group(2).strip("'\"")
                    j += 1
                data.setdefault(cur_list, []).append(d)
                i = j
                continue
            data.setdefault(cur_list, []).append(item.strip("'\""))
            i += 1
            continue

        i += 1

    if block_key is not None:
        data[block_key] = "\n".join(block_lines).strip("\n")
    return data


# ------------------------------------------------------------ 加载 / 匹配
def skill_dirs(app_dir=None):
    dirs = []
    if app_dir:
        dirs.append(os.path.join(app_dir, "skops"))
    dirs.append(DATA_SKILLS)
    return [d for d in dirs if os.path.isdir(d)]


def load_skills(app_dir=None):
    """扫描技能目录 -> [{id,name,desc,trigger,type,system,prompt,steps,...}]

    ⚠️ 必须支持**两级子目录**。自学习生成的技能放在
    ~/.dgx_studio/skops/autolearn/*.yaml，早期的 glob 只扫 `d/*.yaml`，
    结果「技能文件生成了、日志说写成功、但技能引擎里一个都看不到」，
    表现为自动技能数一直是 0。踩过，别改回单层。
    """
    skills, seen = [], set()
    for d in skill_dirs(app_dir):
        files = (glob.glob(os.path.join(d, "*.yaml"))
                 + glob.glob(os.path.join(d, "*.yml"))
                 + glob.glob(os.path.join(d, "*", "*.yaml"))
                 + glob.glob(os.path.join(d, "*", "*.yml")))
        for f in sorted(files):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    raw = fh.read()
                y = _parse_yaml(raw)
                if not isinstance(y, dict) or not y.get("name"):
                    continue
                sid = str(y.get("id") or os.path.splitext(os.path.basename(f))[0])
                if sid in seen:
                    continue
                seen.add(sid)
                trig = y.get("trigger") or []
                if isinstance(trig, str):
                    trig = [trig]
                kw = y.get("keywords") or []
                if isinstance(kw, str):
                    kw = [kw]
                sk = {
                    "id": sid,
                    "name": str(y.get("name")),
                    "desc": str(y.get("description") or ""),
                    "trigger": [str(t).lower() for t in trig],
                    "keywords": [str(t).lower() for t in kw],
                    "type": str(y.get("type") or "prompt").lower(),
                    "system": str(y.get("system") or ""),
                    "prompt": str(y.get("prompt") or ""),
                    "steps": y.get("steps") or [],
                    "params": y.get("params") or [],
                    "file": f,
                }
                skills.append(sk)
            except Exception as e:
                print("[skills] 加载失败 %s: %s" % (f, e))
    return skills


def match_ranked(text, skills, top=3):
    """返回按分数排序的候选 [(skill, score), ...]

    打分口径（v13 收紧）：
      · trigger 命中 1.0（作者显式声明的高置信触发词）
      · keywords 命中 0.5（宽泛辅助词）
      · 技能名/ID 在句中直接出现 +1.5（"用电路设计技能" 这类显式指认）
      · 短词命中做长度折算：2 字以下触发词容易误伤（"电" "码"），降权 0.4

    设计意图：同一句话常常同时沾两个领域（"buck 电路的驱动"= 电路 + 嵌入式的"驱动"），
    与其硬选一个，不如把候选排序交给上层，让用户/模型看到分歧。
    """
    if not text:
        return []
    low = text.strip().lower()
    scored = []
    for s in skills:
        c = 0.0
        for k in s.get("trigger") or []:
            if k and k.lower() in low:
                c += 1.0 if len(k) >= 3 else 0.4
        for k in s.get("keywords") or []:
            if k and k.lower() in low:
                c += 0.5 if len(k) >= 3 else 0.2
        nm = (s.get("name") or "").lower()
        if nm and len(nm) >= 2 and nm in low:
            c += 1.5
        if s.get("id") and s["id"].lower() in low:
            c += 1.5
        if c > 0:
            scored.append((s, round(c, 2)))
    scored.sort(key=lambda x: -x[1])
    return scored[:top]


def match_skill(text, skills):
    """
    匹配技能：优先 /id 显式调用，其次关键词打分取最高。
    返回 (skill, args_text)
    """
    if not text:
        return None, ""
    t = text.strip()
    m = re.match(r"^/([A-Za-z0-9_.-]+)\s*(.*)$", t, re.S)
    if m:
        sid, args = m.group(1), m.group(2).strip()
        for s in skills:
            if s["id"] == sid or s["id"].endswith("-" + sid):
                return s, args
        # 也允许用 name 触发
        for s in skills:
            if s["name"] == sid:
                return s, args
        return None, ""
    r = match_ranked(t, skills, top=1)
    return (r[0][0], t) if r else (None, "")


def build_system(skill, args=""):
    """把 skill 的 system 拼成可被注入的角色提示"""
    if not skill:
        return ""
    head = skill["system"].strip()
    if not head:
        return ""
    return ("【当前技能：%s】\n%s\n" % (skill["name"], head))


def build_prompt(skill, args=""):
    """渲染 skill 的 prompt 模板，{{input}} 占位替换为用户输入"""
    if not skill:
        return args
    tpl = (skill["prompt"] or "{{input}}").strip()
    return tpl.replace("{{input}}", args or "").strip()


# ------------------------------------------------------------ 执行
def _run_step(step, ctx, timeout):
    kind = str(step.get("kind") or "local").lower()
    cmd = step.get("cmd") or step.get("run") or ""
    if not cmd:
        return False, "[空命令]"
    label = step.get("name") or cmd[:60]
    try:
        if kind in ("ssh", "remote"):
            host = step.get("host") or ctx.get("host")
            user = step.get("user") or ctx.get("user") or "nvidia"
            if not host:
                return False, "[%s] 缺少 host" % label
            full = ["ssh", "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=10", "%s@%s" % (user, host), cmd]
        else:
            full = cmd if isinstance(cmd, list) else \
                (["cmd", "/c", cmd] if os.name == "nt" else ["bash", "-lc", cmd])
        p = subprocess.run(full, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "ignore")
        err = (p.stderr or b"").decode("utf-8", "ignore")
        body = (out + ("\n[stderr] " + err if err.strip() else "")).strip()
        if len(body) > 8000:
            body = body[:8000] + "\n...(截断)"
        return True, "### %s\n%s" % (label, body or "(无输出)")
    except subprocess.TimeoutExpired:
        return False, "### %s\n[超时 %ss]" % (label, timeout)
    except Exception as e:
        return False, "### %s\n[异常] %s" % (label, e)


def run_steps(skill, ctx=None, timeout=120):
    """
    执行 script/hybrid 技能的 steps。
    返回 (ok, 汇总文本)
    """
    ctx = ctx or {}
    steps = skill.get("steps") or []
    if not steps:
        return True, ""
    parts, ok_all = [], True
    for st in steps:
        ok, txt = _run_step(st, ctx, int(st.get("timeout") or 0) or timeout)
        ok_all = ok_all and ok
        parts.append(txt)
    return ok_all, "\n\n".join(parts)


def install_user_skill(src_yaml):
    """把一个技能文件装到用户目录"""
    try:
        os.makedirs(DATA_SKILLS, exist_ok=True)
        dst = os.path.join(DATA_SKILLS, os.path.basename(src_yaml))
        shutil.copy(src_yaml, dst)
        return True, dst
    except Exception as e:
        return False, str(e)
