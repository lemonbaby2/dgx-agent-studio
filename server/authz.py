# -*- coding: utf-8 -*-
"""访问口令鉴权（v11）—— 公网开放的前提

为什么要这层：服务一旦 funnel 到公网，任何人都能打开对话页。
页面里有 Agent 能读文件、跑命令的工具 → 必须挡一道。

策略（弱安全，够用且好用）：
  - 环境变量 DGX_STUDIO_ACCESS_CODE 设置口令后：
      * 未登录访问任何页面 → 跳登录页
      * 登录成功下发 httponly cookie，有效期 12 小时
  - DGX_STUDIO_LAN_FREE=1 时，局域网来源（含 tailscale 100.x）免密 —— 同事内网直接开
  - 未设口令：完全开放，但前端顶部挂红条警告
  - 密钥读写接口（/api/providers/keys）永远要求「已登录 或 局域网来源」
    防止公网匿名用户回填别人的 API Key

豁免清单（不鉴权）：/api/health  /login  /api/login  /static/*
"""
import hashlib
import hmac
import ipaddress
import os
import time

DATA_DIR = os.path.expanduser("~/.dgx_studio")
SECRET_FILE = os.path.join(DATA_DIR, ".access_secret")
COOKIE = "dgsid"
COOKIE_DAYS = 0.5          # 12 小时


def enabled():
    return bool(_code())


def _code():
    return (os.environ.get("DGX_STUDIO_ACCESS_CODE") or "").strip()


def lan_free():
    """局域网是否免密"""
    return (os.environ.get("DGX_STUDIO_LAN_FREE") or "1").strip() not in ("0", "false", "no")


def trust_ts_user():
    """是否信任 Tailscale Funnel 透传的已认证用户身份。

    Tailscale Funnel 转发时会注入 Tailscale-User-Login（邮箱）和
    Tailscale-Headers-Info 头，代表这个访客已经在 tailnet 里通过认证。
    没有这两个头的才是真正的互联网匿名访客 —— 那种必须输口令。
    """
    return (os.environ.get("DGX_STUDIO_TRUST_TS_USER") or "1").strip() \
        not in ("0", "false", "no")


def sniff(headers, client_ip):
    """从请求里嗅出「这是谁、从哪来」。返回 dict。

    实测（2026-10-24）Tailscale Funnel 注入的头：
        X-Forwarded-For:        客户端源 IP
        X-Forwarded-Host:       公网主机名
        Tailscale-Headers-Info: https://tailscale.com/s/serve-headers
        Tailscale-User-Login:   liz445299@gmail.com   ← tailnet 已认证用户才有
        Tailscale-User-Name:    中文名（RFC2047 编码）
    """
    try:
        get = headers.get
    except Exception:
        get = lambda k: None          # noqa: E731

    def _g(k):
        try:
            v = get(k)
            return (v or "").strip()
        except Exception:
            return ""

    xff = _g("X-Forwarded-For").split(",")[0].strip()
    proxied = bool(xff) or bool(_g("X-Forwarded-Host"))
    ts_login = _g("Tailscale-User-Login")
    ts_name = decode_rfc2047(_g("Tailscale-User-Name"))
    return {
        "ip": xff or client_ip or "",
        "direct_ip": client_ip or "",
        "proxied": proxied,
        "ts_login": ts_login,
        "ts_name": ts_name,
        "ts_authenticated": bool(ts_login),
    }


