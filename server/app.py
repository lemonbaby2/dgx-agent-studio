# -*- coding: utf-8 -*-
"""
DGX Agent Studio · HTTP 服务 + 桌面窗口
------------------------------------------------
· 后端：Python 标准库 ThreadingHTTPServer，零框架依赖
· 前端：web/ 目录静态文件（打包后随 exe 一起分发）
· 窗口：优先 pywebview（WebView2），失败则退回系统默认浏览器
· 对话：SSE 流式推送 Ollama 输出
"""
from __future__ import annotations

import base64
import hmac
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from . import core                                     # noqa: E402
from . import fs_api                                   # noqa: E402

# v6 新增：视频理解 / 语音 / 技能引擎
try:
    from . import video_api                            # noqa: E402
except Exception as _e:                                # 模块缺失不应拖垮启动
    video_api = None
    print("[v6] video_api 未加载：%s" % _e)
try:
    from . import voice_api                            # noqa: E402
except Exception as _e:
    voice_api = None
    print("[v6] voice_api 未加载：%s" % _e)
try:
    from . import skills                               # noqa: E402
except Exception as _e:
    skills = None
    print("[v6] skills 未加载：%s" % _e)
# v7 新增：语音交互（ASR）与论文 PDF 解析
try:
    from . import speech_api                           # noqa: E402
except Exception as _e:
    speech_api = None
    print("[v7] speech_api 未加载：%s" % _e)

# v12 新增：多用户账号与会话隔离
try:
    from . import users as users_store                 # noqa: E402
except Exception as _e:
    users_store = None
    print("[v12] users 未加载：%s" % _e)

# v8 新增：具身仿真桥 / 笔记仓库 / 论文朗读
try:
    from . import embodied_api                          # noqa: E402
except Exception as _e:
    embodied_api = None
    print("[v8] embodied_api 未加载：%s" % _e)
try:
    from . import git_api                               # noqa: E402
except Exception as _e:
    git_api = None
    print("[v8] git_api 未加载：%s" % _e)
try:
    from . import paper_reader                          # noqa: E402
except Exception as _e:
    paper_reader = None
    print("[v8] paper_reader 未加载：%s" % _e)

# v9 新增：自学习（聊天记录 → 知识库 + 自动技能）
try:
    from . import learn_api                             # noqa: E402
except Exception as _e:
    learn_api = None
    print("[v9] learn_api 未加载：%s" % _e)

# v10 新增：实时学习总线（每轮对话结束即入队，攒批消化）
try:
    from . import learn_bus                             # noqa: E402
except Exception as _e:
    learn_bus = None
    print("[v10] learn_bus 未加载：%s" % _e)

# v11 新增：多供应商（豆包/千问/各家）+ Agent 工具层 + 访问鉴权
try:
    from . import providers as providers_mod           # noqa: E402
except Exception as _e:
    providers_mod = None
    print("[v11] providers 未加载：%s" % _e)

try:
    from . import provider_api                         # noqa: E402
except Exception as _e:
    provider_api = None
    print("[v11] provider_api 未加载：%s" % _e)

try:
    from . import agent_tools                          # noqa: E402
except Exception as _e:
    agent_tools = None
    print("[v11] agent_tools 未加载：%s" % _e)

try:
    from . import authz                                # noqa: E402
except Exception as _e:
    authz = None
    print("[v11] authz 未加载：%s" % _e)

# v12 新增：多用户与会话隔离
try:
    from . import users as users_mod                   # noqa: E402
except Exception as _e:
    users_mod = None
    print("[v12] users 未加载：%s" % _e)

# v13 新增：能力阶梯（模型 / 上下文预算随技能库增长而升级）
try:
    from . import capacity                             # noqa: E402
except Exception as _e:
    capacity = None
    print("[v13] capacity 未加载：%s" % _e)

APP_NAME = "DGX Embodied Studio"
APP_VER = "15.0"


# ---------------- v15：OpenAI 兼容层（给 WorkBuddy / 任意客户端直连） ----
APIKEY_FILE = os.path.join(os.path.expanduser("~"), ".dgx_studio", "apikey.txt")
V1_DEFAULT_MODEL = "deepseek-r1:70b"


def api_key():
    """固定本地密钥：首次启动生成后落盘，之后只读。"""
    try:
        with io.open(APIKEY_FILE, encoding="utf-8") as f:
            k = f.read().strip()
        if k:
            return k
    except Exception:
        pass
    k = "dgx-" + base64.urlsafe_b64encode(os.urandom(18)).decode().strip("=")
    try:
        os.makedirs(os.path.dirname(APIKEY_FILE), exist_ok=True)
        with io.open(APIKEY_FILE, "w", encoding="utf-8") as f:
            f.write(k)
        os.chmod(APIKEY_FILE, 0o600)
    except Exception:
        pass
    return k

# v8：默认监听 0.0.0.0，本机/局域网/其他工作站都能直接访问。
# 想只给本机用就设环境变量 DGX_STUDIO_HOST=127.0.0.1
HOST = os.environ.get("DGX_STUDIO_HOST") or "0.0.0.0"
PORT = 18742              # 主端口；被占用则向后试探
PORT_SCAN = 20

# 应用根目录（server/ 的上一级），技能目录 <root>/skops 在这里找。
# 打包成 exe 时被解到临时目录，故回溯两级；
# 源码运行时 SDK ROOT = E:/DGX-Agent-Studio
def _find_app_root():
    here = os.path.dirname(os.path.abspath(__file__))     # .../server
    up = os.path.dirname(here)                            # .../
    if os.path.isdir(os.path.join(up, "skops")):
        return up
    # PyInstaller: 资源在 sys._MEIPASS 旁
    try:
        base = getattr(sys, "_MEIPASS", None)
        if base:
            for cand in (base, os.path.dirname(base)):
                if os.path.isdir(os.path.join(cand, "skops")):
                    return cand
    except Exception:
        pass
    return up


APP_DIR = _find_app_root()

# ---------------------------------------------------------------------------
# 运行时状态
# ---------------------------------------------------------------------------
_state_lock = threading.RLock()
_state_cache: dict = {}       # uid -> (sessions, current)
_cur_uid = threading.local()  # 每个请求线程自己的身份
_stoppers: dict = {}          # sid -> StreamStopper
_runtime: dict = {}           # v14: sid -> 生成中的实时状态（供多会话并发取回）
_runtime_lock = threading.Lock()
MAX_CONCURRENT_CHAT = int(os.environ.get("DGS_MAX_CONCURRENT", "3"))
_chat_sem = threading.BoundedSemaphore(MAX_CONCURRENT_CHAT)
_web_root = ""                # 前端静态目录
DEFAULT_UID = "_default"      # 没有账号体系时的公共池（= v11 行为）


def cur_uid():
    """当前请求属于谁。后台线程/非请求上下文返回 DEFAULT_UID"""
    return getattr(_cur_uid, "uid", "") or DEFAULT_UID


def set_cur_uid(uid):
    _cur_uid.uid = uid or ""


def _sess_path(uid):
    if uid in ("", None, DEFAULT_UID):
        return core.SESSIONS_PATH
    return users_store.sessions_path(uid)


def _read_user_sessions(uid):
    raw = core._read_json(_sess_path(uid), {}) or {}
    ss = [core.Session.from_dict(x) for x in (raw.get("sessions") or [])]
    cur = raw.get("current") or None
    if not ss:
        s = core.Session()
        ss, cur = [s], s.id
    if not any(s.id == cur for s in ss):
        cur = ss[0].id
    return ss, cur


def get_state():
    uid = cur_uid()
    with _state_lock:
        cached = _state_cache.get(uid)
    if cached:
        return list(cached[0]), cached[1]
    ss, cur = _read_user_sessions(uid)
    with _state_lock:
        _state_cache[uid] = (ss, cur)
    return list(ss), cur


def set_state(sessions, current):
    uid = cur_uid()
    with _state_lock:
        _state_cache[uid] = (sessions, current)


def find_session(sid):
    for s in get_state()[0]:
        if s.id == sid:
            return s
    return None


def persist():
    uid = cur_uid()
    ss, cur = get_state()
    with _state_lock:
        _state_cache[uid] = (ss, cur)
    path = _sess_path(uid)
    try:
        core._write_json(path, {
            "version": 3,
            "current": cur,
            "sessions": [s.to_dict() for s in ss],
        })
    except Exception as e:
        print("[v12] 会话落盘失败 uid=%s: %s" % (uid, e))


def drop_user_cache(uid):
    """用户被删除/切号时清掉内存缓存，防止串号"""
    with _state_lock:
        _state_cache.pop(uid, None)


def get_stopper(sid):
    with _state_lock:
        if sid not in _stoppers:
            _stoppers[sid] = core.StreamStopper()
        return _stoppers[sid]


# ---- v14：多会话并发运行态 -------------------------------------------------
def rt_begin(sid, uid, model):
    """标记某会话进入生成态。同一会话重复进入返回 False（防重复提交）"""
    with _runtime_lock:
        if sid in _runtime and not _runtime[sid].get("done"):
            return False
        _runtime[sid] = {
            "sid": sid, "uid": uid, "model": model,
            "text": "", "think": "", "tools": [],
            "started": time.time(), "updated": time.time(),
            "done": False, "queued": False, "queue_ok_at": 0.0,
        }
        return True


def rt_update(sid, **kw):
    with _runtime_lock:
        r = _runtime.get(sid)
        if r:
            r.update(kw)
            r["updated"] = time.time()


def rt_append(sid, field, txt):
    with _runtime_lock:
        r = _runtime.get(sid)
        if r:
            r[field] = (r.get(field) or "") + (txt or "")
            r["updated"] = time.time()


def rt_end(sid):
    """生成结束：保留 40 秒供前端补取完整文本，然后清掉"""
    def _drop():
        time.sleep(40)
        with _runtime_lock:
            r = _runtime.get(sid)
            if r and r.get("done"):
                _runtime.pop(sid, None)
    with _runtime_lock:
        r = _runtime.get(sid)
        if r:
            r["done"] = True
            r["updated"] = time.time()
    threading.Thread(target=_drop, daemon=True).start()


