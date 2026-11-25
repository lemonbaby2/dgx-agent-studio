# -*- coding: utf-8 -*-
"""
DGX Agent Studio · 后端核心
------------------------------------------------
零第三方依赖：只用 Python 标准库。
职责：配置、会话持久化、Ollama 客户端、本机命令执行。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import uuid

APP_NAME = "DGX Agent Studio"
APP_VER = "5.0"

# 数据目录：%USERPROFILE%\.dgx_studio
DATA_DIR = os.path.join(os.path.expanduser("~"), ".dgx_studio")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SESSIONS_PATH = os.path.join(DATA_DIR, "sessions.json")
WORKSPACES_PATH = os.path.join(DATA_DIR, "workspaces.json")

DEFAULT_BASE = "http://127.0.0.1:11434"
DEFAULT_BASES = ["http://127.0.0.1:11434", "http://127.0.0.1:11434"]
# v12（2026-10-26）：本地主力模型由 deepseek-r1:32b(19.9GB) 升级到
# deepseek-r1:70b(42.5GB)，接替 32b 作为深度推理主力。
# ⚠️ 换模型后必须同步：web/v8ui.js 的下拉与兜底值、embodied_api / paper_reader 的默认值、
#    ~/.dgx_studio/config.json 的 model 字段。缺失任一处都会让网站指向不存在的模型。
DEFAULT_MODEL = "deepseek-r1:70b"

# 本机模型被卸载/删除时的安全回落顺序（同族优先）
MODEL_FALLBACKS = [
    "deepseek-r1:70b",
    "qwen3-coder:30b",
    "qwen3-vl:30b-a3b-instruct",
]

MAX_HISTORY_MSGS = 40

DEFAULT_SYSTEM = (
    "你是运行在本地的 DGX 智能体，服务对象是工业视觉 / SOP 产线方向的工程师。"
    "回答要直接、准确、可执行；涉及代码时给出完整可运行片段；不确定的地方明确说明。"
    "【硬件事实】你所在机器是 NVIDIA DGX Spark（GB10 Grace-Blackwell 超级芯片，"
    "统一内存 128GB，ARM aarch64，Ubuntu 22.04），不是 DGX-1 / DGX-2 / V100 / A100 机型。"
    "被问到自身硬件时按此回答，不要臆测其他型号。"
)

TOOL_RE = re.compile(r"```tool\s*(\{.*?\})\s*```", re.S)

THINK_HINT = ("deepseek-r1", "qwq", "reasoning", "think")
VISION_HINT = ("llava", "minicpm-v", "qwen2.5vl", "qwen2-vl", "vision",
               "bakllava", "moondream")

_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 基础 IO
# ---------------------------------------------------------------------------
def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


def _read_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def _write_json(path, obj):
    """原子写：先写 .tmp 再替换，避免中断损坏文件"""
    ensure_data_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config():
    cfg = {
        "base": DEFAULT_BASE,
        "bases": list(DEFAULT_BASES),
        "model": DEFAULT_MODEL,
        "system": DEFAULT_SYSTEM,
        "temperature": 0.7,
        "show_think": True,
        "agent": False,
        "cwd": "",
        "max_steps": 6,
        # v6：远端 DGX 连接信息（用于 SSH 类技能体检、远端视频抽帧、远端 ASR）
        "ssh_host": "127.0.0.1",
        "ssh_user": "nvidia",
        "ssh_python": "python3",
        # v6：语音偏好
        "tts_backend": "auto",
        "tts_voice": "zh-CN-XiaoxiaoNeural",
        "auto_tts": False,
    }
    cfg.update(_read_json(CONFIG_PATH, {}) or {})
    bases = [b for b in (cfg.get("bases") or []) if b]
    if cfg.get("base") not in bases:
        bases.insert(0, cfg["base"])
    cfg["bases"] = list(dict.fromkeys(bases))
    return cfg


def save_config(cfg):
    with _LOCK:
        _write_json(CONFIG_PATH, cfg)
    return cfg


def patch_config(patch):
    cfg = load_config()
    cfg.update(patch or {})
    return save_config(cfg)


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
class Session:
    def __init__(self, sid=None, title="新对话", model=DEFAULT_MODEL,
                 base=DEFAULT_BASE, system=None, owner=None, owner_name="",
                 provider="ollama", agent_mode=False):
        self.id = sid or uuid.uuid4().hex[:12]
        self.title = title
        self.model = model
        self.base = base
        self.system = system if system is not None else DEFAULT_SYSTEM
        self.messages = []
        self.created = time.time()
        self.updated = time.time()
        # v10 多人：会话归属。owner 为空 = 公共/历史会话（老数据），所有人可见
        self.owner = owner or ""
        self.owner_name = owner_name or ""
        # v11 多云：provider=本地 ollama / 豆包 ark / 千问 qwen / deepseek …
        #      agent_mode=是否允许调用工具（需云模型支持 function calling）
        self.provider = (provider or "ollama").strip()
        self.agent_mode = bool(agent_mode)

    @property
    def meta(self):
        n = sum(1 for m in self.messages if m.get("role") == "user")
        return "%s · %d 轮" % (self.model, n)

    def preview(self):
        for m in self.messages:
            if m.get("role") == "user" and (m.get("content") or "").strip():
                return re.sub(r"\s+", " ", m["content"]).strip()[:60]
        return ""

    def auto_title(self):
        for m in self.messages:
            if m.get("role") == "user" and (m.get("content") or "").strip():
                t = re.sub(r"\s+", " ", m["content"]).strip()
                self.title = t[:16] + ("…" if len(t) > 16 else "")
                return

    def to_dict(self):
        d = {"id": self.id, "title": self.title, "model": self.model,
             "base": self.base, "system": self.system,
             "created": self.created, "updated": self.updated,
             "meta": self.meta, "preview": self.preview(),
             "count": len(self.messages),
             "owner": self.owner, "owner_name": self.owner_name,
             "shared": not self.owner,
             "provider": self.provider, "agent_mode": self.agent_mode}
        msgs = []
        for m in self.messages:
            mm = {"role": m.get("role"), "content": m.get("content", "")}
            for k in ("n_images", "refs", "think", "tool_log"):
                if m.get(k):
                    mm[k] = m[k]
            msgs.append(mm)
        d["messages"] = msgs
        return d

    @classmethod
    def from_dict(cls, d):
        s = cls(sid=d.get("id"), title=d.get("title") or "新对话",
                model=d.get("model") or DEFAULT_MODEL,
                base=d.get("base") or DEFAULT_BASE,
                system=d.get("system") if d.get("system") is not None
                else DEFAULT_SYSTEM,
                owner=d.get("owner") or "",
                owner_name=d.get("owner_name") or "")
        # v11：老会话没有 provider → 归本地 ollama，行为与升级前完全一致
        s.provider = (d.get("provider") or "ollama").strip()
        s.agent_mode = bool(d.get("agent_mode"))
        s.created = d.get("created") or time.time()
        s.updated = d.get("updated") or s.created
        s.messages = []
        for m in d.get("messages") or []:
            item = {"role": m.get("role"), "content": m.get("content", "")}
            for k in ("n_images", "refs", "think", "tool_log"):
                if m.get(k):
                    item[k] = m[k]
            s.messages.append(item)
        return s


def load_sessions():
    d = _read_json(SESSIONS_PATH, {}) or {}
    return ([Session.from_dict(x) for x in (d.get("sessions") or [])],
            (d.get("current") or None))


def save_sessions(sessions, current):
    with _LOCK:
        _write_json(SESSIONS_PATH, {
            "version": 3,
            "current": current,
            "sessions": [s.to_dict() for s in sessions],
        })


# ---------------------------------------------------------------------------
# Ollama 客户端
# ---------------------------------------------------------------------------
def _opener():
    """绕过系统代理 —— 本机 Clash 会把内网请求打成 000"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def model_caps(name, capabilities=None):
    n = (name or "").lower()
    if capabilities:
        vis = "vision" in capabilities
        thk = "thinking" in capabilities
    else:
        vis = any(k in n for k in VISION_HINT)
        thk = any(k in n for k in THINK_HINT)
    tags = []
    if thk:
        tags.append("思维链")
    if vis:
        tags.append("视觉")
    return {"vision": vis, "think": thk, "tags": tags}


