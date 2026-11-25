# -*- coding: utf-8 -*-
"""Agent 工具层（v11）—— 让云端大脑能真的动手

豆包 / 千问 / GPT 都支持 OpenAI 的 function calling 协议。
这里把 DGX 本机能力包装成标准工具，模型一个 tool_call 就能真的去读文件、
跑命令、看 GPU —— 而不是只会「建议你执行 xxx」。

安全三条（照抄项目铁律）：
  1. 文件读写一律走 fs_api.resolve_in_workspace()，出工作区 = 拒绝
  2. shell 命令一律过 core.is_dangerous()，命中 = 拒绝，不执行
  3. 工具回路有最大轮数，超过就停，防死循环烧钱
"""
import json
import mimetypes
import os
import re
import shlex
import subprocess
import glob as _glob

from . import core

try:
    from . import fs_api
except Exception:
    fs_api = None

try:
    from . import providers
except Exception:
    providers = None

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DIR = os.path.expanduser("~/DGX-Notes/kb")
MAX_ROUNDS = int(os.environ.get("AGENT_MAX_ROUNDS", "6"))


# ---------------------------------------------------------------------------
# 工具定义（OpenAI function calling schema）
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容（只能看已授权的工作区或知识库目录）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "目录绝对路径，留空则列工作区根"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容（限工作区内，最多返回 20000 字符）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入文件（限工作区内，自动建父目录）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标绝对路径"},
                    "content": {"type": "string", "description": "文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_cmd",
            "description": "在 DGX 上执行 shell 命令。危险命令（rm -rf/shutdown/mkfs 等）"
                           "会被拒绝。适合查状态、跑只读命令、Python 脚本。"
                           "不要猜测 /workspace，不要用本工具下载或打开文件；"
                           "下载用 download_file，打开用 open_file。若用户给的路径不存在或未授权，"
                           "先执行 pwd 并列出当前目录来恢复，不要重复尝试错误路径",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 180"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "machine_status",
            "description": "查看 DGX 的 GPU / 磁盘 / 内存 / 负载，判断是否适合跑重活",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_file",
            "description": (
                "把 DGX 工作区文件交给当前用户的网页客户端预览或下载。"
                "用户说打开、预览、查看 PDF/图片/文件时必须用本工具，"
                "禁止使用 xdg-open/open/start 等服务器桌面命令"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内文件绝对路径"},
                    "mode": {"type": "string", "enum": ["preview", "download"],
                             "description": "默认 preview；用户明确要求下载时用 download"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_file",
            "description": (
                "从 HTTP/HTTPS 下载文件到已授权工作区，自动跟随重定向、原子写入并验证。"
                "下载 PDF 时会检查 %PDF 文件头，避免把网页错误页误报成 PDF。"
                "用户要求下载文件时优先使用本工具，禁止用 curl/wget 冒充成功"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "HTTP/HTTPS 下载地址"},
                    "path": {"type": "string", "description": "工作区内目标绝对路径"},
                },
                "required": ["url", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_search",
            "description": "在自学习沉淀的知识库里全文搜索（~/DGX-Notes/kb 的 Markdown）",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "integer", "description": "最多返回几条，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skills_list",
            "description": "列出当前已加载的所有技能及其触发词",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索最新信息（技术问题、报错、文档）。不需要密钥",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "integer", "description": "结果条数，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": ("在本机 GPU 上生成图片（SD 模型，不出网、不花钱）。"
                            "中文提示词会自动翻成英文。返回可直接显示的图片链接，"
                            "务必把 markdown 图片语法 ![](url) 原样输出给用户。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string",
                               "description": "画面描述，中英文都行，越具体越好"},
                    "negative": {"type": "string",
                                 "description": "不想出现的东西，可留空"},
                    "width": {"type": "integer", "description": "宽，默认 512（SDXL 1024）"},
                    "height": {"type": "integer", "description": "高，默认 512（SDXL 1024）"},
                    "steps": {"type": "integer", "description": "采样步数，turbo 模型默认 4"},
                    "seed": {"type": "integer", "description": "随机种子，便于复现"},
                },
                "required": ["prompt"],
            },
        },
    },
]