def rt_live(sid):
    with _runtime_lock:
        r = _runtime.get(sid)
        if not r:
            return None
        d = dict(r)
        d["age"] = round(time.time() - d.get("started", time.time()), 1)
        return d


def rt_running():
    with _runtime_lock:
        return {k: {"done": v.get("done"), "text_len": len(v.get("text") or "")}
                for k, v in _runtime.items()}


def bootstrap_state():
    """启动时恢复会话"""
    ss, cur = core.load_sessions()
    if not ss:
        s = core.Session()
        ss = [s]
        cur = s.id
    if not any(s.id == cur for s in ss):
        cur = ss[0].id
    set_state(ss, cur)
    persist()


def start_background():
    """启动后台线程（幂等）。**任何异常都不能阻止服务起来。**"""
    if learn_bus:
        try:
            r = learn_bus.start()
            print("[v10] 实时学习线程：%s（待消化 %s 条）"
                  % ("已在运行" if r.get("already") else "已启动",
                     r.get("pending", 0)))
        except Exception as e:
            print("[v10] 实时学习启动失败（不影响主服务）：%s" % e)


# ---------------------------------------------------------------------------
# v10：自动部署状态读取（只读，不触发部署 —— 部署只能由 timer 或运维手动跑）
# ---------------------------------------------------------------------------
def _deploy_status():
    data = os.path.join(core.DATA_DIR)
    st_path = os.path.join(data, "deploy_state.json")
    inbox = os.path.join(data, "deploy_inbox")
    staged = os.path.join(inbox, "staged")
    ready = os.path.isfile(os.path.join(inbox, "READY"))
    out = {"ok": True, "last": {}, "history": [], "has_staged": False,
           "units": {}}
    try:
        with open(st_path, encoding="utf-8") as f:
            obj = json.load(f)
        out["last"] = obj.get("last") or {}
        out["history"] = (obj.get("history") or [])[-8:]
    except Exception:
        pass
    try:
        n = 0
        if os.path.isdir(staged):
            for _r, _d, fs in os.walk(staged):
                n += len(fs)
        out["has_staged"] = bool(ready and n)
        out["staged_files"] = n
    except Exception:
        pass
    # 下次定时时间（每天 00:05）
    try:
        lt = time.localtime()
        nxt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 5, 0, 0, 0, -1))
        if nxt <= time.time():
            nxt += 86400
        out["next_run"] = int(nxt)
    except Exception:
        out["next_run"] = 0
    try:
        alert = os.path.join(data, "ALERT.txt")
        if os.path.isfile(alert):
            with open(alert, encoding="utf-8") as f:
                out["alert"] = f.read()[:800]
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# 前端静态资源
# ---------------------------------------------------------------------------
def find_web_root():
    global _web_root
    if _web_root and os.path.isdir(_web_root):
        return _web_root
    cands = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        cands += [os.path.join(base, "web"),
                  os.path.join(base, "..", "web"),
                  os.path.join(os.path.dirname(sys.executable), "web")]
    cands += [os.path.join(_HERE, "..", "web"),
              os.path.join(os.path.dirname(_HERE), "web"),
              os.path.join(os.getcwd(), "web")]
    for c in cands:
        if os.path.isfile(os.path.join(c, "index.html")):
            _web_root = os.path.abspath(c)
            return _web_root
    return ""


MIME = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".ico": "image/x-icon", ".woff2": "font/woff2",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
}


