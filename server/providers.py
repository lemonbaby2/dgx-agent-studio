# -*- coding: utf-8 -*-
"""多供应商统一 LLM 调用层（v11）

设计目标：把「本地 Ollama」和「云端豆包 / 千问 / 各家 OpenAI 兼容 API」
收成一个函数签名，上层对话、技能、工具回路全都不用知道自己调的是谁。

两家国产云的关键事实（2026-10-24 在 DGX 上实测）：
  - 火山方舟（豆包）: https://ark.cn-beijing.volces.com/api/v3   OpenAI 兼容
      ⚠ 方舟的对话模型通常要用「推理接入点 ID」ep-xxxxxxxx 而不是模型名，
        直接用 doubao-pro-32k 这类名字会报 model not found。
        用户名下可用的接入点在控制台 → 在线推理 里创建。
  - 阿里百炼（千问）: https://dashscope.aliyuncs.com/compatible-mode/v1  OpenAI 兼容
      直接用 qwen-plus / qwen-max / qwen-long / qwen-vl-max 等模型名即可。
  - 两家都支持 /models 拉取当前 Key 名下真实可用的模型清单 → 不硬编码过期列表。

安全约定：
  - 密钥只从 ~/.dgx_studio/providers.json（0600）或进程环境变量读
  - 任何对外返回的结构里都不得出现完整密钥，只允许 masked
"""
import json
import os
import time
import urllib.request

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.expanduser("~/.dgx_studio")
CRED_FILE = os.path.join(DATA_DIR, "providers.json")
ENV_FILE = os.path.join(DATA_DIR, "credentials.env")