def tool_names():
    return [t["function"]["name"] for t in TOOLS]


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def _safe_path(path):
    """走项目自带的工作区边界；失败则返回 None"""
    if not path:
        return None
    p = os.path.abspath(os.path.expanduser(path))
    if fs_api:
        try:
            ok = fs_api.resolve_in_workspace(p)
            if ok:
                return p
        except Exception:
            pass
    # 知识库是白名单例外（只读）
    if p.startswith(KB_DIR + os.sep) or p == KB_DIR:
        return p
    return None


def _t_list_dir(args):
    p = (args.get("path") or "").strip()
    if not p:
        try:
            ws = fs_api.load_workspaces() if fs_api else []
        except Exception:
            ws = []
        if ws:
            p = ws[0].get("path") if isinstance(ws[0], dict) else str(ws[0])
        else:
            p = APP_DIR
    safe = _safe_path(p)
    if not safe:
        return {"error": "路径不在已授权的工作区内：%s" % p}
    if not os.path.isdir(safe):
        return {"error": "不是目录或不存在：%s" % safe}
    try:
        items = []
        for name in sorted(os.listdir(safe))[:200]:
            fp = os.path.join(safe, name)
            items.append({
                "name": name,
                "dir": os.path.isdir(fp),
                "size": os.path.getsize(fp) if os.path.isfile(fp) else 0,
            })
        return {"path": safe, "count": len(items), "items": items[:100]}
    except Exception as e:
        return {"error": str(e)[:200]}


def _t_read_file(args):
    p = (args.get("path") or "").strip()
    safe = _safe_path(p)
    if not safe:
        return {"error": "路径不在已授权的工作区内：%s" % p}
    if not os.path.isfile(safe):
        return {"error": "文件不存在：%s" % safe}
    try:
        with open(safe, "r", encoding="utf-8", errors="ignore") as f:
            s = f.read(20000)
        return {"path": safe, "chars": len(s), "content": s}
    except Exception as e:
        return {"error": str(e)[:200]}


def _t_write_file(args):
    p = (args.get("path") or "").strip()
    content = args.get("content") or ""
    safe = _safe_path(p)
    if not safe:
        return {"error": "路径不在已授权的工作区内，禁止写入：%s" % p}
    try:
        os.makedirs(os.path.dirname(safe), exist_ok=True)
        with open(safe, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "path": safe, "bytes": len(content.encode("utf-8"))}
    except Exception as e:
        return {"error": str(e)[:200]}


def _t_open_file(args):
    p = (args.get("path") or "").strip()
    safe = _safe_path(p)
    if not safe:
        return {"error": "路径不在已授权的工作区内：%s" % p}
    if not os.path.isfile(safe):
        return {"error": "文件不存在：%s" % safe}
    if os.path.splitext(safe)[1].lower() == ".pdf":
        try:
            with open(safe, "rb") as handle:
                if handle.read(5) != b"%PDF-":
                    return {"error": "文件扩展名是 PDF，但文件头不是 %PDF-，可能下载到了网页错误页"}
        except Exception as e:
            return {"error": "PDF 校验失败：%s" % str(e)[:160]}
    mode = (args.get("mode") or "preview").strip().lower()
    if mode not in ("preview", "download"):
        mode = "preview"
    mime = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    return {"ok": True, "action": mode, "path": safe,
            "name": os.path.basename(safe), "size": os.path.getsize(safe),
            "mime": mime}