def _list_models_raw(base, timeout=10):
    url = base.rstrip("/") + "/api/tags"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore")).get("models", [])


def list_models_full(base, timeout=10):
    out = []
    for m in _list_models_raw(base, timeout):
        name = m.get("name")
        caps = m.get("capabilities") or []
        c = model_caps(name, caps)
        det = m.get("details") or {}
        out.append({
            "name": name,
            "capabilities": caps,
            "vision": c["vision"],
            "think": c["think"],
            "tags": c["tags"],
            "size": m.get("size") or 0,
            "digest": (m.get("digest") or "")[:12],
            "param": det.get("parameter_size") or "",
            "family": det.get("family") or "",
        })
    return out


def list_models(base, timeout=10):
    return [m["name"] for m in list_models_full(base, timeout)]


def health(base, timeout=6):
    url = base.rstrip("/") + "/api/version"
    req = urllib.request.Request(url)
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


class StreamStopper:
    def __init__(self):
        self._flag = threading.Event()

    def stop(self):
        self._flag.set()

    def is_set(self):
        return self._flag.is_set()

    def clear(self):
        self._flag.clear()


_TAGS_CACHE = {"ts": 0.0, "base": "", "names": []}


def _cached_names(base, ttl=60.0):
    """本机模型名缓存（默认 60s）。
    查不动就返回上一次结果或空 list —— 缓存失败绝不能阻断对话。"""
    key = (base or "").rstrip("/")
    now = time.time()
    if _TAGS_CACHE["base"] == key and (now - _TAGS_CACHE["ts"]) < ttl:
        return _TAGS_CACHE["names"]
    try:
        names = list_models(base, timeout=6)
    except Exception:
        return _TAGS_CACHE["names"] or []
    _TAGS_CACHE.update(ts=now, base=key, names=names)
    return names