# ---------------------------------------------------------------------------
# HTTP 处理器
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "DGXStudio/%s" % APP_VER
    protocol_version = "HTTP/1.1"

    # ---------------- 工具 ----------------
    def log_message(self, fmt, *args):
        pass

    # ---------- v10：多人身份 ----------
    def _client(self):
        """从请求头取客户端身份。

        优先级：
          1. 前端 localStorage 的 X-Client-Id（同事改名后的稳定身份）
          2. Tailscale Funnel 透传的已认证用户（公网访问时自动识别，免改名）
        没有登录系统 —— 这是**弱隔离**。目的是每人只看到自己的会话、
        不互相串；学习池是共享的，谁聊的都进同一套知识。
        """
        try:
            cid = (self.headers.get("X-Client-Id") or "").strip()[:40]
            # HTTP 头只能是 latin-1，中文必须前端 encodeURIComponent 过来
            raw_nm = self.headers.get("X-Client-Name") or ""
            try:
                from urllib.parse import unquote as _uq
                nm = _uq(raw_nm).strip()[:24]
            except Exception:
                nm = raw_nm.strip()[:24]
        except Exception:
            cid, nm = "", ""
        if not re.match(r"^[A-Za-z0-9_\-]{4,40}$", cid or ""):
            cid = ""
        # v11：Tailscale 透传身份兜底 —— 公网访问也能自动认出是谁
        if not cid and authz:
            try:
                info = self._info()
                login = (info.get("ts_login") or "").strip()
                if login:
                    cid = "ts_" + re.sub(r"[^A-Za-z0-9_\-]", "_", login)[:32]
                    if not nm:
                        nm = (info.get("ts_name") or login.split("@")[0])[:24]
            except Exception:
                pass
        return cid, nm

    def _visible(self, ss, cid):
        """当前客户端能看到的会话：自己的 + 公共的（owner 为空=历史/公共）"""
        if not cid:
            return list(ss)          # 没身份的（curl/脚本）看全部，便于运维排查
        return [s for s in ss if (not s.owner) or s.owner == cid]

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _send_error(self, msg, code=400):
        self._send_json({"ok": False, "error": msg}, code)

    def _send_workspace_file(self, path, download=False):
        """把授权工作区文件以内嵌预览或下载方式交付给浏览器。"""
        rp = fs_api.resolve_in_workspace(path)
        if not rp or not os.path.isfile(rp):
            return self._send_error("文件不存在或不在已授权工作区内", 404)
        try:
            size = os.path.getsize(rp)
            ext = os.path.splitext(rp)[1].lower()
            if ext == ".pdf":
                with open(rp, "rb") as check:
                    if check.read(5) != b"%PDF-":
                        return self._send_error("该文件不是有效 PDF，可能是下载失败后的网页错误页", 422)
            name = os.path.basename(rp)
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(size))
            disposition = "attachment" if download else "inline"
            self.send_header("Content-Disposition",
                             "%s; filename*=UTF-8''%s" % (disposition, quote(name)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with open(rp, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._send_error("文件交付失败：%s" % e, 500)
            except Exception:
                pass

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(min(n, 32 * 1024 * 1024))
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send_sse(self, obj):
        """SSE 单帧"""
        data = json.dumps(obj, ensure_ascii=False)
        try:
            self.wfile.write(("data: %s\n\n" % data).encode("utf-8"))
            self.wfile.flush()
            return True
        except Exception:
            return False

    # ---------------- v11：访问鉴权 ----------------
    def _info(self):
        """一次性嗅探请求来源身份（IP / 是否过反代 / tailscale 用户）"""
        if authz and hasattr(authz, "sniff"):
            return authz.sniff(self.headers, self.client_address[0])
        return {"ip": self.client_address[0], "direct_ip": self.client_address[0],
                "proxied": False, "ts_login": "", "ts_name": "",
                "ts_authenticated": False}

    def _peer_ip(self):
        """真实来源 IP。Tailscale Funnel / 反代会把原 IP 放进 X-Forwarded-For"""
        return self._info().get("ip") or self.client_address[0]

    def _identify(self):
        """把 cookie 解析成 uid，写进本线程上下文。

        返回 uid（"" = 匿名/旧式口令登录）。每个请求线程独立，
        所以并发请求之间不会串号。
        """
        uid = ""
        if authz:
            cookies = authz.parse_cookies(self.headers.get("Cookie") or "")
            uid = authz.verify_user_token(cookies.get(authz.COOKIE, "")) or ""
            if uid == "_":          # 旧式口令登录：没有身份
                uid = ""
        set_cur_uid(uid)
        return uid

    def _has_users(self):
        try:
            return bool(users_store and users_store.has_users())
        except Exception:
            return False

    def _gate(self, path):
        """返回 True = 已拦住并发出响应，调用方应直接 return"""
        # v15：/v1/* 走 Bearer 密钥，不受网页登录门禁影响
        if path.startswith("/v1/"):
            if self._bearer_ok():
                return False
            self._v1_401()
            return True
        if not authz:
            return False
        has_users = self._has_users()
        if not has_users and not authz.enabled():
            return False
        cookies = authz.parse_cookies(self.headers.get("Cookie") or "")
        if not authz.need_auth(path, self._info(), cookies, has_users=has_users):
            return False
        # SSE / XHR 请求给 JSON，页面请求给登录页
        if path.startswith("/api/"):
            _b = json.dumps({"ok": False, "need_login": True, "error": "需要登录"},
                            ensure_ascii=False).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(_b)))
            self.end_headers()
            try:
                self.wfile.write(_b)
            except Exception:
                pass
        else:
            _b = authz.LOGIN_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_b)))
            self.end_headers()
            try:
                self.wfile.write(_b)
            except Exception:
                pass
        return True

    def _secrets_gate(self):
        """密钥读写接口的额外担保：公网匿名一律拒绝"""
        if not authz:
            return False
        cookies = authz.parse_cookies(self.headers.get("Cookie") or "")
        if not authz.needs_login_for_secrets(self._info(), cookies):
            return False
        return self._send_error(
            "密钥接口不允许匿名公网访问。请登录后操作，或从内网访问。", 403)

    # ---------------- 路由入口 ----------------
    def do_GET(self):
        try:
            self._identify()
            p = urlparse(self.path)
            path = p.path
            q = parse_qs(p.query)
            if self._gate(path):
                return
            if path.startswith("/v1/"):
                if path.rstrip("/") in ("/v1/models",):
                    return self._v1_models()
                return self._send_error("Not Found", 404)
            if path.startswith("/api/"):
                return self._api_get(path, q)
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self._send_error("服务器内部错误：%s" % e, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            self._identify()
            p = urlparse(self.path)
            if self._gate(p.path):
                return
            if p.path.startswith("/v1/"):
                if p.path.rstrip("/").endswith("/chat/completions"):
                    return self._v1_chat(self._read_body())
                return self._send_error("Not Found", 404)
            if p.path.startswith("/api/"):
                return self._api_post(p.path, self._read_body())
            self._send_error("Not Found", 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self._send_error("服务器内部错误：%s" % e, 500)
            except Exception:
                pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ---------------- v15：OpenAI 兼容 ----------------
    def _bearer_ok(self):
        want = api_key()
        got = ""
        ah = self.headers.get("Authorization") or ""
        if ah.lower().startswith("bearer "):
            got = ah[7:].strip()
        if not got:
            got = (self.headers.get("X-Api-Key") or "").strip()
        if not got:
            try:
                got = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
            except Exception:
                got = ""
        # 本机 / 局域网直连也可匿名（WorkBuddy 在别的机器上时用 key）
        return bool(got) and hmac.compare_digest(got, want)

    def _v1_401(self):
        body = json.dumps({"error": {"message": "invalid api key", "type": "auth"}},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _v1_models(self):
        base = (core.load_config() or {}).get("base") or core.DEFAULT_BASE
        rows = []
        try:
            body = io.open(APIKEY_FILE, encoding="utf-8").read()  # 触发一次磁盘校验
        except Exception:
            body = ""
        try:
            req = urllib.request.Request(base.rstrip("/") + "/api/tags")
            with core._opener().open(req, timeout=10) as r:
                tags = json.loads(r.read().decode("utf-8"))
            for m in tags.get("models") or []:
                rows.append({"id": m.get("name"), "object": "model",
                             "created": int(time.time()),
                             "owned_by": "dgx-local",
                             "size": m.get("size")})
        except Exception as e:
            rows = [{"id": V1_DEFAULT_MODEL, "object": "model",
                     "created": int(time.time()), "owned_by": "dgx-local",
                     "note": "tags 读取失败：%s" % e}]
        return self._send_json({"object": "list", "data": rows})

    def _v1_chat(self, body):
        model = (body.get("model") or "").strip() or V1_DEFAULT_MODEL
        msgs = body.get("messages") or []
        if not isinstance(msgs, list) or not msgs:
            return self._send_json({"error": {"message": "messages 不能为空"}}, 400)
        clean = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = (m.get("role") or "user").strip()
            c = m.get("content")
            if isinstance(c, list):
                c = "".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
            if role in ("system", "user", "assistant") and c:
                clean.append({"role": role, "content": str(c)})
        if not clean:
            return self._send_json({"error": {"message": "messages 无有效内容"}}, 400)
        cfg = core.load_config() or {}
        base = body.get("base") or cfg.get("base") or core.DEFAULT_BASE
        try:
            temp = float(body.get("temperature")) if body.get("temperature") is not None \
                else float(cfg.get("temperature") or 0.6)
        except Exception:
            temp = 0.6
        cid = "chatcmpl-" + base64.urlsafe_b64encode(os.urandom(9)).decode().strip("=")
        t0 = time.time()

        if not body.get("stream"):
            try:
                text, think = core.chat_stream(base, model, clean, temperature=temp)
            except Exception as e:
                return self._send_json(
                    {"error": {"message": "上游失败：%s" % e, "type": "upstream"}}, 502)
            msg = {"role": "assistant", "content": text or ""}
            if think:
                msg["reasoning_content"] = think
            return self._send_json({
                "id": cid, "object": "chat.completion", "created": int(t0),
                "model": model,
                "choices": [{"index": 0, "finish_reason": "stop", "message": msg}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def chunk(delta, finish=None):
            d = {"id": cid, "object": "chat.completion.chunk", "created": int(t0),
                 "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(("data: %s\n\n" % json.dumps(d, ensure_ascii=False)).encode("utf-8"))
            self.wfile.flush()

        try:
            chunk({"role": "assistant", "content": ""})
            core.chat_stream(base, model, clean, temperature=temp,
                             on_delta=lambda t: chunk({"content": t}))
            chunk({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                chunk({"content": "\n[错误] %s" % e})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass

    # ---------------- 静态 ----------------
    def _static(self, path):
        root = find_web_root()
        if not root:
            self._send_error("前端资源未找到（web/index.html 缺失）", 500)
            return
        rel = path.lstrip("/")
        if rel in ("", "index.html"):
            rel = "index.html"
        # 防目录穿越
        rel = rel.replace("\\", "/")
        if ".." in rel.split("/"):
            self._send_error("非法路径", 403)
            return
        full = os.path.normpath(os.path.join(root, rel))
        if not full.startswith(root):
            self._send_error("非法路径", 403)
            return
        if not os.path.isfile(full):
            # SPA 兜底
            full = os.path.join(root, "index.html")
            if not os.path.isfile(full):
                self._send_error("Not Found", 404)
                return
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, "rb") as f:
                body = f.read()
        except Exception:
            self._send_error("读取失败", 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    # ================= GET API =================
    def _api_get(self, path, q):
        if path == "/api/health":
            cfg = core.load_config()
            base = (q.get("base") or [cfg.get("base")])[0]
            info = {"ok": True, "app": APP_NAME, "ver": APP_VER,
                    "host": HOST,
                    "time": int(time.time() * 1000)}
            try:
                info["ollama"] = core.health(base)
                info["base"] = base
            except Exception as e:
                info["ollama_error"] = str(e)
            self._send_json(info)
            return

        # v13：能力阶梯 —— 当前档位 / 上下文预算 / 升级判据 / 升级历史
        if path == "/api/capacity":
            try:
                n = len(skills.load_skills(APP_DIR)) if skills else 0
                snap = capacity.snapshot(n) if capacity else {"error": "capacity 未加载"}
                if capacity:
                    ok, why = capacity.evaluate(n)
                    snap["can_promote"] = ok
                    snap["reason"] = why
                self._send_json({"ok": True, "capacity": snap})
            except Exception as e:
                self._send_error("能力阶梯读取失败：%s" % e, 500)
            return

        # v8：能力总表 —— 前端据此点亮/隐藏各个面板
        if path == "/api/capabilities":
            caps = {
                "chat": True, "multi_window": True, "fs": True,
                "skills": bool(skills), "video": bool(video_api),
                "voice": bool(voice_api),
                "pdf": bool(speech_api),
                "embodied": bool(embodied_api),
                "git": bool(git_api),
                "tts": bool(paper_reader),
                "learn": bool(learn_api),
                "cloud": bool(providers_mod and provider_api),
                "agent_tools": bool(agent_tools),
                "auth": bool(authz and authz.enabled()),
            }
            detail = {}
            try:
                if embodied_api:
                    st = embodied_api.status()
                    detail["ros2"] = st["ros2"]["ready"]
                    detail["isaac"] = st["isaac"]["running"]
                    detail["isaac_level"] = (st.get("isaac_pip") or {}).get("level", 0)
                    detail["gpu"] = st["gpu"].get("name", "")
                if paper_reader:
                    detail["tts_ready"] = paper_reader.status()["tts_ready"]
                if git_api:
                    gs = git_api.status()
                    detail["git_repo"] = gs["is_repo"]
                    detail["git_token"] = gs["has_token"]
                    detail["notes_dir"] = gs["notes_dir"]
                if authz:
                    detail["auth_enabled"] = authz.enabled()
                    detail["auth_logged_in"] = authz.verify_token(
                        authz.parse_cookies(
                            self.headers.get("Cookie") or "").get(authz.COOKIE, ""))
                if providers_mod and provider_api:
                    pl = provider_api.list_providers().get("providers") or []
                    # 只暴露「配没配」，不暴露任何密钥内容
                    detail["providers"] = {p["id"]: p["has_key"] for p in pl}
                    detail["providers_ready"] = [p["id"] for p in pl
                                                 if p["has_key"] or
                                                 not p["needs_key"]]
                if agent_tools:
                    detail["tools"] = len(agent_tools.tool_names())
                if learn_api:
                    ls = learn_api.status()
                    detail["learned_messages"] = ls.get("learned_messages", 0)
                    detail["skills_auto"] = ls.get("skills_auto", 0)
                    detail["kb_files"] = ls.get("kb_files", 0)
                    detail["learn_running"] = ls.get("running", False)
                if learn_bus:
                    q = learn_bus.summary()
                    detail["rt_pending"] = q.get("pending", 0)
                    detail["rt_runs"] = q.get("runs", 0)
                    detail["rt_ok"] = q.get("ok_runs", 0)
                    detail["rt_running"] = q.get("running", False)
            except Exception as e:
                detail["probe_error"] = str(e)
            self._send_json({"ok": True, "ver": APP_VER,
                             "caps": caps, "detail": detail})
            return

        # ============ v9：自学习 ============
        if path == "/api/learn/status":
            if not learn_api:
                return self._send_error("学习模块未加载")
            r = learn_api.status()
            if learn_bus:                     # v10：附带实时队列状态
                r["realtime"] = learn_bus.status()
            return self._send_json(r)

        if path == "/api/learn/queue":
            if not learn_bus:
                return self._send_json({"ok": False, "error": "实时学习未加载"})
            return self._send_json({"ok": True, "queue": learn_bus.status()})

        # ============ v10：自动部署状态（只读）============
        if path == "/api/deploy/status":
            return self._send_json(_deploy_status())

        if path == "/api/learn/history":
            if not learn_api:
                return self._send_error("学习模块未加载")
            return self._send_json(learn_api.history(
                int((q.get("limit") or ["30"])[0] or 30)))

        if path == "/api/learn/report":
            if not learn_api:
                return self._send_error("学习模块未加载")
            return self._send_json(learn_api.report(
                (q.get("date") or [None])[0]))

        if path == "/api/learn/kb":
            if not learn_api:
                return self._send_error("学习模块未加载")
            return self._send_json(learn_api.kb_list(
                int((q.get("limit") or ["50"])[0] or 50)))

        if path == "/api/learn/kb/read":
            if not learn_api:
                return self._send_error("学习模块未加载")
            return self._send_json(learn_api.read_kb(
                (q.get("name") or q.get("path") or [""])[0]))

        if path == "/api/git/note":
            if not git_api:
                return self._send_error("笔记模块未加载")
            return self._send_json(git_api.read_note(
                (q.get("name") or q.get("path") or [""])[0]))

        if path == "/api/models":
            cfg = core.load_config()
            base = (q.get("base") or [cfg.get("base")])[0]
            try:
                ms = core.list_models_full(base)
                self._send_json({"ok": True, "base": base, "models": ms})
            except Exception as e:
                self._send_json({"ok": False, "error": "连接失败：%s" % e,
                                 "base": base}, 502)
            return

        if path == "/api/config":
            self._send_json({"ok": True, "config": core.load_config(),
                             "app": APP_NAME, "ver": APP_VER})
            return

        # v7：语音/论文能力探测（前端 GET 调用）
        if path == "/api/speech/status":
            if not speech_api:
                return self._send_error("speech_api 未加载")
            st = speech_api.status()
            st["ok"] = True
            self._send_json(st)
            return

        # ============ v11：多供应商 / 模型中心 ============
        if path == "/api/providers":
            if not provider_api:
                return self._send_error("供应商模块未加载")
            return self._send_json(provider_api.list_providers())

        if path == "/api/providers/models":
            pid = (q.get("id") or [""])[0] or "ollama"
            if not provider_api:
                return self._send_error("供应商模块未加载")
            return self._send_json(provider_api.models(pid))

        if path == "/api/providers/tools":
            if not provider_api:
                return self._send_error("供应商模块未加载")
            return self._send_json(provider_api.tools())

        if path == "/api/auth/status":
            if not authz:
                return self._send_json({"ok": True, "enabled": False,
                                        "logged_in": True})
            cookies = authz.parse_cookies(self.headers.get("Cookie") or "")
            info = self._info()
            cid, nm = self._client()
            uid = authz.verify_user_token(cookies.get(authz.COOKIE, "")) or ""
            if uid == "_":
                uid = ""
            rec = users_store.get(uid) if (users_store and uid) else None
            hu = bool(users_store and users_store.has_users())
            return self._send_json({
                "ok": True,
                "enabled": authz.enabled(),
                "logged_in": bool(uid) or (not hu and not authz.enabled())
                              or (not hu and authz.verify_token(
                                  cookies.get(authz.COOKIE, ""))),
                "has_users": hu,
                "need_setup": (not hu),
                "user": users_store.public(rec) if rec else None,
                "lan_free": authz.lan_free(),
                "trust_ts_user": authz.trust_ts_user(),
                "peer_ip": info.get("ip"),
                "proxied": info.get("proxied"),
                "ts_authenticated": info.get("ts_authenticated"),
                "ts_login": info.get("ts_login"),
                "ts_name": info.get("ts_name"),
                "identified": bool(cid),
                "client_name": nm,
            })

        # 公开：登录页据此决定显示「登录」还是「初始化管理员」
        if path == "/api/auth/info":
            hu = bool(users_store and users_store.has_users())
            return self._send_json({"ok": True, "has_users": hu,
                                    "need_setup": not hu,
                                    "enabled": bool(authz and authz.enabled())})

        if path == "/api/users":
            if not users_store:
                return self._send_error("用户模块未加载")
            if not users_store.has_users():
                return self._send_json({"ok": True, "users": []})
            uid = cur_uid()
            rec = users_store.get(uid)
            if not rec or rec.get("role") != "admin":
                return self._send_json({"ok": False, "error": "仅管理员可见"}, 403)
            out = []
            for u in users_store.list_users():
                u["usage"] = users_store.usage(u["uid"])
                out.append(u)
            return self._send_json({"ok": True, "users": out})

        if path == "/api/whoami":
            cid, nm = self._client()
            ss, _cur = get_state()
            mine = len([s for s in ss if cid and s.owner == cid])
            shared = len([s for s in ss if not s.owner])
            return self._send_json({
                "ok": True, "client_id": cid, "client_name": nm,
                "identified": bool(cid), "my_sessions": mine,
                "shared_sessions": shared,
                "total_sessions": len(ss),
            })

        if path == "/api/sessions":
            cid, _nm = self._client()
            ss, cur = get_state()
            vis = self._visible(ss, cid)
            ids = {s.id for s in vis}
            run = rt_running()
            items = []
            for s in sorted(vis, key=lambda x: x.updated, reverse=True):
                d = s.to_dict()
                r = run.get(s.id)
                d["busy"] = bool(r and not r.get("done"))
                items.append(d)
            self._send_json({
                "ok": True,
                "sessions": items,
                # current 必须也在可见集合里，否则前端会选中一个拿不到的会话
                "current": cur if cur in ids else (vis[0].id if vis else None),
                "client_id": cid,
                "max_concurrent": MAX_CONCURRENT_CHAT,
                "running": sum(1 for v in run.values() if not v.get("done")),
            })
            return

        if path == "/api/session/live":
            sid = (q.get("id") or [""])[0]
            if not sid:
                return self._send_json({"ok": False, "error": "缺少 id"}, 400)
            ss, _cur = get_state()
            s = find_session(sid)
            if s is None:
                return self._send_json({"ok": False, "error": "会话不存在"}, 404)
            r = rt_live(sid)
            self._send_json({
                "ok": True,
                "busy": bool(r and not r.get("done")),
                "queued": bool(r and r.get("queued")),
                "text": (r or {}).get("text") or "",
                "think": (r or {}).get("think") or "",
                "tools": (r or {}).get("tools") or [],
                "age": (r or {}).get("age") or 0,
                "done": bool(r and r.get("done")),
                "messages": len(s.messages),
            })
            return

        if path == "/api/fs/roots":
            ws = fs_api.load_workspaces()
            self._send_json({"ok": True, "workspaces": ws,
                             "roots": fs_api.default_roots()})
            return

        if path == "/api/fs/list":
            path_ = (q.get("path") or [""])[0]
            if not path_:
                # 默认列第一个工作区
                ws = fs_api.load_workspaces()
                if not ws:
                    return self._send_json({"ok": True, "empty": True,
                                            "items": [], "path": ""})
                path_ = ws[0]["path"]
            self._send_json(fs_api.list_dir(path_))
            return

        if path == "/api/fs/stat":
            p = (q.get("path") or [""])[0]
            self._send_json(fs_api.stat_path(p) if p else
                            {"ok": False, "error": "缺少 path"})
            return

        if path == "/api/fs/image":
            p = (q.get("path") or [""])[0]
            self._send_json(fs_api.read_image_b64(p) if p else
                            {"ok": False, "error": "缺少 path"})
            return

        if path == "/api/fs/file":
            p = (q.get("path") or [""])[0]
            if not p:
                return self._send_error("缺少 path")
            download = (q.get("download") or [""])[0] in ("1", "true", "yes")
            return self._send_workspace_file(p, download=download)

        if path == "/api/fs/read":
            p = (q.get("path") or [""])[0]
            if not p:
                return self._send_error("缺少 path")
            self._send_json(fs_api.read_text(p))
            return

        # ---------- v16.3：版本与更新清单 ----------
        if path in ("/api/version", "/api/update/manifest"):
            try:
                from . import version_api as _va
            except Exception as _e:  # noqa: BLE001
                return self._send_json({"ok": False,
                                        "error": "版本模块未加载: %s" % _e})
            if path == "/api/version":
                return self._send_json(_va.info(APP_DIR))
            return self._send_json(_va.manifest(APP_DIR))

        # ---------- v6：技能列表 ----------
        if path == "/api/skills":
            if not skills:
                return self._send_json({"ok": False, "error": "技能引擎未加载"})
            ss = skills.load_skills(APP_DIR)
            self._send_json({
                "ok": True,
                "skills": [{"id": s["id"], "name": s["name"],
                            "desc": s["desc"], "type": s["type"],
                            "trigger": s["trigger"]} for s in ss],
                "dirs": skills.skill_dirs(APP_DIR),
            })
            return

        # ---------- v6：语音后端可用性 ----------
        if path == "/api/voice/status":
            if not voice_api:
                return self._send_json({"ok": False, "error": "语音模块未加载"})
            cfg = core.load_config()
            ssh_conf = None
            if cfg.get("ssh_host"):
                ssh_conf = {"host": cfg["ssh_host"],
                            "user": cfg.get("ssh_user") or "nvidia",
                            "python": cfg.get("ssh_python") or "python3"}
            backs = voice_api.asr_backends(ssh_conf)
            import shutil as _sh
            self._send_json({
                "ok": True,
                "asr": [{"name": b[0], "desc": b[1], "ok": b[2]} for b in backs],
                "asr_ready": any(b[2] for b in backs),
                "tts_windows": os.name == "nt",
                "tts_edge": bool(_sh.which("edge-tts")),
            })
            return

        # ============ v8：具身仿真桥 ============
        if path == "/api/embodied/status":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.status())

        if path == "/api/embodied/job":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            jid = (q.get("id") or [""])[0]
            return self._send_json(embodied_api.job(jid))

        if path == "/api/embodied/ros/topics":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.ros_topics())

        if path == "/api/embodied/ros/nodes":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.ros_nodes())

        if path == "/api/embodied/ros/topic":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.ros_topic_info(
                (q.get("topic") or [""])[0]))

        if path == "/api/embodied/ros/echo":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.ros_echo(
                (q.get("topic") or [""])[0], int((q.get("n") or ["1"])[0] or 1)))

        if path == "/api/embodied/screenshot":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.screenshot())

        if path == "/api/embodied/isaac/logs":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.isaac_logs(
                int((q.get("n") or ["60"])[0] or 60)))

        # ============ v8：笔记仓库 ============
        if path == "/api/git/status":
            if not git_api:
                return self._send_error("笔记模块未加载")
            return self._send_json(git_api.status())

        if path == "/api/git/notes":
            if not git_api:
                return self._send_error("笔记模块未加载")
            return self._send_json(git_api.notes())

        if path == "/api/git/log":
            if not git_api:
                return self._send_error("笔记模块未加载")
            return self._send_json(git_api.log(int((q.get("n") or ["10"])[0] or 10)))

        # ============ v8：朗读 ============
        if path == "/api/tts/status":
            if not paper_reader:
                return self._send_error("朗读模块未加载")
            return self._send_json(paper_reader.status())

        if path == "/api/tts/audio":
            if not paper_reader:
                return self._send_error("朗读模块未加载")
            doc = (q.get("doc") or [""])[0]
            try:
                idx = int((q.get("i") or ["0"])[0])
            except Exception:
                idx = 0
            blob = paper_reader.read_audio(doc, idx)
            if not blob:
                return self._send_error("音频不存在，请先合成", 404)
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            try:
                self.wfile.write(blob)
            except Exception:
                pass
            return

        self._send_error("未知接口：%s" % path, 404)

    # ================= POST API =================
    def _api_post(self, path, body):
        # ---- 配置 ----
        if path == "/api/config":
            patch = body.get("config") or {}
            if "base" in patch and patch["base"]:
                bases = patch.get("bases") or core.load_config().get("bases")
                if patch["base"] not in bases:
                    bases.insert(0, patch["base"])
                patch["bases"] = list(dict.fromkeys(bases))
            cfg = core.patch_config(patch)
            self._send_json({"ok": True, "config": cfg})
            return

        # ---- 会话 ----
        if path == "/api/session/new":
            cfg = core.load_config()
            cid, cname = self._client()        # v10：会话归属
            s = core.Session(model=body.get("model") or cfg["model"],
                             base=body.get("base") or cfg["base"],
                             system=body.get("system"),
                             owner=cid, owner_name=cname,
                             provider=body.get("provider") or "ollama",
                             agent_mode=bool(body.get("agent_mode")))
            ss, _ = get_state()
            ss.insert(0, s)
            set_state(ss, s.id)
            persist()
            self._send_json({"ok": True, "session": s.to_dict()})
            return

        if path == "/api/session/select":
            sid = body.get("id")
            s = find_session(sid)
            if not s:
                return self._send_error("会话不存在", 404)
            ss, _ = get_state()
            set_state(ss, s.id)
            persist()
            self._send_json({"ok": True, "session": s.to_dict()})
            return

        if path == "/api/session/rename":
            sid, title = body.get("id"), (body.get("title") or "").strip()
            s = find_session(sid)
            if not s:
                return self._send_error("会话不存在", 404)
            s.title = title[:40] or "新对话"
            s.updated = time.time()
            persist()
            self._send_json({"ok": True, "session": s.to_dict()})
            return

        if path == "/api/session/delete":
            sid = body.get("id")
            ss, cur = get_state()
            if not any(s.id == sid for s in ss):
                # 会话不属于当前用户：按不存在处理，避免越权探测
                return self._send_error("会话不存在", 404)
            ss = [s for s in ss if s.id != sid]
            if not ss:
                ns = core.Session(model=core.load_config()["model"])
                ss = [ns]
            if cur == sid:
                cur = ss[0].id
            set_state(ss, cur)
            persist()
            self._send_json({"ok": True, "current": cur,
                             "sessions": [s.to_dict() for s in ss]})
            return

        if path == "/api/session/clear":
            sid = body.get("id")
            s = find_session(sid)
            if not s:
                return self._send_error("会话不存在", 404)
            s.messages = []
            s.updated = time.time()
            persist()
            self._send_json({"ok": True, "session": s.to_dict()})
            return

        if path == "/api/session/update":
            """更新会话的模型 / 系统提示 / 温度等（v11 加 provider/agent_mode）"""
            sid = body.get("id")
            s = find_session(sid)
            if not s:
                return self._send_error("会话不存在", 404)
            for k in ("model", "base", "system", "provider"):
                if body.get(k) is not None:
                    setattr(s, k, body[k])
            if body.get("agent_mode") is not None:
                s.agent_mode = bool(body["agent_mode"])
            s.updated = time.time()
            persist()
            self._send_json({"ok": True, "session": s.to_dict()})
            return

        # ---- 对话（SSE）----
        if path == "/api/chat":
            return self._handle_chat(body)

        if path == "/api/chat/stop":
            sid = body.get("id")
            st = _stoppers.get(sid)
            if st:
                st.stop()
            self._send_json({"ok": True})
            return

        # ---- 文件系统 ----
        if path == "/api/fs/read":
            self._send_json(fs_api.read_text(body.get("path")))
            return
        if path == "/api/fs/write":
            self._send_json(fs_api.write_text(body.get("path"),
                                              body.get("content") or "",
                                              body.get("encoding") or "utf-8"))
            return
        if path == "/api/fs/create":
            self._send_json(fs_api.create_file(body.get("path"),
                                               body.get("content") or ""))
            return
        if path == "/api/fs/mkdir":
            self._send_json(fs_api.mkdir(body.get("path")))
            return
        if path == "/api/fs/rename":
            self._send_json(fs_api.rename(body.get("path"), body.get("name")))
            return
        if path == "/api/fs/trash":
            self._send_json(fs_api.trash(body.get("paths") or []))
            return
        if path == "/api/fs/delete":
            self._send_json(fs_api.hard_delete(body.get("paths") or []))
            return
        if path == "/api/fs/search":
            self._send_json(fs_api.search_by_name(body.get("keyword"),
                                                  body.get("root"),
                                                  int(body.get("limit") or 200)))
            return
        if path == "/api/fs/grep":
            self._send_json(fs_api.search_content(body.get("keyword"),
                                                  body.get("root"),
                                                  int(body.get("limit") or 100)))
            return

        # ---- 工作区 ----
        if path == "/api/ws/add":
            ok, msg, ws = fs_api.add_workspace(body.get("path"))
            self._send_json({"ok": ok, "msg": msg, "workspaces": ws,
                             "roots": fs_api.default_roots()})
            return
        if path == "/api/ws/remove":
            ws = fs_api.remove_workspace(body.get("path"))
            self._send_json({"ok": True, "workspaces": ws,
                             "roots": fs_api.default_roots()})
            return
        if path == "/api/ws/browse":
            """列出本机盘符 + 常用目录，供添加工作区时选择"""
            items = []
            for d in "CDEFG":
                p = "%s:\\" % d
                if os.path.isdir(p):
                    try:
                        import shutil as _sh
                        u, t, f = _sh.disk_usage(p)
                        items.append({"path": p, "name": "%s 盘" % d,
                                      "total": t, "free": f,
                                      "used_pct": round((u / t) * 100, 1)})
                    except Exception:
                        pass
            home = os.path.expanduser("~")
            for name, sub in (("主目录", ""), ("桌面", "Desktop"),
                              ("文档", "Documents"), ("下载", "Downloads")):
                p = os.path.join(home, sub) if sub else home
                if os.path.isdir(p):
                    items.append({"path": p, "name": name, "kind": "shortcut"})
            self._send_json({"ok": True, "items": items})
            return

        # ---- 命令执行（Agent 模式）----
        if path == "/api/exec/check":
            cmd = body.get("command") or ""
            self._send_json({"ok": True, "dangerous": core.is_dangerous(cmd),
                             "command": cmd})
            return
        if path == "/api/exec":
            cmd = (body.get("command") or "").strip()
            if not cmd:
                return self._send_error("命令为空")
            cwd = body.get("cwd") or None
            if cwd:
                rp = fs_api.resolve_in_workspace(cwd)
                if not rp:
                    return self._send_error("工作目录不在工作区内")
                cwd = rp
            code, out = core.run_command(cmd, cwd=cwd)
            self._send_json({"ok": True, "code": code, "output": out})
            return

        # ============ v6：视频理解 / 语音 / 技能 ============
        if path == "/api/video/analyze":
            if not video_api:
                return self._send_error("视频模块未加载")
            cfg = core.load_config()
            base = body.get("base") or cfg.get("base")
            video = body.get("video") or ""
            if not video:
                return self._send_error("缺少 video 路径")
            ssh_conf = None
            if video_api.is_remote(video) or body.get("remote"):
                ssh_conf = (body.get("host") or cfg.get("ssh_host") or "127.0.0.1",
                            body.get("user") or cfg.get("ssh_user") or "nvidia")
            try:
                avail = [m["name"] for m in core.list_models_full(base)]
            except Exception:
                avail = []
            res = video_api.analyze_video(
                base, body.get("model"), video,
                body.get("question") or "请描述这段视频的作业流程",
                frames=int(body.get("frames") or 8),
                ssh=ssh_conf, available=avail)
            self._send_json(res)
            return

        if path == "/api/voice/asr":
            if not voice_api:
                return self._send_error("语音模块未加载")
            cfg = core.load_config()
            audio = body.get("audio") or ""
            if not audio:
                return self._send_error("缺少 audio 路径")
            ssh_conf = None
            if cfg.get("ssh_host"):
                ssh_conf = {"host": cfg["ssh_host"],
                            "user": cfg.get("ssh_user") or "nvidia",
                            "python": cfg.get("ssh_python") or "python3"}
            self._send_json(voice_api.transcribe(
                audio, ssh_conf, lang=body.get("lang") or "zh"))
            return

        if path == "/api/voice/tts":
            if not voice_api:
                return self._send_error("语音模块未加载")
            text = (body.get("text") or "").strip()
            if not text:
                return self._send_error("文本为空")
            self._send_json(voice_api.tts(
                text, backend=body.get("backend") or "auto",
                voice=body.get("voice") or "zh-CN-XiaoxiaoNeural",
                rate=int(body.get("rate") or 0)))
            return

        # ============ v7：语音交互 / 论文朗读 ============
        if path == "/api/speech/status":
            if not speech_api:
                return self._send_error("speech_api 未加载")
            self._send_json({"ok": True, **speech_api.status()})
            return

        if path == "/api/speech/asr":
            if not speech_api:
                return self._send_error("speech_api 未加载")
            b64 = body.get("audio") or ""
            if not b64:
                return self._send_error("缺少 audio(base64)")
            tmp = speech_api._write_b64(b64, ".webm")
            if not tmp:
                return self._send_error("audio 不是合法 base64")
            try:
                self._send_json(speech_api.transcribe(
                    tmp, size=body.get("size") or "small",
                    lang=body.get("lang") or "zh"))
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            return

        if path == "/api/pdf/read":
            if not speech_api:
                return self._send_error("speech_api 未加载")
            src = body.get("pdf") or body.get("path") or ""
            if not src:
                return self._send_error("缺少 pdf(base64) 或 path")
            tmp = None
            if src.startswith("/") or (len(src) < 4096 and "\n" not in src
                                       and not src.startswith("data:")):
                tmp = src                      # 服务端路径
            else:
                tmp = speech_api._write_b64(src, ".pdf")
                if not tmp:
                    return self._send_error("pdf 不是合法 base64")
            try:
                self._send_json(speech_api.pdf_text(
                    tmp, max_pages=int(body.get("max_pages") or 0),
                    max_chars=int(body.get("max_chars") or 60000)))
            finally:
                try:
                    if tmp and tmp.startswith(tempfile.gettempdir()):
                        os.remove(tmp)
                except Exception:
                    pass
            return

        if path == "/api/pdf/ask":
            if not speech_api:
                return self._send_error("speech_api 未加载")
            src = body.get("pdf") or body.get("path") or ""
            if not src:
                return self._send_error("缺少 pdf(base64) 或 path")
            cfg = core.load_config()
            base = body.get("base") or cfg.get("base")
            model = body.get("model") or cfg.get("model") or "qwen3-coder:30b"
            tmp = src if src.startswith("/") else speech_api._write_b64(src, ".pdf")
            if not tmp:
                return self._send_error("pdf 不是合法 base64")
            try:
                self._send_json(speech_api.pdf_ask(
                    base, model, tmp,
                    body.get("question") or "请概括这篇论文的核心内容",
                    max_chars=int(body.get("max_chars") or 60000)))
            finally:
                try:
                    if tmp and tmp.startswith(tempfile.gettempdir()):
                        os.remove(tmp)
                except Exception:
                    pass
            return

        if path == "/api/skill/match":
            if not skills:
                return self._send_error("技能引擎未加载")
            text_ = body.get("text") or body.get("q") or ""
            all_sk = skills.load_skills(APP_DIR)
            ranked = skills.match_ranked(text_, all_sk, top=3)
            if not ranked:
                return self._send_json({"ok": True, "hit": False,
                                        "candidates": []})
            sk = ranked[0][0]
            return self._send_json({
                "ok": True, "hit": True,
                "skill": {"id": sk["id"], "name": sk["name"], "type": sk["type"]},
                "args": text_,
                "score": ranked[0][1],
                "candidates": [{"id": s["id"], "name": s["name"], "score": sc}
                               for s, sc in ranked],
                "ambiguous": len(ranked) > 1 and
                             (ranked[0][1] - ranked[1][1]) < 0.6,
            })

        if path == "/api/skill/run":
            if not skills:
                return self._send_error("技能引擎未加载")
            sid = body.get("id") or ""
            ss = skills.load_skills(APP_DIR)
            sk = next((x for x in ss if x["id"] == sid), None)
            if not sk:
                return self._send_error("技能不存在：%s" % sid, 404)
            cfg = core.load_config()
            ctx = {"host": cfg.get("ssh_host") or "",
                   "user": cfg.get("ssh_user") or "nvidia"}
            ok, out = skills.run_steps(sk, ctx)
            resp = {"ok": True, "steps_ok": ok, "output": out,
                    "name": sk["name"], "type": sk["type"],
                    "system": sk["system"], "prompt": sk["prompt"]}
            # prompt / hybrid 技能：用 LLM 生成可执行的结论（让技能真正干活）
            if sk.get("type") in ("prompt", "hybrid") \
                    and (sk.get("system") or sk.get("prompt")):
                try:
                    user_input = (body.get("input") or body.get("text")
                                  or body.get("args") or "")
                    skill_model = body.get("model") or "qwen3-coder:30b"
                    sys_msg = sk.get("system") or \
                        "你是 SOP 工程助手，请用中文给出可执行的结论。"
                    p = (sk.get("prompt") or "").replace(
                        "{{input}}", user_input or "（用户未提供额外输入）")
                    msgs = [{"role": "system", "content": sys_msg},
                            {"role": "user", "content": p}]
                    if out.strip():
                        msgs.append({"role": "user",
                                     "content": "以下是已执行的步骤数据，"
                                                "请据此给出结论：\n" + out})
                    try:
                        ans, _ = core.chat_stream(
                            cfg["base"], skill_model, msgs,
                            temperature=0.5, timeout=300)
                    except Exception:
                        ans, _ = core.chat_stream(
                            cfg["base"], core.DEFAULT_MODEL, msgs,
                            temperature=0.5, timeout=300)
                    resp["answer"] = ans
                except Exception as e:
                    resp["answer_error"] = "LLM 结论生成失败：%s" % e
                    if not out.strip():
                        resp["output"] = ((sk.get("system") or "")
                                          + "\n\n" + (sk.get("prompt") or ""))
            # 文档类技能（论文精读 / 思维导图 / 总结 / 创新点）：读 PDF 后交给 LLM
            if sk.get("type") == "document":
                if not speech_api:
                    resp["answer"] = "PDF 模块未加载"
                else:
                    pdf_src = body.get("pdf") or body.get("path") or ""
                    if not pdf_src:
                        resp["answer"] = ("这是论文类技能。请先点页面上的「📄 论文」"
                                          "选一篇 PDF，再选具体模式。")
                    else:
                        tmp = None
                        try:
                            tmp = (pdf_src if pdf_src.startswith("/")
                                   else speech_api._write_b64(pdf_src, ".pdf"))
                            if not tmp:
                                resp["answer"] = "PDF 数据无效（不是合法 base64）"
                            else:
                                r = speech_api.pdf_text(
                                    tmp, max_chars=int(
                                        body.get("max_chars") or 70000))
                                resp["pages"] = r.get("pages")
                                resp["chars"] = r.get("chars")
                                if not r.get("ok") or not (r.get("text") or "").strip():
                                    resp["answer"] = ("PDF 解析失败：%s"
                                                      % (r.get("error") or "无文本"))
                                else:
                                    tpl = sk.get("prompt") or "{{input}}"
                                    user_p = tpl.replace("{{input}}", r["text"])
                                    msgs = [
                                        {"role": "system",
                                         "content": sk.get("system") or
                                         "你是论文助手，用中文作答。"},
                                        {"role": "user", "content": user_p}]
                                    ans, _ = core.chat_stream(
                                        cfg["base"],
                                        body.get("model") or "qwen3-coder:30b",
                                        msgs, temperature=0.4, timeout=900)
                                    resp["answer"] = ans
                        except Exception as e:
                            resp["answer"] = "论文技能执行失败：%s" % e
                        finally:
                            try:
                                if tmp and tmp.startswith(tempfile.gettempdir()):
                                    os.remove(tmp)
                            except Exception:
                                pass
            self._send_json(resp)
            return

        # ============ v8：具身仿真桥 ============
        if path == "/api/embodied/isaac/pull":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.isaac_pull(body.get("tag")))

        if path == "/api/embodied/isaac/start":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.isaac_start(
                image=body.get("image"), gui=bool(body.get("gui", True)),
                usd=body.get("usd"), seconds=float(body.get("seconds") or 0),
                mode=body.get("mode") or "auto"))

        if path == "/api/embodied/isaac/stop":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.isaac_stop())

        if path == "/api/embodied/ros/pub":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.ros_publish(
                body.get("topic"), body.get("type"), body.get("payload")))

        if path == "/api/embodied/vla/plan":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            cfg = core.load_config()
            return self._send_json(embodied_api.vla_plan(
                body.get("text") or "",
                base=body.get("base") or cfg.get("base"),
                model=body.get("model") or cfg.get("model"),
                chat_stream=core.chat_stream))

        if path == "/api/embodied/vla/run":
            if not embodied_api:
                return self._send_error("具身模块未加载")
            return self._send_json(embodied_api.vla_run(
                body.get("steps") or [], bool(body.get("stop_on_error", True))))

        # ============ v8：笔记仓库 ============
        if path == "/api/git/init":
            if not git_api:
                return self._send_error("笔记模块未加载")
            return self._send_json(git_api.init(body.get("repo_url"),
                                                body.get("branch") or "main"))

        if path == "/api/git/token":
            if not git_api:
                return self._send_error("笔记模块未加载")
            return self._send_json(git_api.set_token(body.get("token") or ""))

        if path == "/api/git/save":
            if not git_api:
                return self._send_error("笔记模块未加载")
            return self._send_json(git_api.save_note(
                body.get("title") or "", body.get("content") or "",
                tags=body.get("tags") or [],
                auto_commit=bool(body.get("auto_commit", True))))

        if path == "/api/git/push":
            if not git_api:
                return self._send_error("笔记模块未加载")
            return self._send_json(git_api.push(body.get("branch"),
                                                body.get("message")))

        if path == "/api/git/pull":
            if not git_api:
                return self._send_error("笔记模块未加载")
            return self._send_json(git_api.pull(body.get("branch")))

        # ============ v8：论文朗读 ============
        if path == "/api/paper/prepare":
            if not paper_reader:
                return self._send_error("朗读模块未加载")
            src = body.get("pdf") or body.get("path") or ""
            text = body.get("text") or ""
            tmp = None
            if src and not text:
                if src.startswith("/"):
                    tmp = src
                else:
                    if not speech_api:
                        return self._send_error("PDF 模块未加载")
                    tmp = speech_api._write_b64(src, ".pdf")
                    if not tmp:
                        return self._send_error("pdf 不是合法 base64")
            try:
                r = paper_reader.prepare(
                    pdf_path=tmp, text=text, title=body.get("title"),
                    voice=body.get("voice"),
                    rate=int(body.get("rate") or 0),
                    max_chunks=int(body.get("max_chunks") or 400))
            finally:
                try:
                    if tmp and tmp.startswith(tempfile.gettempdir()):
                        os.remove(tmp)
                except Exception:
                    pass
            return self._send_json(r)

        if path == "/api/paper/narrate":
            if not paper_reader:
                return self._send_error("朗读模块未加载")
            cfg = core.load_config()
            src = body.get("pdf") or body.get("path") or ""
            text = body.get("text") or ""
            tmp = None
            if src and not text:
                if src.startswith("/"):
                    tmp = src
                else:
                    if not speech_api:
                        return self._send_error("PDF 模块未加载")
                    tmp = speech_api._write_b64(src, ".pdf")
                    if not tmp:
                        return self._send_error("pdf 不是合法 base64")
            try:
                r = paper_reader.narrate_script(
                    pdf_path=tmp, text=text,
                    base=body.get("base") or cfg.get("base"),
                    model=body.get("model") or cfg.get("model"),
                    chat_stream=core.chat_stream)
            finally:
                try:
                    if tmp and tmp.startswith(tempfile.gettempdir()):
                        os.remove(tmp)
                except Exception:
                    pass
            return self._send_json(r)

        if path == "/api/tts/synth":
            if not paper_reader:
                return self._send_error("朗读模块未加载")
            return self._send_json(paper_reader.synth(
                body.get("doc") or "", int(body.get("i") or 0),
                text=body.get("text"), voice=body.get("voice"),
                rate=int(body.get("rate") or 0),
                force=bool(body.get("force"))))

        if path == "/api/tts/many":
            if not paper_reader:
                return self._send_error("朗读模块未加载")
            return self._send_json(paper_reader.synth_many(
                body.get("doc") or "", body.get("items") or [],
                voice=body.get("voice"), rate=int(body.get("rate") or 0)))

        if path == "/api/tts/purge":
            if not paper_reader:
                return self._send_error("朗读模块未加载")
            return self._send_json(paper_reader.purge(body.get("doc")))

        # ============ v11：登录 / 多供应商 / Agent 工具 ============
        if path == "/api/login":
            # v12：账号密码优先；没有账号时退回 v11 访问口令
            name = (body.get("name") or body.get("username") or "").strip()
            pwd = body.get("password") or body.get("pass") or ""
            code = body.get("code") or ""
            uid = None
            if users_store and users_store.has_users():
                ok, payload = users_store.authenticate(name, pwd or code)
                if not ok:
                    return self._send_json({"ok": False, "error": payload}, 401)
                uid = payload["uid"]
                who = payload
            else:
                if code and authz and authz.enabled() and not authz.check_code(code):
                    return self._send_json({"ok": False, "error": "口令错误"}, 401)
                who = {"uid": "", "name": "guest", "display": "访客", "role": "user"}
                uid = None
            tok = authz.issue_user_token(uid) if authz else ""
            _body = json.dumps({"ok": True, "user": who},
                               ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(_body)))
            self.send_header("Set-Cookie",
                             "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                             % (authz.COOKIE, tok, int(authz.COOKIE_DAYS * 86400)))
            self.end_headers()
            try:
                self.wfile.write(_body)
            except Exception:
                pass
            return

        if path == "/api/signup":
            if not users_store:
                return self._send_json({"ok": False, "error": "用户模块未加载"}, 500)
            name = (body.get("name") or body.get("username") or "").strip()
            pwd = body.get("password") or ""
            display = body.get("display") or ""
            ok, payload = users_store.register(name, pwd, display)
            if not ok:
                return self._send_json({"ok": False, "error": payload}, 400)
            tok = authz.issue_user_token(payload["uid"])
            _body = json.dumps({"ok": True, "user": payload},
                               ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(_body)))
            self.send_header("Set-Cookie",
                             "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                             % (authz.COOKIE, tok, int(authz.COOKIE_DAYS * 86400)))
            self.end_headers()
            try:
                self.wfile.write(_body)
            except Exception:
                pass
            return

        if path == "/api/account/password":
            if not users_store:
                return self._send_error("用户模块未加载")
            uid = cur_uid()
            rec = users_store.get(uid)
            if not rec:
                return self._send_json({"ok": False, "error": "请先登录"}, 401)
            if not users_store.check_pwd(body.get("old") or "", rec.get("pwd") or ""):
                return self._send_json({"ok": False, "error": "原密码不对"}, 403)
            ok, msg = users_store.set_password(uid, body.get("new") or "")
            return self._send_json({"ok": ok, "msg": msg,
                                    "error": None if ok else msg})

        if path == "/api/account/display":
            if not users_store:
                return self._send_error("用户模块未加载")
            uid = cur_uid()
            if not users_store.get(uid):
                return self._send_json({"ok": False, "error": "请先登录"}, 401)
            ok, msg = users_store.set_display(uid, body.get("display") or "")
            return self._send_json({"ok": ok, "display": msg if ok else None,
                                    "error": None if ok else msg})

        if path in ("/api/users/role", "/api/users/delete"):
            if not users_store:
                return self._send_error("用户模块未加载")
            me = users_store.get(cur_uid())
            if not me or me.get("role") != "admin":
                return self._send_json({"ok": False, "error": "仅管理员可操作"}, 403)
            target = body.get("uid") or ""
            if path.endswith("/delete"):
                ok, msg = users_store.remove(target)
                if ok:
                    drop_user_cache(target)
            else:
                ok, msg = users_store.set_role(target, body.get("role") or "")
            return self._send_json({"ok": ok, "msg": msg,
                                    "error": None if ok else msg})

        if path == "/api/logout":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "11")
            self.send_header("Set-Cookie",
                             "%s=; Path=/; HttpOnly; Max-Age=0" % authz.COOKIE)
            self.end_headers()
            try:
                self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            except Exception:
                pass
            return

        if path == "/api/providers/keys":
            if self._secrets_gate():
                return
            if not provider_api:
                return self._send_error("供应商模块未加载")
            pid = (body.get("id") or "").strip()
            return self._send_json(provider_api.set_cred(
                pid, body.get("key"), body.get("base")))

        if path == "/api/providers/keys/delete":
            if self._secrets_gate():
                return
            if not provider_api:
                return self._send_error("供应商模块未加载")
            return self._send_json(
                provider_api.delete_key((body.get("id") or "").strip()))

        if path == "/api/providers/test":
            if not provider_api:
                return self._send_error("供应商模块未加载")
            return self._send_json(provider_api.test(
                (body.get("id") or "").strip(),
                body.get("model"),
                timeout=int(body.get("timeout") or 45)))

        if path == "/api/providers/models/refresh":
            if self._secrets_gate():
                return
            if not provider_api:
                return self._send_error("供应商模块未加载")
            return self._send_json(provider_api.models(
                (body.get("id") or "").strip(), refresh=True))

        if path == "/api/agent/tool/run":
            """单独调试某个工具（不经过模型）"""
            if not agent_tools:
                return self._send_error("工具层未加载")
            r = agent_tools.dispatch((body.get("name") or "").strip(),
                                     body.get("args") or {})
            return self._send_json({"ok": "error" not in (r or {}), "result": r})

        # ============ v9：自学习 ============
        if path == "/api/learn/run":
            if not learn_api:
                return self._send_error("学习模块未加载")
            return self._send_json(learn_api.run(
                force=bool(body.get("force")),
                dry_run=bool(body.get("dry_run")),
                min_chars=int(body.get("min_chars") or 120)))

        # v10：催实时队列立刻消化（不等静默窗口）
        if path == "/api/learn/poke":
            if not learn_bus:
                return self._send_json({"ok": False, "error": "实时学习未加载"})
            return self._send_json(learn_bus.poke())

        self._send_error("未知接口：%s" % path, 404)

    # ================= SSE 对话 =================
    def _handle_chat(self, body):
        sid = body.get("id")
        text = (body.get("text") or "").strip()
        images = body.get("images") or []
        refs = body.get("refs") or []
        temp = float(body.get("temperature") or 0.7)
        want_think = bool(body.get("show_think", True))
        s = find_session(sid) if sid else None
        if s is None:
            return self._send_error("会话不存在，请新建对话", 404)
        if not text and not images and not refs:
            return self._send_error("请输入内容")

        # v14：同一会话不允许并发两条流（会串内容）；不同会话可以并行
        if not rt_begin(s.id, cur_uid(), s.model):
            return self._send_error("这个会话正在生成中，等它完成或先点停止", 429)

        stopper = get_stopper(s.id)
        stopper.clear()

        # 组装 user 消息
        ref_ctx, ref_detail = fs_api.build_refs(refs)
        imgs = []
        for im in images:
            b = im or ""
            k = b.find("base64,")
            if k >= 0:
                b = b[k + 7:]
            if b:
                imgs.append(b)
        full_text = text
        if ref_ctx:
            full_text = ("以下是用户引用的本地文件内容，请结合它们回答问题。\n\n"
                         + ref_ctx
                         + ("\n\n---\n用户的问题：\n" + text if text else ""))
        user_msg = {"role": "user", "content": full_text}
        if imgs:
            user_msg["images"] = imgs
        s.messages.append(user_msg)
        s.auto_title()
        s.updated = time.time()
        persist()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True      # 发完即断，客户端才能收到 EOF

        ok = self._send_sse({"type": "start", "id": s.id,
                             "title": s.title,
                             "refs": ref_detail, "n_images": len(imgs)})
        if not ok:
            rt_end(s.id)
            return

        # v14：并发闸门。超过上限先告诉前端「排队中」，再阻塞等名额。
        # 之所以不直接拒绝：用户开了三个会话一起问是常见操作，
        # 拒绝比排队更让人难受。单个大型模型本来就只能串行算。
        if not _chat_sem.acquire(blocking=False):
            with _runtime_lock:
                waiting = sum(1 for v in _runtime.values()
                              if not v.get("done") and not v.get("queued"))
            rt_update(s.id, queued=True)
            self._send_sse({"type": "queued",
                            "message": "前面还有 %d 个回答在生成，已排队"
                                       % max(waiting, 1)})
            _chat_sem.acquire(timeout=900)
            rt_update(s.id, queued=False)
            self._send_sse({"type": "dequeued"})

        provider = (body.get("provider") or "").strip() or \
            (getattr(s, "provider", "") or "ollama")
        use_tools = bool(body.get("agent_mode", getattr(s, "agent_mode", False)))
        model = body.get("model") or s.model
        # 会话记住最近一次的选择，下次不用重新选
        s.provider = provider
        s.model = model
        s.agent_mode = use_tools
        state = {"think_open": False}

        def on_think(t):
            if not want_think:
                return
            rt_append(s.id, "think", t)
            if not state["think_open"]:
                state["think_open"] = True
                self._send_sse({"type": "think_start"})
            self._send_sse({"type": "think", "text": t})

        def on_delta(t):
            rt_append(s.id, "text", t)
            if state["think_open"]:
                state["think_open"] = False
                self._send_sse({"type": "think_end"})
            self._send_sse({"type": "delta", "text": t})

        t0 = time.time()
        err = None
        answer, think_txt = "", ""
        tool_trace = []

        def on_tool(ev):
            """工具开始执行时通知前端（结果随后一起打包）"""
            with _runtime_lock:
                r = _runtime.get(s.id)
                if r is not None:
                    r.setdefault("tools", []).append(
                        {"name": ev.get("name"), "args": ev.get("args"),
                         "ok": None, "updated": time.time()})
                    r["updated"] = time.time()
            self._send_sse({"type": "tool_start", "name": ev.get("name"),
                            "args": ev.get("args")})

        try:
            msgs = core.api_messages(
                core.trim_history([{"role": "system", "content": s.system}]
                                  + s.messages))
            if provider == "ollama" or agent_tools is None:
                # qwen3-coder 原生支持 Ollama tools，优先走结构化工具回路；
                # 其他本地模型继续用文本 ReAct 兼容层。
                if use_tools and agent_tools is not None:
                    if "qwen3-coder" in (model or "").lower():
                        res = agent_tools.run_with_tools(
                            "ollama", model, msgs,
                            on_delta=on_delta, on_think=on_think, on_tool=on_tool,
                            use_tools=True, temperature=temp, stop_flag=stopper,
                            timeout=600)
                    else:
                        res = agent_tools.run_local_react(
                            s.base, model, msgs,
                            on_delta=on_delta, on_think=on_think, on_tool=on_tool,
                            use_tools=True, temperature=temp, stop_flag=stopper,
                            timeout=600)
                    answer = res.get("content") or ""
                    think_txt = res.get("thinking") or ""
                    tool_trace = res.get("tools") or []
                else:
                    # 本地模型普通对话：行为与 v10 完全一致
                    answer, think_txt = core.chat_stream(
                        s.base, model, msgs, on_delta=on_delta, on_think=on_think,
                        temperature=temp, stop_flag=stopper)
            else:
                # 云端模型：走统一适配层，可选工具回路
                res = agent_tools.run_with_tools(
                    provider, model, msgs,
                    on_delta=on_delta, on_think=on_think, on_tool=on_tool,
                    use_tools=use_tools, temperature=temp, stop_flag=stopper,
                    timeout=300)
                answer = res.get("content") or ""
                think_txt = res.get("thinking") or ""
                tool_trace = res.get("tools") or []
                if res.get("error"):
                    raise RuntimeError(res["error"])
            # v12：工具轨迹统一发 SSE（本地 / 云端一致）
            for i, t in enumerate(tool_trace):
                try:
                    payload = json.dumps(t.get("result") or {},
                                         ensure_ascii=False)[:2000]
                except Exception:
                    payload = str(t.get("result"))[:2000]
                self._send_sse({"type": "tool_result",
                                "name": t.get("name"),
                                "ok": t.get("ok"),
                                "round": t.get("round"),
                                "result": payload})
        except Exception as e:
            err = str(e)

        if stopper.is_set():
            self._send_sse({"type": "stopped"})
        if err:
            self._send_sse({"type": "error", "message": err})
            s.messages.append({"role": "assistant",
                               "content": "[错误] %s" % err})
        else:
            self._send_sse({"type": "think_end"})
            a_msg = {"role": "assistant", "content": answer}
            if think_txt:
                a_msg["think"] = think_txt
            if tool_trace:
                # 工具轨迹存进会话，刷新后还能看到「它当时做了什么」
                a_msg["tool_log"] = [
                    {"name": t.get("name"), "args": t.get("args"),
                     "ok": t.get("ok"),
                     "result_preview": str(t.get("result"))[:600]}
                    for t in tool_trace]
            s.messages.append(a_msg)
        s.updated = time.time()
        persist()

        # v10：把这一轮喂给实时学习总线。
        # publish 只做「内存入队 + 追加落盘」，不调 LLM，所以不会拖慢响应。
        # 整个块包在 try 里：学习坏了也绝不能让对话失败。
        if learn_bus and not err and answer and not stopper.is_set():
            try:
                learn_bus.publish({
                    "q": text, "a": answer,
                    "session": s.id, "title": s.title,
                    "owner": s.owner, "owner_name": s.owner_name,
                    "ts": time.time(),
                })
            except Exception as _e:
                print("[v10] 学习入队失败：%s" % _e)

        self._send_sse({"type": "end", "id": s.id,
                        "elapsed": round(time.time() - t0, 2),
                        "stopped": stopper.is_set(),
                        "session": s.to_dict()})
        # v14：释放并发名额 + 标记运行态结束（文本留 40 秒给前端补取）
        rt_end(s.id)
        try:
            _chat_sem.release()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def pick_port():
    import socket
    for i in range(PORT_SCAN):
        p = PORT + i
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, p))
            s.close()
            return p
        except OSError:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    return PORT