def _t_download_file(args):
    import urllib.parse
    import urllib.request
    url = (args.get("url") or "").strip()
    target = (args.get("path") or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"error": "只允许 HTTP/HTTPS 下载地址"}
    safe = _safe_path(target)
    if not safe:
        return {"error": "目标路径不在已授权工作区内：%s" % target}
    os.makedirs(os.path.dirname(safe), exist_ok=True)
    part = safe + ".part"
    max_bytes = 256 * 1024 * 1024
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 DGX-Agent-Studio/16"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=180) as response, open(part, "wb") as handle:
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0]
            final_url = response.geturl()
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("文件超过 256MB 限制")
                handle.write(chunk)
        with open(part, "rb") as handle:
            header = handle.read(8)
        if os.path.splitext(safe)[1].lower() == ".pdf" and not header.startswith(b"%PDF-"):
            raise ValueError("下载结果不是有效 PDF（文件头不是 %PDF-，Content-Type=%s）" % content_type)
        os.replace(part, safe)
        return {"ok": True, "path": safe, "bytes": total,
                "content_type": content_type, "final_url": final_url,
                "header": header.decode("ascii", "replace")}
    except Exception as e:
        try:
            if os.path.isfile(part):
                os.remove(part)
        except Exception:
            pass
        return {"error": "下载或校验失败：%s" % str(e)[:300], "url": url}


# 带 CUDA torch 的解释器（系统 python3 没有任何 AI 库）
SAFE_PYTHON = "/home/gpu/isaacsim-venv/bin/python"


def _t_run_cmd(args):
    cmd = (args.get("command") or args.get("cmd") or "").strip()
    if not cmd:
        return {"error": "命令为空"}
    # DGX 服务没有图形桌面。把模型常见的 xdg-open 误调用转换成客户端预览。
    try:
        parts = shlex.split(cmd)
    except Exception:
        parts = []
    if len(parts) == 2 and parts[0] in ("xdg-open", "open"):
        return _t_open_file({"path": parts[1], "mode": "preview"})
    if len(parts) == 3 and parts[:2] == ["gio", "open"]:
        return _t_open_file({"path": parts[2], "mode": "preview"})
    if core.is_dangerous(cmd):
        return {"error": "命中危险命令黑名单，已拒绝执行：%s" % cmd[:120]}
    # 禁止 agent 改动系统环境：DGX 是 ARM aarch64 + 定制 CUDA torch，
    # pip/apt 装包会破坏现有编译版本，且 sudo 需要交互密码。
    if re.search(r"\b(sudo|pip3?\s+install|apt(?:-get)?\s+install|"
                 r"--break-system-packages)\b", cmd, re.I):
        return {"error": "拒绝修改系统环境（sudo / pip install / apt install）。"
                         "本机已预装带 CUDA torch 的解释器，直接用绝对路径调用："
                         "\n  /home/gpu/isaacsim-venv/bin/python"
                         "\n  /home/gpu/anaconda3/envs/yolo-sop/bin/python3"
                         "\n  /home/gpu/sop-platform/.venv/bin/python"
                         "\n系统 python3 没有 torch，但 venv 里有，不需要安装任何东西。"}
    try:
        to = int(args.get("timeout") or 60)
        to = max(5, min(to, 180))
    except Exception:
        to = 60
    # 裸 python3/python 指向系统解释器（无 torch）。对算力任务自动改写到带 CUDA 的 venv，
    # 否则模型会反复报 ModuleNotFoundError 并试图 pip install。
    run_cmd = cmd
    m = re.match(r"^\s*(python3?)(\s+.*)$", cmd)
    if m and os.path.isfile(SAFE_PYTHON):
        run_cmd = SAFE_PYTHON + m.group(2)
    rc, out = core.run_command(run_cmd, cwd=APP_DIR, timeout=to)
    res = {"command": cmd, "returncode": rc, "output": (out or "")[:6000]}
    if run_cmd != cmd:
        res["interpreter_rewritten"] = SAFE_PYTHON
    return res