def resolve_local_model(base, want=None):
    """把目标模型映射到本机真实存在的模型。

    存在的意义：会话/配置里记的是模型名，一旦那个模型被卸载、删掉或改名，
    对话会直接 404 报错。这里做三级兜底：
      1) 精确命中 → 用原模型
      2) 同族命中（deepseek-r1 换成哪个 tag 都认）→ 用同族实际 tag
      3) 顺着 MODEL_FALLBACKS 找下一个本机有的模型
      4) 都找不到 → 原样返回，交给 ollama 给明确报错
    """
    want = (want or DEFAULT_MODEL).strip()
    try:
        names = _cached_names(base)
    except Exception:
        return want
    if not names:
        return want
    for cand in [want] + list(MODEL_FALLBACKS):
        fam = cand.split(":")[0]
        for nm in names:
            if nm == cand:
                return nm
        for nm in names:
            if nm.split(":")[0] == fam:
                return nm
    return want


def chat_stream(base, model, messages, on_delta=None, on_think=None,
                temperature=0.7, stop_flag=None, timeout=900):
    """流式对话 —— Ollama 原生 /api/chat（NDJSON）。
    返回 (正文, 思维链)。"""
    try:
        model = resolve_local_model(base, model)
    except Exception:
        pass
    # ⚠️ 必须显式限制上下文长度（2026-10-26 实测，血的教训）：
    #   Ollama 会按可用内存自动挑 context size，GB10 有 121GB 统一内存，它直接挑了
    #   131072（128K）。而 deepseek-r1:70b 的 KV cache 约 0.32MB/token，
    #   128K ≈ 42GB —— 和模型权重本身一样大。结果权重 42.5G + KV 42G ≈ 85G，
    #   把 121GB 内存吃到剩 5GB、swap 打满、推理最终返回空内容。
    #   锁到 16384（约 5GB）后总占用回到 ~50GB，余量充足。
    try:
        num_ctx = int(os.environ.get("DGX_OLLAMA_NUM_CTX") or 16384)
    except Exception:
        num_ctx = 16384
    url = base.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": float(temperature), "num_ctx": num_ctx},
        # v15：70B 冷加载 15.6s，常驻可省掉这段时间（121GB 统一内存够放 70B+30B）
        "keep_alive": os.environ.get("DGX_OLLAMA_KEEP_ALIVE", "30m"),
    }
    # v13：推理模型（deepseek-r1 / qwen3 思考档）默认关思考。
    # 实测 2026-10-29：deepseek-r1:70b 回答一句 80 字的电源问题用了 93 秒，
    # 而且思维链整段吐进正文（"好的，我现在需要解释…"），指令跟随也失效。
    # 关掉后正文才是答案；需要深推理时设 DGX_OLLAMA_THINK=1。
    if (os.environ.get("DGX_OLLAMA_THINK") or "0").strip() not in ("1", "true", "yes"):
        payload["think"] = False
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    parts, thinks = [], []

    def hit(fn, txt):
        if fn and txt:
            try:
                fn(txt)
            except Exception:
                pass

    try:
        resp = _opener().open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        # 老版本 Ollama 不认 think 字段 → 去掉重试一次
        if e.code == 400 and "think" in payload:
            payload.pop("think", None)
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            resp = _opener().open(req, timeout=timeout)
        else:
            raise
    with resp as r:
        for raw in r:
            if stop_flag is not None and stop_flag.is_set():
                break
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("error"):
                raise RuntimeError(obj["error"])
            msg = obj.get("message") or {}
            th, ct = msg.get("thinking"), msg.get("content")
            if th:
                thinks.append(th)
                hit(on_think, th)
            if ct:
                parts.append(ct)
                hit(on_delta, ct)
            if obj.get("done"):
                break
    return "".join(parts), "".join(thinks)