def _secret():
    """签名密钥；落盘保证服务重启后 cookie 仍有效"""
    try:
        if os.path.isfile(SECRET_FILE):
            with open(SECRET_FILE, "r", encoding="utf-8") as f:
                s = f.read().strip()
            if s:
                return s
        os.makedirs(DATA_DIR, exist_ok=True)
        s = hashlib.sha256(
            ("%s|%s|%s" % (time.time(), os.getpid(), os.urandom(16)
                           )).encode("utf-8")).hexdigest()
        with open(SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(s)
        try:
            os.chmod(SECRET_FILE, 0o600)
        except Exception:
            pass
        return s
    except Exception:
        return "dgx-studio-fallback-secret"


def is_lan(ip):
    """私有地址 / tailscale 100.x / 本机 都算内网"""
    if not ip:
        return False
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    # Tailscale funnel 转发过来会把真实 IP 放进 header，见 app.py
    if ip.startswith("100.") and 64 <= _second_octet(ip) <= 127:
        return True
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_loopback or a.is_link_local
    except Exception:
        return False


def _second_octet(ip):
    try:
        return int(ip.split(".")[1])
    except Exception:
        return -1


def check_code(code):
    """恒定时间比较，防时序侧信道"""
    want, got = _code(), (code or "").strip()
    if not want:
        return True
    return hmac.compare_digest(want, got)


def issue_token():
    """登录后下发的凭证：时间戳 + HMAC，无服务端状态（v11 旧式，无身份）"""
    exp = int(time.time()) + int(COOKIE_DAYS * 86400)
    sig = hmac.new(_secret().encode("utf-8"),
                   ("%d" % exp).encode("utf-8"),
                   hashlib.sha256).hexdigest()[:32]
    return "%d.%s" % (exp, sig)


def issue_user_token(uid):
    """v12：凭证里带上用户 id，格式 exp.uid.sig

    uid 用 "_" 表示「没有身份的旧式口令登录」，与两段式 token 天然区分。
    加了 uid 后服务端就知道这次请求是谁，会话因此可以物理隔离。
    """
    exp = int(time.time()) + int(COOKIE_DAYS * 86400)
    payload = "%d.%s" % (exp, uid or "_")
    sig = hmac.new(_secret().encode("utf-8"), payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()[:32]
    return "%s.%s" % (payload, sig)


def verify_user_token(tok):
    """校验并取出 uid。

    返回：  uid 字符串（"_" = 旧式口令登录，无身份）
            ""    = 匿名 / 无 cookie
            None  = 非法或过期
    """
    if not tok:
        return ""
    parts = (tok or "").split(".")
    if len(parts) == 3:
        exp, uid, sig = parts
        payload = "%s.%s" % (exp, uid)
    elif len(parts) == 2:
        exp, sig = parts
        uid, payload = "", exp
    else:
        return None
    try:
        if int(exp) < int(time.time()):
            return None
    except Exception:
        return None
    want = hmac.new(_secret().encode("utf-8"), payload.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(want, sig):
        return None
    return uid or "_"


def verify_token(tok):
    """校验 cookie（兼容两段式与三段式）"""
    return verify_user_token(tok) not in ("", None)


def decode_rfc2047(s):
    """Tailscale 传来的中文名是 =?utf-8?q?=E6=9D=8E...?= 编码"""
    if not s or "=?" not in s:
        return s
    try:
        from email.header import decode_header
        parts = decode_header(s)
        out = []
        for val, enc in parts:
            if isinstance(val, bytes):
                out.append(val.decode(enc or "utf-8", "ignore"))
            else:
                out.append(val)
        return "".join(out).strip()
    except Exception:
        return s


def parse_cookies(header):
    out = {}
    for kv in (header or "").split(";"):
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        out[k.strip()] = v.strip()
    return out


PUBLIC_PATHS = ("/api/health", "/api/login", "/api/logout",
                "/api/capabilities", "/api/signup", "/api/auth/info")

# 前端静态资源白名单（不含任何用户数据）—— 免鉴权，可缓存，可做健康探针
ASSET_PATHS = ("/app.css", "/app.js", "/panel.js", "/v6ui.js", "/v7voice.js",
               "/v8.css", "/v8ui.js", "/v9user.js", "/v14multi.js",
               "/v16skills.js", "/v16.css", "/v17update.js", "/app.ico",
               "/favicon.ico")


def allow_anon():
    """无账号时是否允许匿名进入（老部署兼容开关）

    默认 False：没有账号就强制进登录页注册 —— 否则会话没有归属，
    所谓"每人独立账号"就是假的。老部署要保留 v11 行为时设
    DGX_STUDIO_ALLOW_ANON=1。
    """
    return (os.environ.get("DGX_STUDIO_ALLOW_ANON") or "0").strip() \
        in ("1", "true", "yes")


def need_auth(path, info, cookies, has_users=False):
    """是否需要登录。返回 True = 拦住。

    v12 判定：
      1. 有账号体系 → 一律要身份（内网也不例外）。会话按 uid 物理隔离，
         没有身份就无法判定会话归属，放匿名进来只会制造公共垃圾池。
      2. 无账号 → 强制进登录页注册（注册第一个账号即管理员）；
         老部署兼容可设 DGX_STUDIO_ALLOW_ANON=1。
    """
    if path.startswith("/static/") or path in PUBLIC_PATHS:
        return False
    # v16.3：版本与更新清单必须免登录 —— 客户端装完还没登录就要能自检更新，
    # 且这两个接口只暴露版本号与安装包哈希，不含任何用户数据。
    if path in ("/api/version", "/api/update/manifest"):
        return False
    # 前端静态资源免鉴权：不含任何用户数据，放行后浏览器才能缓存，
    # 守护脚本也才能用 /v8ui.js 做"前端是否真的在服务"的探针。
    # （v12 初版把 *.css/*.js 一起拦了，结果所有路径都回登录页 5238B。）
    if path in ASSET_PATHS:
        return False
    uid = verify_user_token(cookies.get(COOKIE, ""))
    if has_users:
        return uid in ("", None)
    if allow_anon():
        return False
    return True


def needs_login_for_secrets(info, cookies):
    """密钥读写必须更强担保：已登录，或来自内网直连。

    注意：这里比 need_auth 更严 —— Tailscale 已认证用户从公网访问时
    也只能「聊」，不能回填 API Key，防止 Key 经公网落到别人手里。
    """
    if verify_token(cookies.get(COOKIE, "")):
        return False
    if not info.get("proxied") and is_lan(info.get("ip")):
        return False
    return True


LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · DGX Embodied Studio</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
 background:linear-gradient(135deg,#0e1420,#16233a);font-family:'Segoe UI',system-ui,sans-serif}
.card{width:min(420px,92vw);background:#fff;border-radius:16px;padding:32px 30px;
 box-shadow:0 20px 60px rgba(0,0,0,.45)}
h1{margin:0 0 4px;font-size:20px;color:#16233a}
p.sub{margin:0 0 18px;color:#6b7a90;font-size:13px;line-height:1.6}
.tabs{display:flex;gap:6px;margin-bottom:18px;background:#f2f5fa;padding:4px;border-radius:10px}
.tabs button{flex:1;border:0;background:transparent;padding:9px;border-radius:8px;
 font-size:14px;color:#5d6d84;cursor:pointer;font-weight:600}
.tabs button.on{background:#fff;color:#16233a;box-shadow:0 1px 4px rgba(0,0,0,.1)}
label{display:block;font-size:12px;color:#7a8798;margin:11px 0 5px}
input{width:100%;padding:11px 13px;border:1px solid #d5dce8;border-radius:9px;
 font-size:15px;outline:none;transition:.15s}
input:focus{border-color:#3b6ef5;box-shadow:0 0 0 3px rgba(59,110,245,.15)}
button.go{width:100%;margin-top:16px;padding:12px;border:0;border-radius:9px;
 background:#3b6ef5;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button.go:hover{background:#2f5ce0}
button.go:disabled{background:#9fb3e8;cursor:default}
.err{color:#d33;font-size:13px;margin-top:11px;min-height:18px}
.ok{color:#12924f;font-size:13px;margin-top:11px;min-height:18px}
.tip{margin-top:16px;padding-top:13px;border-top:1px solid #eef2f8;
 color:#94a2b8;font-size:12px;line-height:1.75}
.hide{display:none}
</style></head><body>
<div class="card">
  <h1>DGX Embodied Studio</h1>
  <p class="sub" id="sub">多模型具身智能体 · 每人独立账号与会话</p>

  <div class="tabs" id="tabs">
    <button id="tabLogin" class="on">登录</button>
    <button id="tabReg">注册</button>
  </div>

  <form id="fLogin">
    <label>用户名</label>
    <input id="lname" autocomplete="username" placeholder="你的用户名">
    <label>密码</label>
    <input id="lpwd" type="password" autocomplete="current-password" placeholder="密码">
    <button class="go" id="btnLogin" type="submit">进入</button>
  </form>

  <form id="fReg" class="hide">
    <label>用户名（2–32 位，字母数字 _ . -）</label>
    <input id="rname" autocomplete="username" placeholder="例如 lzp">
    <label>昵称（显示用，可留空）</label>
    <input id="rdisp" placeholder="例如 李老师">
    <label>密码（至少 6 位）</label>
    <input id="rpwd" type="password" autocomplete="new-password" placeholder="密码">
    <label>再输一次</label>
    <input id="rpwd2" type="password" autocomplete="new-password" placeholder="确认密码">
    <button class="go" id="btnReg" type="submit">创建账号</button>
  </form>

  <div class="err" id="e"></div>
  <div class="tip" id="tip">每个人都用自己的账号登录，会话记录互不可见。</div>
</div>
<script>
var E = document.getElementById('e');
var $ = function (i) { return document.getElementById(i); };

function show(which) {
  var isLogin = which === 'login';
  $('fLogin').className = isLogin ? '' : 'hide';
  $('fReg').className = isLogin ? 'hide' : '';
  $('tabLogin').className = isLogin ? 'on' : '';
  $('tabReg').className = isLogin ? '' : 'on';
  E.textContent = '';
}
$('tabLogin').onclick = function () { show('login'); };
$('tabReg').onclick = function () { show('reg'); };

function post(url, data) {
  return fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  }).then(function (r) { return r.json(); });
}

$('fLogin').onsubmit = function (ev) {
  ev.preventDefault();
  E.textContent = '验证中…';
  $('btnLogin').disabled = true;
  post('/api/login', { name: $('lname').value.trim(), password: $('lpwd').value })
    .then(function (d) {
      if (d.ok) { location.reload(); }
      else { E.textContent = d.error || '登录失败'; $('btnLogin').disabled = false; }
    })
    .catch(function (err) { E.textContent = '网络错误：' + err; $('btnLogin').disabled = false; });
};

$('fReg').onsubmit = function (ev) {
  ev.preventDefault();
  if ($('rpwd').value !== $('rpwd2').value) { E.textContent = '两次密码不一致'; return; }
  E.textContent = '创建中…';
  $('btnReg').disabled = true;
  post('/api/signup', {
    name: $('rname').value.trim(),
    display: $('rdisp').value.trim(),
    password: $('rpwd').value
  }).then(function (d) {
    if (d.ok) { location.reload(); }
    else { E.textContent = d.error || '注册失败'; $('btnReg').disabled = false; }
  }).catch(function (err) { E.textContent = '网络错误：' + err; $('btnReg').disabled = false; });
};

fetch('/api/auth/info').then(function (r) { return r.json(); }).then(function (d) {
  if (d && d.need_setup) {
    show('reg');
    $('tabLogin').classList.add('hide');
    $('sub').textContent = '首次使用：请创建管理员账号（第一个账号即管理员）';
    E.textContent = '';
  }
}).catch(function () { });
</script><script src="/v17update.js"></script>
</body></html>
"""