def _t_machine_status(args):
    rc, out = core.run_command(
        "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,"
        "power.draw,temperature.gpu --format=csv,noheader 2>/dev/null; "
        "echo '--'; df -h / | tail -1; echo '--'; free -g | head -2 | tail -1; "
        "echo '--'; uptime", timeout=30)
    lines = [x.strip() for x in (out or "").splitlines() if x.strip()]
    info = {"raw": out}
    try:
        i2 = lines.index("--")
        info["gpu"] = lines[0] if lines else ""
        rest = lines[i2 + 1:]
        for seg in rest:
            pass
        if rest:
            info["disk"] = rest[0]
        if len(rest) > 1:
            info["mem"] = rest[1]
        if len(rest) > 2:
            info["load"] = rest[2]
    except Exception:
        pass
    return info


def _t_kb_search(args):
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "缺少关键词"}
    try:
        limit = max(1, min(int(args.get("limit") or 5), 20))
    except Exception:
        limit = 5
    hits = []
    if not os.path.isdir(KB_DIR):
        return {"hits": [], "note": "知识库目录不存在"}
    pat = re.compile(re.escape(q), re.I)
    for f in sorted(_glob.glob(os.path.join(KB_DIR, "*.md")), reverse=True)[:60]:
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                s = fh.read(60000)
        except Exception:
            continue
        if pat.search(s) or pat.search(os.path.basename(f)):
            idx = [m.start() for m in pat.finditer(s)][:3]
            snips = []
            for i in idx:
                snips.append(s[max(0, i - 90):i + 150].replace("\n", " "))
            hits.append({"file": os.path.basename(f), "snippets": snips})
        if len(hits) >= limit:
            break
    return {"query": q, "hits": hits, "count": len(hits)}


def _t_skills_list(args):
    try:
        from . import skills as _sk
        ss = _sk.load_skills(APP_DIR)
        return {"count": len(ss),
                "skills": [{"id": x.get("id"), "name": x.get("name"),
                            "desc": (x.get("desc") or "")[:120],
                            "trigger": (x.get("trigger") or [])[:6]}
                           for x in ss]}
    except Exception as e:
        return {"error": str(e)[:200]}


def _t_web_search(args):
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "缺少关键词"}
    try:
        limit = max(1, min(int(args.get("limit") or 5), 10))
    except Exception:
        limit = 5
    import urllib.parse
    import urllib.request
    import html as _html
    try:
        url = ("https://html.duckduckgo.com/html/?q=" +
               urllib.parse.quote(q))
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux aarch64)"})
        with urllib.request.build_opener(
                urllib.request.ProxyHandler({})).open(req, timeout=25) as r:
            raw = r.read().decode("utf-8", "ignore")
        out = []
        for m in re.finditer(
                r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                raw, re.S) or []:
            link = _html.unescape(m.group(1))
            title = re.sub(r"<[^>]+>", "", _html.unescape(m.group(2))).strip()
            if "uddg=" in link:
                try:
                    link = urllib.parse.unquote(
                        link.split("uddg=")[1].split("&")[0])
                except Exception:
                    pass
            out.append({"title": title[:150], "url": link})
            if len(out) >= limit:
                break
        if out:
            return {"query": q, "results": out}
        # 退化：直接抓 Bing
        url2 = "https://www.bing.com/search?q=" + urllib.parse.quote(q)
        req2 = urllib.request.Request(
            url2, headers={"User-Agent": "Mozilla/5.0 (X11; Linux aarch64)"})
        with urllib.request.build_opener(
                urllib.request.ProxyHandler({})).open(req2, timeout=25) as r:
            raw2 = r.read().decode("utf-8", "ignore")
        for m in re.finditer(r'<h2><a href="([^"]+)"[^>]*>(.*?)</a></h2>', raw2, re.S):
            out.append({"title": re.sub(r"<[^>]+>", "", _html.unescape(
                m.group(2))).strip()[:150], "url": _html.unescape(m.group(1))})
            if len(out) >= limit:
                break
        return {"query": q, "results": out}
    except Exception as e:
        return {"error": "搜索失败：%s" % str(e)[:200]}