def trim_history(messages):
    """保留 system + 最近 N 条，防上下文无限增长"""
    if len(messages) <= MAX_HISTORY_MSGS + 1:
        return messages
    head = [m for m in messages[:1] if m.get("role") == "system"]
    return head + messages[-(MAX_HISTORY_MSGS):]


def api_messages(messages):
    """清洗成 Ollama 可接受的结构（去掉前端专用字段）"""
    out = []
    for m in messages:
        item = {"role": m.get("role", "user"), "content": m.get("content", "")}
        imgs = m.get("images")
        if imgs:
            item["images"] = imgs
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 本机命令执行
# ---------------------------------------------------------------------------
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\s+[^\s]",
    r"\brm\s+-r\s+[^\s]",
    r"(?:^|[;&|]\s*)format(?:\.com)?\s+[a-zA-Z]:",  # 仅 Windows `format C:`；不能误伤 nvidia-smi --format=csv
    r"\bmkfs\b",
    r"\bdiskpart\b",
    r"\bdel\s+/[sfq].*\*",
    r"\brd\s+/s",
    r":\s*>\s*\S",            # :> file 清空文件
    r"\btruncate\s+-s\s*0\b",
    r"\bshutdown\b",
    r"\breg\s+delete\b",
    r"\bbcdedit\b",
    r"\bvssadmin\s+delete\b",
]

_DANGER_RE = [re.compile(p, re.I) for p in DANGEROUS_PATTERNS]


def is_dangerous(command):
    return any(r.search(command or "") for r in _DANGER_RE)


def run_command(command, cwd=None, timeout=180):
    """执行本机命令；返回 (退出码, 输出)"""
    try:
        env = dict(os.environ)
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ELECTRON_RUN_AS_NODE"):
            env.pop(k, None)
        p = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout, env=env,
                           encoding="utf-8", errors="ignore")
        out = p.stdout or ""
        if p.stderr:
            out += "\n[stderr]\n" + p.stderr
        return p.returncode, out.strip()[:8000]
    except subprocess.TimeoutExpired:
        return -1, "命令执行超时（%ss）" % timeout
    except Exception as e:
        return -1, "执行失败：%s" % e