def _load_env_file():
    """把 credentials.env 灌进 os.environ（不覆盖已有值）

    为什么需要：providers.json 只有 Studio 主进程读得到，agent 自己 spawn 的
    子进程（skill / 终端命令 / 工具回路 / 自学习）拿不到 key，云端调用全 401。
    加载进环境变量后，这些子进程 fork 时自然继承。
    """
    try:
        if not os.path.isfile(ENV_FILE):
            return
        with open(ENV_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                s = raw.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith("export "):
                    s = s[7:].strip()
                if "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v and not os.environ.get(k):
                    os.environ[k] = v
    except Exception:
        pass


_load_env_file()

# ---------------------------------------------------------------------------
# 供应商注册表
# ---------------------------------------------------------------------------
PROVIDERS = {
    "ollama": {
        "name": "本地 Ollama",
        "vendor": "本机",
        "type": "ollama",
        "base": "http://127.0.0.1:11434",
        "base_env": "OLLAMA_BASE",
        "key_env": None,
        "needs_key": False,
        "hint": "本机的 qwen3-coder / deepseek-r1 / qwen3-vl，不出网不要钱",
        "models": [],          # 动态拉取
    },
    "ark": {
        "name": "火山方舟 · 豆包",
        "vendor": "字节",
        "type": "openai",
        "base": "https://ark.cn-beijing.volces.com/api/v3",
        "base_env": "ARK_BASE_URL",
        "key_env": "ARK_API_KEY",
        "needs_key": True,
        "hint": "豆包大模型全家桶。对话模型请填控制台里的接入点 ID（ep- 开头）",
        "models": [
            "doubao-pro-32k", "doubao-lite-32k", "doubao-pro-256k",
            "doubao-vision-pro-32k", "doubao-seed-1-6", "doubao-seed-1-6-thinking",
            "doubao-embedding-large", "doubao-seedream-3-0-t2i",
        ],
    },
    "qwen": {
        "name": "阿里百炼 · 通义千问",
        "vendor": "阿里",
        "type": "openai",
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "base_env": "DASHSCOPE_BASE_URL",
        "key_env": "DASHSCOPE_API_KEY",
        "needs_key": True,
        "hint": "Qwen 全家桶，直接填模型名即可（qwen-plus / qwen-max / qwen-long …）",
        "models": [
            "qwen-plus", "qwen-max", "qwen-turbo", "qwen-long",
            "qwen2.5-coder-32b-instruct", "qwen-vl-max", "qwq-plus",
            "text-embedding-v3", "qwen-mt-plus",
        ],
    },
    "deepseek": {
        "name": "DeepSeek",
        "vendor": "深度求索",
        "type": "openai",
        "base": "https://api.deepseek.com",
        "base_env": "DEEPSEEK_BASE_URL",
        "key_env": "DEEPSEEK_API_KEY",
        "needs_key": True,
        "hint": "deepseek-chat / deepseek-reasoner",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "moonshot": {
        "name": "月之暗面 Kimi",
        "vendor": "Moonshot",
        "type": "openai",
        "base": "https://api.moonshot.cn/v1",
        "base_env": "MOONSHOT_BASE_URL",
        "key_env": "MOONSHOT_API_KEY",
        "needs_key": True,
        "hint": "长文能力强 moonshot-v1-128k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    },
    "zhipu": {
        "name": "智谱 GLM",
        "vendor": "智谱",
        "type": "openai",
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "base_env": "ZHIPU_BASE_URL",
        "key_env": "ZHIPU_API_KEY",
        "needs_key": True,
        "hint": "glm-4-plus / glm-4v",
        "models": ["glm-4-plus", "glm-4-air", "glm-4-flash", "glm-4v-plus"],
    },
    "openai": {
        "name": "OpenAI",
        "vendor": "OpenAI",
        "type": "openai",
        "base": "https://api.openai.com/v1",
        "base_env": "OPENAI_BASE_URL",
        "key_env": "OPENAI_API_KEY",
        "needs_key": True,
        "hint": "gpt-4o / gpt-4.1 系列",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"],
    },
    "custom": {
        "name": "自定义 OpenAI 兼容",
        "vendor": "自建",
        "type": "openai",
        "base": "http://127.0.0.1:8000/v1",
        "base_env": "CUSTOM_BASE_URL",
        "key_env": "CUSTOM_API_KEY",
        "needs_key": False,
        "hint": "vLLM / LM Studio / 任何 OpenAI 兼容网关",
        "models": [],
    },
}

# 哪些 provider 的 thinking 字段是 reasoning_content（OpenAI 兼容阵营的事实标准）
REASONING_FIELD_PROVIDERS = {"ark", "qwen", "deepseek", "moonshot", "zhipu",
                             "openai", "custom"}


# ---------------------------------------------------------------------------
# 凭据读写
# ---------------------------------------------------------------------------
def _ensure_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass


def load_creds():
    """读 ~/.dgx_studio/providers.json，失败返回空 dict"""
    try:
        if os.path.isfile(CRED_FILE):
            with open(CRED_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def save_creds(d):
    _ensure_dir()
    tmp = CRED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CRED_FILE)
    try:
        os.chmod(CRED_FILE, 0o600)
    except Exception:
        pass
    return True


def mask(k):
    """密钥掩码 —— 任何出网的结构里只能有这个"""
    k = (k or "").strip()
    if not k:
        return ""
    if len(k) <= 10:
        return k[:2] + "*" * (len(k) - 2)
    return "%s...%s (%d 位)" % (k[:6], k[-4:], len(k))


def get_key(pid):
    """优先级：配置文件 > 环境变量"""
    pid = (pid or "").strip()
    if not pid:
        return ""
    d = load_creds()
    v = (d.get("keys") or {}).get(pid) or ""
    if v:
        return v.strip()
    env = PROVIDERS.get(pid, {}).get("key_env")
    return (os.environ.get(env) or "").strip() if env else ""


def get_base(pid):
    pid = (pid or "").strip()
    d = load_creds()
    v = (d.get("bases") or {}).get(pid) or ""
    if v:
        return v.rstrip("/")
    p = PROVIDERS.get(pid, {})
    env = p.get("base_env")
    if env and os.environ.get(env):
        return os.environ[env].rstrip("/")
    return (p.get("base") or "").rstrip("/")


def set_key(pid, key):
    d = load_creds()
    d.setdefault("keys", {})
    if key:
        d["keys"][pid] = key.strip()
    else:
        d["keys"].pop(pid, None)
    return save_creds(d)


def set_base(pid, base):
    d = load_creds()
    d.setdefault("bases", {})
    if base:
        d["bases"][pid] = base.strip().rstrip("/")
    else:
        d["bases"].pop(pid, None)
    return save_creds(d)


def provider_list():
    """给前端的供应商清单（含是否已配密钥、连通性提示、绝不含明文密钥）"""
    creds = load_creds()
    keys = creds.get("keys") or {}
    out = []
    for pid, p in PROVIDERS.items():
        has = bool(keys.get(pid)) or bool(
            p.get("key_env") and os.environ.get(p["key_env"]))
        out.append({
            "id": pid,
            "name": p["name"],
            "vendor": p["vendor"],
            "type": p["type"],
            "base": get_base(pid),
            "needs_key": p["needs_key"],
            "has_key": has,
            "key_mask": mask(keys.get(pid) or ""),
            "hint": p["hint"],
            "models": list(p.get("models") or []),
        })
    return out


def resolve(pid, model=None):
    """把 provider/model 解析成 (type, base, key, model)；model 为空则给个默认"""
    pid = (pid or "ollama").strip()
    if pid not in PROVIDERS:
        # 容错：未知 provider 当自定义 OpenAI 处理
        pid = "ollama" if pid.startswith("ollama") else "custom"
    p = PROVIDERS[pid]
    base = get_base(pid)
    key = get_key(pid)
    m = (model or "").strip()
    if not m:
        m = (p.get("models") or [""])[0] if p.get("models") else ""
    return p["type"], base, key, m


# ---------------------------------------------------------------------------
# HTTP 工具（一律绕过系统代理 —— 本机 Clash 会把请求打成 000）
# ---------------------------------------------------------------------------
def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url, headers=None, data=None, timeout=20, method=None):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


# ---------------------------------------------------------------------------
# 模型清单
# ---------------------------------------------------------------------------
def list_ollama_models(base=None, timeout=12):
    base = base or get_base("ollama")
    try:
        d = http_json(base.rstrip("/") + "/api/tags", timeout=timeout)
        return [m.get("name") for m in (d.get("models") or []) if m.get("name")]
    except Exception:
        return []


def list_cloud_models(pid, timeout=25):
    """用已配 Key 拉该供应商名下真实可用模型；失败返回 (False, 原因)"""
    p = PROVIDERS.get(pid)
    if not p:
        return False, "未知供应商"
    if p["type"] != "openai":
        return True, list_ollama_models(get_base(pid), timeout)
    key = get_key(pid)
    if not key and p.get("needs_key"):
        return False, "未配置密钥"
    try:
        d = http_json(get_base(pid) + "/models",
                      headers={"Authorization": "Bearer %s" % key},
                      timeout=timeout)
        names = sorted({(x.get("id") or "") for x in (d.get("data") or [])
                        if x.get("id")})
        return True, names
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        if e.code in (401, 403):
            return False, "密钥无效或无权限（HTTP %d）%s" % (e.code, body)
        return False, "HTTP %d %s" % (e.code, body)
    except Exception as e:
        return False, str(e)[:200]


# ---------------------------------------------------------------------------
# 统一对话
# ---------------------------------------------------------------------------
def _cleanmsgs(messages):
    """清洗成标准 OpenAI/Ollama 都能吃的结构"""
    out = []
    for m in messages or []:
        role = m.get("role") or "user"
        if role not in ("system", "user", "assistant", "tool"):
            role = "user"
        item = {"role": role}
        c = m.get("content")
        if isinstance(c, list):
            item["content"] = c            # 多模态 content parts 原样透传
        else:
            item["content"] = c if c is not None else ""
        # 工具协议字段
        for k in ("tool_calls", "tool_call_id", "name"):
            if m.get(k):
                item[k] = m[k]
        # Ollama 的 images 转成 OpenAI 的 image_url
        imgs = m.get("images")
        if imgs and role == "user":
            parts = [{"type": "text", "text": item.get("content") or ""}]
            for im in imgs[:6]:
                parts.append({"type": "image_url",
                              "image_url": {"url": "data:image/jpeg;base64,%s" % im}})
            item["content"] = parts
        out.append(item)
    return out


def _cleanmsgs_ollama(messages):
    """Translate OpenAI-style tool history to Ollama's native message shape."""
    out = _cleanmsgs(messages)
    for item in out:
        if item.get("role") == "assistant" and item.get("tool_calls"):
            calls = []
            for tc in item["tool_calls"]:
                fn = (tc or {}).get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                calls.append({"function": {"name": fn.get("name") or "",
                                             "arguments": args}})
            item["tool_calls"] = calls
        if item.get("role") == "tool":
            tool_name = item.pop("name", "")
            item.pop("tool_call_id", None)
            if tool_name:
                item["tool_name"] = tool_name
    return out


def chat_stream_openai(base, key, model, messages, tools=None,
                       on_delta=None, on_think=None, temperature=0.7,
                       stop_flag=None, timeout=300, max_tokens=None):
    """OpenAI 兼容流式（豆包 / 千问 / DeepSeek / Kimi / GLM / OpenAI 通用）

    返回 (content, thinking, tool_calls)
    """
    body = {
        "model": model,
        "messages": _cleanmsgs(messages),
        "stream": True,
        "temperature": float(temperature),
    }
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdr = {"Content-Type": "application/json"}
    if key:
        hdr["Authorization"] = "Bearer %s" % key
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=data, headers=hdr)

    parts, thinks = [], []
    tool_map = {}     # index -> {id, name, arguments}

    def hit(fn, txt):
        if fn and txt:
            try:
                fn(txt)
            except Exception:
                pass

    with _opener().open(req, timeout=timeout) as r:
        for raw in r:
            if stop_flag is not None and stop_flag.is_set():
                break
            line = raw.decode("utf-8", "ignore").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if obj.get("error"):
                msg = obj["error"]
                if isinstance(msg, dict):
                    msg = msg.get("message") or json.dumps(msg, ensure_ascii=False)
                raise RuntimeError(str(msg)[:300])
            ch = (obj.get("choices") or [{}])[0]
            d = ch.get("delta") or {}
            # 思维链：各家都叫 reasoning_content，少数用 reasoning
            th = d.get("reasoning_content") or d.get("reasoning")
            if th:
                thinks.append(th)
                hit(on_think, th)
            ct = d.get("content")
            if ct:
                parts.append(ct)
                hit(on_delta, ct)
            # 工具调用增量拼接
            for tc in (d.get("tool_calls") or []):
                idx = tc.get("index", 0)
                slot = tool_map.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if ch.get("finish_reason") in ("stop", "tool_calls", "length"):
                break

    tcs = [{"id": v["id"] or ("call_%d" % i),
            "type": "function",
            "function": {"name": v["name"],
                         "arguments": v["arguments"] or "{}"}}
           for i, v in sorted(tool_map.items()) if v["name"]]
    return "".join(parts), "".join(thinks), tcs


def chat_stream_ollama(base, model, messages, tools=None, on_delta=None, on_think=None,
                       temperature=0.7, stop_flag=None, timeout=900):
    """本地 Ollama 对话；有 tools 时使用原生工具协议。"""
    body = json.dumps({
        "model": model,
        "messages": _cleanmsgs_ollama(messages),
        **({"tools": tools} if tools else {}),
        "stream": False if tools else True,
        "options": {"temperature": float(temperature)},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    parts, thinks, tool_calls = [], [], []

    def hit(fn, txt):
        if fn and txt:
            try:
                fn(txt)
            except Exception:
                pass

    with _opener().open(req, timeout=timeout) as r:
        if tools:
            obj = json.loads(r.read().decode("utf-8", "ignore"))
            if obj.get("error"):
                raise RuntimeError(str(obj["error"])[:300])
            msg = obj.get("message") or {}
            th, ct = msg.get("thinking"), msg.get("content")
            if th:
                thinks.append(th)
                hit(on_think, th)
            if ct:
                parts.append(ct)
                hit(on_delta, ct)
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name"):
                    args = fn.get("arguments") or {}
                    tool_calls.append({"id": tc.get("id") or "ollama_tool_%d" % len(tool_calls),
                                       "type": "function",
                                       "function": {"name": fn["name"],
                                                    "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}})
            return "".join(parts), "".join(thinks), tool_calls
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
                raise RuntimeError(str(obj["error"])[:300])
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
    return "".join(parts), "".join(thinks), tool_calls


def chat_stream(pid, model, messages, tools=None, on_delta=None, on_think=None,
                temperature=0.7, stop_flag=None, timeout=300, max_tokens=None):
    """统一入口：上层只认这一个函数

    pid: provider id（ollama / ark / qwen / deepseek / ...）
    返回 (content, thinking, tool_calls)
    """
    ptype, base, key, m = resolve(pid, model)
    if not base:
        raise RuntimeError("供应商 %s 未配置服务地址" % pid)
    if not m:
        raise RuntimeError("未指定模型")
    if ptype == "openai":
        return chat_stream_openai(base, key, m, messages, tools=tools,
                                  on_delta=on_delta, on_think=on_think,
                                  temperature=temperature, stop_flag=stop_flag,
                                  timeout=timeout, max_tokens=max_tokens)
    return chat_stream_ollama(base, m, messages, tools=tools, on_delta=on_delta,
                              on_think=on_think, temperature=temperature,
                              stop_flag=stop_flag, timeout=timeout)


def chat_once(pid, model, messages, temperature=0.7, timeout=300,
              max_tokens=None, stop_flag=None):
    """非流式一次性取回（给自学习、工具回路内部用）"""
    c, t, tc = chat_stream(pid, model, messages, temperature=temperature,
                           timeout=timeout, max_tokens=max_tokens,
                           stop_flag=stop_flag)
    return c, t, tc


# ---------------------------------------------------------------------------
# 连通性自检
# ---------------------------------------------------------------------------
def test_provider(pid, model=None, timeout=45):
    """真实发一个最小请求验证连通 + 鉴权，返回详细结果"""
    pid = (pid or "").strip()
    if pid not in PROVIDERS:
        return {"ok": False, "error": "未知供应商 %s" % pid}
    ptype, base, key, m = resolve(pid, model)
    if not base:
        return {"ok": False, "error": "未配置服务地址"}
    if not m:
        return {"ok": False, "error": "未指定模型，请先拉模型列表或手填"}
    if ptype == "openai" and PROVIDERS[pid].get("needs_key") and not key:
        return {"ok": False, "error": "未配置密钥"}
    t0 = time.time()
    try:
        txt, think, _ = chat_once(
            pid, m,
            [{"role": "user", "content": "只回复两个字：可用"}],
            temperature=0.1, timeout=timeout, max_tokens=64)
        return {"ok": True, "provider": pid, "model": m,
                "reply": (txt or "")[:120],
                "thinking": bool(think),
                "elapsed": round(time.time() - t0, 2)}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        tip = ""
        if e.code == 401:
            tip = "密钥无效或没权限。火山方舟的对话模型要填 ep- 开头的接入点 ID"
        elif e.code == 404:
            tip = "模型名不存在。方舟请用接入点 ID；千请用真实模型名"
        elif e.code == 429:
            tip = "触发限流或余额不足"
        return {"ok": False, "error": "HTTP %d %s" % (e.code, body),
                "tip": tip, "elapsed": round(time.time() - t0, 2)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300],
                "elapsed": round(time.time() - t0, 2)}