def _t_generate_image(args):
    """本地 GPU 生图（ComfyUI）"""
    try:
        from . import image_api
    except Exception as e:
        return {"error": "生图模块未加载：%s" % str(e)[:150]}
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt 不能为空"}
    r = image_api.generate(
        prompt,
        negative=(args.get("negative") or "").strip(),
        width=int(args.get("width") or 0),
        height=int(args.get("height") or 0),
        steps=int(args.get("steps") or 0),
        seed=args.get("seed") if isinstance(args.get("seed"), int) else None,
    )
    if not r.get("ok"):
        return {"error": r.get("error") or "生图失败"}
    # 给模型一个可以直接抄进回答里的 markdown 片段
    r["markdown"] = "![%s](%s)" % (r.get("prompt_zh", "")[:40], r["url"])
    return r


IMPL = {
    "list_dir": _t_list_dir,
    "read_file": _t_read_file,
    "write_file": _t_write_file,
    "run_cmd": _t_run_cmd,
    "machine_status": _t_machine_status,
    "open_file": _t_open_file,
    "download_file": _t_download_file,
    "kb_search": _t_kb_search,
    "skills_list": _t_skills_list,
    "web_search": _t_web_search,
    "generate_image": _t_generate_image,
}


def dispatch(name, args):
    """执行单个工具；未知工具 / 参数异常都要返回结构化错误，不能抛给上层"""
    fn = IMPL.get((name or "").strip())
    if not fn:
        return {"error": "未知工具：%s（可用：%s）" % (name, ", ".join(tool_names()))}
    if not isinstance(args, dict):
        args = {}
    try:
        return fn(args)
    except Exception as e:
        return {"error": "工具执行异常：%s" % str(e)[:200]}


def parse_args(raw):
    """模型的 arguments 有时不是合法 JSON，兜一层"""
    if isinstance(raw, dict):
        return raw
    s = (raw or "").strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        try:
            return json.loads(s + "}")
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# 工具回路
# ---------------------------------------------------------------------------
def run_with_tools(pid, model, messages, on_delta=None, on_think=None,
                   on_tool=None, use_tools=True, temperature=0.7,
                   stop_flag=None, max_rounds=None, timeout=300):
    """带工具的完整回路。返回 {"content","thinking","tools":[日志],"rounds"}

    模型要求：支持工具调用；本地 Ollama 使用原生 tools 协议。
    """
    if providers is None:
        return {"content": "", "thinking": "", "tools": [],
                "error": "providers 模块未加载"}
    ptype = (providers.PROVIDERS.get(pid, {}) or {}).get("type")
    if use_tools and ptype == "ollama":
        local_names = {"list_dir", "read_file", "write_file", "run_cmd",
                       "machine_status", "open_file", "download_file"}
        tools = [t for t in TOOLS if t["function"]["name"] in local_names]
    else:
        tools = TOOLS if (use_tools and ptype == "openai") else None

    msgs = list(messages)
    log = []
    thinking = ""
    maxr = int(max_rounds or MAX_ROUNDS)

    def hit(fn, t):
        if fn and t:
            try:
                fn(t)
            except Exception:
                pass

    for rnd in range(maxr):
        if stop_flag is not None and stop_flag.is_set():
            break
        content, th, tcs = providers.chat_stream(
            pid, model, msgs, tools=tools,
            on_delta=on_delta, on_think=on_think,
            temperature=temperature, stop_flag=stop_flag, timeout=timeout)
        thinking += th or ""

        if not tcs:
            return {"content": content, "thinking": thinking,
                    "tools": log, "rounds": rnd + 1}

        # 有工具调用：把 assistant 的 tool_calls 追加进上下文再逐个执行
        msgs.append({"role": "assistant", "content": content or "",
                     "tool_calls": tcs})
        for tc in tcs:
            if stop_flag is not None and stop_flag.is_set():
                break
            fname = (tc.get("function") or {}).get("name") or ""
            fargs = parse_args((tc.get("function") or {}).get("arguments"))
            entry = {"round": rnd + 1, "name": fname, "args": fargs}
            if on_tool:
                hit(on_tool, {"name": fname, "args": fargs, "index": len(log)})
            res = dispatch(fname, fargs)
            entry["result"] = res if isinstance(res, dict) else {"raw": str(res)}
            entry["ok"] = ("error" not in entry["result"] and
                           not (fname == "run_cmd" and
                                entry["result"].get("returncode", 0) != 0))
            log.append(entry)
            try:
                payload = json.dumps(entry["result"], ensure_ascii=False)[:12000]
            except Exception:
                payload = str(entry["result"])[:12000]
            msgs.append({"role": "tool", "tool_call_id": tc.get("id") or "",
                         "name": fname, "content": payload})

    # 超轮数：再问一次让它总结
    try:
        content, th, _ = providers.chat_stream(
            pid, model, msgs, tools=None, temperature=temperature,
            stop_flag=stop_flag, timeout=timeout)
        thinking += th or ""
    except Exception as e:
        content = "（工具回路已达最大轮数 %d，且最后一次生成失败：%s）" % (maxr, e)
    return {"content": content, "thinking": thinking, "tools": log,
            "rounds": maxr}