def run_server():
    bootstrap_state()
    start_background()
    port = pick_port()
    httpd = StudioServer((HOST, port), Handler)
    httpd.daemon_threads = True
    url = "http://%s:%d/" % (HOST, port)
    print("[%s] %s v%s" % (time.strftime("%H:%M:%S"), APP_NAME, APP_VER))
    print("[server] 前端目录: %s" % (find_web_root() or "未找到"))
    print("[server] 监听: %s" % url)
    return httpd, port, url


def open_desktop(url, title=None):
    """优先 pywebview 原生窗口，失败退回系统浏览器"""
    title = title or ("%s v%s" % (APP_NAME, APP_VER))
    try:
        import webview
        webview.create_window(title, url, width=1440, height=900,
                              min_size=(1080, 680),
                              background_color="#151823",
                              text_select=True)
        webview.start(debug=False, gui="edgechromium")
        return "pywebview"
    except Exception as e:
        print("[window] pywebview 启动失败：%s" % e)
    try:
        webbrowser.open(url)
        return "browser"
    except Exception as e:
        print("[window] 浏览器打开失败：%s" % e)
        return "none"


# ---------------------------------------------------------------------------
# 多窗口支持：多个实例共享同一个后端，各自开自己的窗口
#   ~/.dgx_studio/server.json 记录当前后端地址，第二个实例直接连过来开新窗口，
#   不再各自起一个服务（否则会话数据会分裂成互不相干的两份）。
# ---------------------------------------------------------------------------
def _server_meta_path():
    try:
        core.ensure_data_dir()
    except Exception:
        pass
    return os.path.join(core.DATA_DIR, "server.json")


