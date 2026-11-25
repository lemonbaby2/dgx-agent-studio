# -*- coding: utf-8 -*-
"""
DGX Agent Studio · v8 笔记仓库（git_api）
================================================================
职责：把对话 / 论文总结 / 实验记录沉淀成 Markdown，并一键 push 到 GitHub。

踩过的坑（都要绕）：
  · DGX 的 ~/.gitconfig 有
        [url "https://gitclone.com/github.com/"] insteadOf = https://github.com/
    所以 push 会**静默改道**到 gitclone 镜像 —— 镜像只读，必然失败，
    而且报错信息看起来像认证问题。必须用
        git -c url.https://github.com/.insteadOf= ...
    显式覆盖（空值 = 清除改写）。
  · 没有 gh CLI（which gh -> 不存在），所以不能走 gh auth。
  · 没配 .git-credentials。token 绝不能写进 .git/config（会被 git 备份/日志带走）。
    做法：token 落 ~/.dgx_studio/git_token（chmod 600），push 时通过
        -c http.https://github.com/.extraheader="AUTHORIZATION: basic <b64>"
    临时注入，进程结束即失效。
  · 中文文件名 + markdown：git 需 core.quotepath=false 才会正常显示。

对外 API：
    status()                       仓库状态 / 是否有 token / 最近提交
    set_token(token)               保存 PAT
    init(repo_url, branch)         初始化或绑定远端（在笔记目录里）
    save_note(title, content, tags) 写一个 markdown 并 commit
    push(branch)                   推送到远端
    log(n)                         最近提交
    notes()                        列出笔记文件
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time

STUDIO_DATA = os.path.join(os.path.expanduser("~"), ".dgx_studio")
TOKEN_PATH = os.path.join(STUDIO_DATA, "git_token")
NOTES_DIR = os.environ.get("DGX_NOTES_DIR") or os.path.join(
    os.path.expanduser("~"), "DGX-Notes")

GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/echo",
    "LANG": "C.UTF-8",
}


def _env():
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env.update(GIT_ENV)
    return env


def _git(args, cwd=None, timeout=120, extra_config=None):
    """执行 git。extra_config: [(key, value)] 用 -c 临时注入，不写盘。"""
    cmd = ["git"]
    for k, v in (extra_config or []):
        cmd += ["-c", "%s=%s" % (k, v)]
    cmd += list(args)
    try:
        p = subprocess.run(cmd, cwd=cwd or NOTES_DIR, capture_output=True,
                           text=True, timeout=timeout, env=_env(),
                           encoding="utf-8", errors="ignore")
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return -1, "git 超时（%ss）" % timeout
    except Exception as e:
        return -1, "git 执行失败：%s" % e


# 清除 insteadOf 改写 —— 这是本模块最关键的一行
NO_REWRITE = [("url.https://github.com/.insteadOf", "")]


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------
def load_token():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""


def set_token(token):
    token = (token or "").strip()
    os.makedirs(STUDIO_DATA, exist_ok=True)
    if not token:
        try:
            os.remove(TOKEN_PATH)
        except Exception:
            pass
        return {"ok": True, "has_token": False}
    if not re.match(r"^(gh[pousr]_[\w]{20,}|github_pat_[\w]{20,})$", token):
        # 不强制，但提示一下
        pass
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(token)
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except Exception:
        pass
    return {"ok": True, "has_token": True}


def _auth_header():
    t = load_token()
    if not t:
        return None
    b = base64.b64encode(("x-access-token:%s" % t).encode()).decode()
    return ("http.https://github.com/.extraheader",
            "AUTHORIZATION: basic %s" % b)


# ---------------------------------------------------------------------------
# 仓库
# ---------------------------------------------------------------------------
def _ensure_dir():
    os.makedirs(NOTES_DIR, exist_ok=True)
    return NOTES_DIR


def _is_repo():
    return os.path.isdir(os.path.join(NOTES_DIR, ".git"))


def status():
    _ensure_dir()
    info = {
        "ok": True,
        "notes_dir": NOTES_DIR,
        "is_repo": _is_repo(),
        "has_token": bool(load_token()),
        "remote": "",
        "branch": "",
        "dirty": 0,
        "last_commit": "",
        "count": 0,
    }
    try:
        info["count"] = len([f for f in os.listdir(NOTES_DIR)
                             if f.endswith((".md", ".markdown"))])
    except Exception:
        pass
    if not _is_repo():
        return info
    c, out = _git(["remote", "-v"], timeout=15)
    if c == 0 and out:
        m = re.search(r"origin\s+(\S+)", out)
        info["remote"] = _mask_url(m.group(1)) if m else ""
    c, out = _git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    if c == 0:
        info["branch"] = out.strip()
    c, out = _git(["status", "--porcelain"], timeout=20)
    if c == 0:
        info["dirty"] = len([l for l in out.splitlines() if l.strip()])
    c, out = _git(["log", "-1", "--pretty=%h %s (%cr)"], timeout=10)
    if c == 0:
        info["last_commit"] = out.strip()[:120]
    return info


def _mask_url(u):
    """把 token/口令从 URL 里抹掉再回显"""
    return re.sub(r"//[^/@]+@", "//***@", u or "")


def init(repo_url=None, branch="main"):
    _ensure_dir()
    msgs = []
    if not _is_repo():
        c, out = _git(["init", "-b", branch], timeout=30)
        msgs.append("git init: %s" % out)
        # 中文路径正常显示
        _git(["config", "core.quotepath", "false"])
        _git(["config", "core.autocrlf", "false"])
        if not _git(["config", "user.name"])[1]:
            _git(["config", "user.name", "DGX Agent Studio"])
        if not _git(["config", "user.email"])[1]:
            _git(["config", "user.email", "gpu@dgx-host.local"])
        # .gitignore：别把大文件塞进仓库
        gi = os.path.join(NOTES_DIR, ".gitignore")
        if not os.path.isfile(gi):
            with open(gi, "w", encoding="utf-8") as f:
                f.write("*.pdf\n*.mp4\n*.pt\n*.onnx\n*.rknn\n"
                        "*.bin\n__pycache__/\n.tmp/\n*.wav\n*.mp3\n")
    if repo_url:
        c, out = _git(["remote", "get-url", "origin"], timeout=10)
        if c == 0:
            _git(["remote", "set-url", "origin", repo_url], timeout=10)
            msgs.append("remote 已更新：%s" % _mask_url(repo_url))
        else:
            c2, out2 = _git(["remote", "add", "origin", repo_url], timeout=10)
            msgs.append("remote 已添加：%s (%s)" % (_mask_url(repo_url), out2))
    return {"ok": True, "msgs": msgs, "status": status()}


# ---------------------------------------------------------------------------
# 写笔记
# ---------------------------------------------------------------------------
def _slug(title):
    t = (title or "").strip() or "note"
    t = re.sub(r'[\\/:*?"<>|\s]+', "-", t)
    return t[:60].strip("-") or "note"


def save_note(title, content, tags=None, auto_commit=True):
    """把内容写成 markdown。文件名带日期前缀，天然按时间排序。"""
    _ensure_dir()
    if not (content or "").strip():
        return {"ok": False, "error": "内容为空"}
    date = time.strftime("%Y-%m-%d")
    name = "%s_%s.md" % (date, _slug(title))
    path = os.path.join(NOTES_DIR, name)
    # 重名加序号
    i = 2
    while os.path.exists(path):
        name = "%s_%s-%d.md" % (date, _slug(title), i)
        path = os.path.join(NOTES_DIR, name)
        i += 1
    tag_line = ("tags: [%s]\n" % ", ".join(tags)) if tags else ""
    front = ("---\ntitle: %s\ndate: %s\nsource: DGX Agent Studio\n%s---\n\n"
             % (title or name, date, tag_line))
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(front + content)
    except Exception as e:
        return {"ok": False, "error": "写入失败：%s" % e}
    res = {"ok": True, "path": path, "name": name, "bytes": os.path.getsize(path)}
    if auto_commit and _is_repo():
        c, out = _git(["add", name], timeout=30)
        c2, out2 = _git(["commit", "-m", "note: %s (%s)"
                         % (title or name, date)], timeout=60)
        res["commit"] = out2.strip()[:300]
        res["committed"] = c2 == 0
    return res


def notes(limit=100):
    _ensure_dir()
    items = []
    for f in sorted(os.listdir(NOTES_DIR), reverse=True):
        if not f.endswith((".md", ".markdown")):
            continue
        p = os.path.join(NOTES_DIR, f)
        try:
            st = os.stat(p)
        except Exception:
            continue
        items.append({"name": f, "path": p, "bytes": st.st_size,
                      "mtime": time.strftime("%Y-%m-%d %H:%M",
                                             time.localtime(st.st_mtime))})
        if len(items) >= limit:
            break
    return {"ok": True, "notes": items, "dir": NOTES_DIR}


def read_note(name_or_path, max_bytes=400_000):
    """读笔记内容。

    ⚠️ 同理不能复用 fs_api.read_text（它约束在工作区内，
    而笔记本体在 ~/DGX-Notes，未必是工作区）。白名单限定 NOTES_DIR 一级子文件。
    """
    if not name_or_path:
        return {"ok": False, "error": "缺少文件"}
    name = os.path.basename(str(name_or_path))
    if not name.endswith((".md", ".markdown")):
        return {"ok": False, "error": "只允许 .md"}
    p = os.path.realpath(os.path.join(NOTES_DIR, name))
    root = os.path.realpath(NOTES_DIR)
    if not p.startswith(root + os.sep):
        return {"ok": False, "error": "路径越界"}
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % name}
    size = os.path.getsize(p)
    if size > max_bytes:
        return {"ok": False, "error": "文件太大"}
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return {"ok": True, "name": name, "path": p,
                    "content": f.read(), "bytes": size}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def log(n=10):
    if not _is_repo():
        return {"ok": False, "error": "笔记目录还不是 git 仓库，先 init"}
    c, out = _git(["log", "-%d" % max(1, min(int(n or 10), 50)),
                   "--pretty=%h|%ad|%s", "--date=format:%m-%d %H:%M"],
                  timeout=15)
    return {"ok": c == 0, "log": out}


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------
def push(branch=None, message=None):
    """commit 本地改动（如指定 message）并 push。"""
    if not _is_repo():
        return {"ok": False, "error": "不是 git 仓库，先 /api/git/init"}
    steps = []
    if message:
        _git(["add", "-A"], timeout=60)
        c, out = _git(["commit", "-m", message], timeout=90)
        steps.append({"commit": out.strip()[:300], "code": c})

    c, out = _git(["remote", "get-url", "origin"], timeout=10)
    if c != 0 or not out.strip():
        return {"ok": False, "error": "未配置 origin，先 /api/git/init 给 repo_url",
                "steps": steps}
    remote = out.strip()

    br = branch
    if not br:
        c, out = _git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
        br = out.strip() or "main"

    cfg = list(NO_REWRITE)
    hdr = _auth_header()
    if hdr and remote.startswith("https://github.com/"):
        cfg.append(hdr)

    c, out = _git(["push", "-u", "origin", br], timeout=180, extra_config=cfg)
    ok = c == 0
    res = {"ok": ok, "branch": br, "remote": _mask_url(remote),
           "output": out[-1500:], "steps": steps,
           "used_token": bool(hdr)}
    if not ok:
        low = (out or "").lower()
        if "authentication failed" in low or "could not read username" in low \
                or "403" in low:
            res["hint"] = ("认证失败：在设置里填 GitHub PAT（需要 repo 权限）；"
                           "或改用 SSH remote（git@github.com:user/repo.git）")
        elif "insteadof" in low or "gitclone" in low:
            res["hint"] = "疑似被 gitclone 镜像改写，检查 ~/.gitconfig"
        elif "rejected" in low or "non-fast-forward" in low:
            res["hint"] = "远端有本地没有的提交，先 git pull --rebase"
        elif "repository not found" in low:
            res["hint"] = "仓库不存在或无权限：确认 PAT 有 repo 权限、仓库已创建"
    return res


def pull(branch=None):
    if not _is_repo():
        return {"ok": False, "error": "不是 git 仓库"}
    cfg = list(NO_REWRITE)
    hdr = _auth_header()
    if hdr:
        cfg.append(hdr)
    args = ["pull", "--rebase", "origin"] + ([branch] if branch else [])
    c, out = _git(args, timeout=180, extra_config=cfg)
    return {"ok": c == 0, "output": out[-1500:]}