# ---------------------------------------------------------------------------
# v12：本地模型的自主执行回路（ReAct 文本协议，不依赖 function calling）
#
# 背景：run_with_tools 只对支持 OpenAI function calling 的云厂商生效，
#      本地 Ollama 模型（deepseek-r1:70b 等）协议不同、用不上工具。
# 方案：把工具清单写进 system 提示，约定模型用 JSON 文本发工具调用，
#      服务端解析→执行→把结果回填对话，循环到模型给出最终答案。
#      任何会认字本地模型都能跑，r1 的思维链还能顺带当「过程展示」。
# ---------------------------------------------------------------------------
LOCAL_MAX_ROUNDS = int(os.environ.get("AGENT_LOCAL_MAX_ROUNDS", "12"))

REACT_GUIDE = """

## 自主执行模式（重要，必须严格遵守）
你是能真正动手的执行型智能体，可以使用以下工具完成用户的实际任务：
{tools_desc}

协议（严格遵守）：
1. 动手前先用不超过 4 行给出执行计划。
2. 需要执行命令或查数据时，【禁止】把命令写进 ```bash 代码块——那样它不会被执行。
   正确做法：回复中输出且只输出一个 JSON 对象（不要放进代码块、不要加解释）：
   {{"tool": "工具名", "args": {{...}}}}
3. 系统执行后把结果以「[工具结果]」回发给你，你再决定下一步。
4. 一次只调用一个工具；同一工具失败两次就换思路，不要原地重试。
5. 信息足够后，输出最终答案（不要再输出工具 JSON）：要完整、有结论、可执行。
6. 用户要求你执行的任务必须由你执行并检查结果。禁止把命令、脚本或“下一步”交给用户运行。
7. 用户要求打开、预览或下载文件时，调用 open_file。禁止调用 xdg-open/open/start。
8. 用户要求从网络下载文件时，调用 download_file；不要用 curl/wget 判断下载是否成功。

示例（严格照此格式）：
用户：机器内存还剩多少？
你的回复：我先查机器状态。
{{"tool": "machine_status", "args": {{}}}}
（系统回发 [工具结果] machine_status ...）
你的最终回复：机器当前内存可用 39GB，其中 GPU 已占用 47GB。
"""