def read_server_meta():
    try:
        with open(_server_meta_path(), "r", encoding="utf-8") as f:
            meta = json.loads(f.read() or "{}")
        if meta.get("port"):
            return meta
    except Exception:
        pass
    return None


def write_server_meta(port, pid):
    try:
        with open(_server_meta_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps({"port": port, "pid": pid,
                                "started": int(time.time()),
                                "ver": APP_VER}))
    except Exception:
        pass


def server_alive(port, timeout=2.0):
    """探测已有后端是否还活着"""
    try:
        import urllib.request as _u
        with _u.urlopen("http://%s:%d/api/health" % (HOST, port),
                        timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


class StudioServer(ThreadingHTTPServer):
    """v15：默认 backlog 只有 5 —— 浏览器一次并发 6+ 连接就会 SYN 溢出，
    表现为"有时打不开 / 一直转圈"。抬到 128。"""
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def main():
    import argparse
    ap = argparse.ArgumentParser(description=APP_NAME, add_help=True)
    ap.add_argument("--web", action="store_true",
                    help="只起服务、不开窗口（给别人用浏览器多标签访问）")
    ap.add_argument("--port", type=int, default=0, help="指定端口")
    ap.add_argument("--no-reuse", action="store_true",
                    help="不复用已有后端，强制起新的")
    args = ap.parse_args()

    # 1) 已有后端 -> 直接开一个新窗口连过去（真正的多窗口）
    if not args.no_reuse and not args.web:
        meta = read_server_meta()
        if meta and meta.get("port") and server_alive(meta["port"]):
            url = "http://%s:%d/" % (HOST, meta["port"])
            print("[multi] 复用已有后端 %s，打开新窗口" % url)
            open_desktop(url)
            return

    # 2) 起服务
    bootstrap_state()
    start_background()
    port = args.port or pick_port()
    httpd = StudioServer((HOST, port), Handler)
    httpd.daemon_threads = True
    url = "http://%s:%d/" % (HOST, port)
    print("[%s] %s v%s" % (time.strftime("%H:%M:%S"), APP_NAME, APP_VER))
    print("[server] 前端目录: %s" % (find_web_root() or "未找到"))
    print("[server] 技能目录: %s" % ", ".join(
        skills.skill_dirs(APP_DIR)) if skills else "[server] 技能引擎未启用")
    print("[server] 监听: %s" % url)
    write_server_meta(port, os.getpid())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    if args.web:
        print("[web] 无窗口模式。浏览器打开 %s 即可；"
              "开多个标签页 = 多个独立问答窗口。" % url)
    else:
        mode = open_desktop(url)
        print("[window] 模式: %s  URL: %s" % (mode, url))

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