NATIVE_TOOL_GUIDE = """

## Agent 工具执行纪律
你必须使用工具真正完成并验证用户要求的任务，不能把尚未执行的命令或下一步交给用户。
工具失败时必须读取结果、换一种有依据的方法继续，并在最终答案中如实说明；不得把非零退出码说成成功。
不要假设工作区是 /workspace。用户给出的路径不存在或未授权时，立即用 run_cmd 执行 pwd 并列出当前目录，
然后基于真实返回路径继续；不要连续重试同一错误路径。

【本机 Python 环境（硬事实，直接用，不要再试探）】
- 系统 `python3` 没有任何 AI/科学库，`import torch` 必然报 ModuleNotFoundError，禁止用它跑模型或 GPU 任务。
- 以下解释器带 CUDA torch，必须写绝对路径：
  /home/gpu/isaacsim-venv/bin/python              (torch 2.11.0+cu130)
  /home/gpu/anaconda3/envs/yolo-sop/bin/python3   (torch 2.13.0+cu130，训练用)
  /home/gpu/sop-platform/.venv/bin/python        (torch 2.13.0+cu130)
- 默认工作目录 = /home/gpu/DGX-Agent-Studio；已授权目录只有它和 /home/gpu/Desktop/workspace。
  其它路径 list_dir/read_file/write_file 会被拒绝，但 run_cmd 可以访问全机（除危险命令）。
- 硬件：NVIDIA GB10（Grace-Blackwell，ARM aarch64，统一内存 121GB）。显存是共享的，
  `nvidia-smi --query-gpu=memory.total` 返回 [N/A] 属正常，不是故障，改用 free -g 看内存。
下载文件只能用 download_file；打开或预览文件只能用 open_file，禁止用 xdg-open、gio open 或 open。
只有任务确实完成或已经遇到无法在现有工具内解决的明确阻塞时，才输出最终答案。
"""


def _tools_brief():
    lines = []
    for t in TOOLS:
        fn = t["function"]
        desc = (fn.get("description") or "").split("。")[0].split("\n")[0]
        lines.append("- %s：%s" % (fn["name"], desc))
    return "\n".join(lines)


_TOOL_RE = re.compile(r'\{\s*"tool"\s*:\s*"([a-zA-Z_]+)"\s*,\s*"args"\s*:', re.I)
_TOOL_RE2 = re.compile(r'\{\s*"name"\s*:\s*"([a-zA-Z_]+)"\s*,\s*"arguments"\s*:', re.I)


def extract_tool_call(text):
    """从模型输出中提取工具调用 JSON（括号配平，容忍尾逗号/单引号）"""
    if not text:
        return None
    m = _TOOL_RE.search(text) or _TOOL_RE2.search(text)
    if not m:
        return None
    start = text.find("{", m.start())
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                blob = text[start:i + 1]
                try:
                    d = json.loads(blob)
                except Exception:
                    try:
                        d = json.loads(blob.replace("'", '"'))
                    except Exception:
                        return None
                name = d.get("tool") or d.get("name") or ""
                if not name:
                    return None
                args = d.get("args") or d.get("arguments") or {}
                return {"name": str(name), "args": args if isinstance(args, dict) else {}}
    return None


def _bash_block_cmd(text):
    """兜底桥接：模型没按协议发 JSON、而是写了单行 bash 代码块时，
    把第一条命令取出来当作 run_cmd 执行（仍要过危险命令黑名单）。
    r1 这类推理模型经常无视协议直接写命令，这个桥能让它照样动起来。"""
    if not text:
        return None
    m = re.search(r"```(?:bash|sh|shell|console)?[ \t]*\r?\n(.*?)```", text, re.S)
    if not m:
        return None
    code = m.group(1).strip()
    if (not code or "\n" in code or len(code) > 300
            or code.startswith("#!")):
        return None
    return code


_DELEGATION_RE = re.compile(
    r"请.{0,18}(?:运行|执行)|你可以.{0,18}(?:运行|执行)|"
    r"(?:please\s+)?run\s+(?:this|the)\s+(?:command|script)", re.I | re.S)


def _delegates_execution(text):
    return bool(text and _DELEGATION_RE.search(text))


def run_local_react(base, model, messages, on_delta=None, on_think=None,
                    on_tool=None, use_tools=True, temperature=0.7,
                    stop_flag=None, max_rounds=None, timeout=600):
    """本地 Ollama 模型的自主执行回路。返回结构与 run_with_tools 一致。"""
    if not use_tools:
        ans, th = core.chat_stream(base, model, messages,
                                   on_delta=on_delta, on_think=on_think,
                                   temperature=temperature, stop_flag=stop_flag,
                                   timeout=timeout)
        return {"content": ans, "thinking": th, "tools": [], "rounds": 1}

    guide = REACT_GUIDE.replace("{tools_desc}", _tools_brief())
    msgs = [dict(m) for m in messages]
    if tools:
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = (msgs[0].get("content") or "") + NATIVE_TOOL_GUIDE
        else:
            msgs.insert(0, {"role": "system", "content": NATIVE_TOOL_GUIDE.strip()})
    if msgs and msgs[0].get("role") == "system":
        msgs[0] = dict(msgs[0])
        msgs[0]["content"] = msgs[0]["content"] + guide
    else:
        msgs.insert(0, {"role": "system",
                        "content": "你是 DGX 上的执行型智能体。" + guide})

    log = []
    thinking = ""
    maxr = int(max_rounds or LOCAL_MAX_ROUNDS)

    def hit(fn, t):
        if fn and t:
            try:
                fn(t)
            except Exception:
                pass

    for rnd in range(maxr):
        if stop_flag is not None and stop_flag.is_set():
            break
        # 中间轮：所有输出都当「执行过程」走 on_think 展示
        ans, th = core.chat_stream(
            base, model, msgs, on_delta=on_think, on_think=on_think,
            temperature=temperature, stop_flag=stop_flag, timeout=timeout)
        thinking += th or ""
        text = ans or ""

        call = extract_tool_call(text)
        if not call and (_delegates_execution(text) or
                         (log and not log[-1].get("ok"))):
            msgs.append({"role": "assistant", "content": text})
            msgs.append({"role": "user",
                         "content": "任务尚未完成。不要让我运行命令；请立即调用工具继续执行，"
                                    "并在成功后读取或检查结果。"})
            continue
        if not call:
            # 协议兜底：模型写了单行 bash 代码块 → 自动转成 run_cmd 执行
            bcmd = _bash_block_cmd(text)
            if bcmd and not core.is_dangerous(bcmd):
                call = {"name": "run_cmd",
                        "args": {"command": bcmd, "timeout": 60}}
        if not call:
            # 最终答案：过程已进 think，这里把正文补发给前端渲染
            if on_delta and text.strip():
                on_delta(text)
            return {"content": text, "thinking": thinking,
                    "tools": log, "rounds": rnd + 1}

        hit(on_tool, {"name": call["name"], "args": call["args"],
                      "index": len(log)})
        res = dispatch(call["name"], call["args"])
        entry = {"round": rnd + 1, "name": call["name"], "args": call["args"],
                 "result": res if isinstance(res, dict) else {"raw": str(res)}}
        entry["ok"] = (isinstance(entry["result"], dict) and
                       "error" not in entry["result"] and
                       not (call["name"] == "run_cmd" and
                            entry["result"].get("returncode", 0) != 0))
        log.append(entry)
        try:
            payload = json.dumps(entry["result"], ensure_ascii=False)[:10000]
        except Exception:
            payload = str(entry["result"])[:10000]
        msgs.append({"role": "assistant", "content": text})
        msgs.append({"role": "user",
                     "content": "[工具结果] %s\n%s\n\n请继续；信息足够就给最终答案。"
                                % (call["name"], payload)})

    # 超轮数：强制收尾总结
    content = ""
    if stop_flag is None or not stop_flag.is_set():
        try:
            msgs.append({"role": "user",
                         "content": "已达工具调用轮数上限。请基于以上所有结果直接输出最终答案，"
                                    "不要再调用工具。"})
            ans, th = core.chat_stream(base, model, msgs, on_delta=on_delta,
                                       on_think=on_think, temperature=temperature,
                                       stop_flag=stop_flag, timeout=timeout)
            content = ans or ""
            thinking += th or ""
        except Exception as e:
            content = "（执行回路达最大轮数 %d，收尾失败：%s）" % (maxr, e)
    return {"content": content, "thinking": thinking, "tools": log, "rounds": maxr}
