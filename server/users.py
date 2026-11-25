# -*- coding: utf-8 -*-
"""多用户账号体系（v12）

为什么要这层：v11 只有一个全局会话池，前端靠 X-Client-Id 过滤 UI，
数据其实混在一个 sessions.json 里 —— 任何人都能通过改 header 看到别人的记录。
这里把「谁是谁」落到服务端，并让每个用户的会话独立落盘。

存储：
  ~/.dgx_studio/users.json           账号表（PBKDF2-SHA256 加盐哈希，绝不存明文）
  ~/.dgx_studio/users/<uid>/sessions.json   该用户自己的会话（物理隔离）

角色：
  admin  第一个注册的人；能看用户列表、改别人角色
  user   普通用户；只能看自己的会话

兼容：
  没有任何账号时，服务退回 v11 行为（全局单会话 + 可选访问口令），
  所以老部署不会因为升级而打不开。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid

DATA_DIR = os.path.join(os.path.expanduser("~"), ".dgx_studio")
USERS_PATH = os.path.join(DATA_DIR, "users.json")
USERS_DIR = os.path.join(DATA_DIR, "users")

_LOCK = threading.RLock()
_ITER = 120000                     # PBKDF2 轮数：aarch64 上约 80ms，够用不拖慢登录
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,32}$")


# ---------------------------------------------------------------------------
# 底层读写
# ---------------------------------------------------------------------------
def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _db():
    d = _read(USERS_PATH, None) or {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("version", 1)
    d.setdefault("users", [])
    return d


def _save(db):
    _write(USERS_PATH, db)


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------
def hash_pwd(pwd: str) -> str:
    """pbkdf2_sha256$轮数$盐$哈希 —— 自带参数，日后调轮数不破坏老密码"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", (pwd or "").encode("utf-8"),
                             salt, _ITER)
    return "pbkdf2_sha256$%d$%s$%s" % (
        _ITER,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"))


def check_pwd(pwd: str, stored: str) -> bool:
    try:
        algo, it, salt_b64, hash_b64 = (stored or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", (pwd or "").encode("utf-8"),
                                 salt, int(it))
        return hmac.compare_digest(dk, want)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------
def has_users() -> bool:
    return bool(_db().get("users"))


def list_users() -> list:
    """给管理页用：不含任何哈希"""
    out = []
    for u in _db().get("users") or []:
        out.append({
            "uid": u.get("uid"),
            "name": u.get("name"),
            "display": u.get("display") or u.get("name"),
            "role": u.get("role") or "user",
            "created": u.get("created"),
            "last_login": u.get("last_login"),
        })
    return out


def get(uid) -> dict | None:
    for u in _db().get("users") or []:
        if u.get("uid") == uid:
            return u
    return None


def by_name(name) -> dict | None:
    n = (name or "").strip().lower()
    for u in _db().get("users") or []:
        if (u.get("name") or "").lower() == n:
            return u
    return None


def register(name, pwd, display="") -> tuple:
    """返回 (ok, payload)。payload 成功时是用户信息，失败时是错误文案"""
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        return False, "用户名需 2–32 位，只能用字母、数字、下划线、点、短横线"
    if len(pwd or "") < 6:
        return False, "密码至少 6 位"
    with _LOCK:
        db = _db()
        if by_name(name):
            return False, "用户名已存在"
        first = not db["users"]
        uid = "u_" + uuid.uuid4().hex[:12]
        rec = {
            "uid": uid,
            "name": name.lower(),
            "display": (display or name).strip()[:24],
            "role": "admin" if first else "user",
            "pwd": hash_pwd(pwd),
            "created": int(time.time()),
            "last_login": 0,
        }
        db["users"].append(rec)
        _save(db)
        os.makedirs(os.path.join(USERS_DIR, uid), exist_ok=True)
    return True, public(rec)


def public(rec) -> dict:
    if not rec:
        return {}
    return {"uid": rec.get("uid"), "name": rec.get("name"),
            "display": rec.get("display") or rec.get("name"),
            "role": rec.get("role") or "user"}


def authenticate(name, pwd) -> tuple:
    rec = by_name(name)
    if not rec or not check_pwd(pwd or "", rec.get("pwd") or ""):
        return False, "用户名或密码错误"
    with _LOCK:
        db = _db()
        for u in db["users"]:
            if u.get("uid") == rec.get("uid"):
                u["last_login"] = int(time.time())
                rec = u
                break
        _save(db)
    return True, public(rec)


def set_password(uid, new_pwd) -> tuple:
    if len(new_pwd or "") < 6:
        return False, "密码至少 6 位"
    with _LOCK:
        db = _db()
        for u in db["users"]:
            if u.get("uid") == uid:
                u["pwd"] = hash_pwd(new_pwd)
                _save(db)
                return True, "已更新"
    return False, "用户不存在"


def set_display(uid, display) -> tuple:
    d = (display or "").strip()[:24]
    if not d:
        return False, "昵称不能为空"
    with _LOCK:
        db = _db()
        for u in db["users"]:
            if u.get("uid") == uid:
                u["display"] = d
                _save(db)
                return True, d
    return False, "用户不存在"


def set_role(uid, role) -> tuple:
    if role not in ("admin", "user"):
        return False, "角色只能是 admin / user"
    with _LOCK:
        db = _db()
        admins = [u for u in db["users"] if u.get("role") == "admin"]
        for u in db["users"]:
            if u.get("uid") == uid:
                if u.get("role") == "admin" and role != "admin" and len(admins) <= 1:
                    return False, "至少要保留一个管理员"
                u["role"] = role
                _save(db)
                return True, "已更新"
    return False, "用户不存在"


def remove(uid) -> tuple:
    with _LOCK:
        db = _db()
        admins = [u for u in db["users"] if u.get("role") == "admin"]
        for u in list(db["users"]):
            if u.get("uid") == uid:
                if u.get("role") == "admin" and len(admins) <= 1:
                    return False, "不能删除唯一的管理员"
                db["users"].remove(u)
                _save(db)
                return True, "已删除"
    return False, "用户不存在"


# ---------------------------------------------------------------------------
# 每用户数据目录
# ---------------------------------------------------------------------------
def user_dir(uid) -> str:
    d = os.path.join(USERS_DIR, uid)
    os.makedirs(d, exist_ok=True)
    return d


def sessions_path(uid) -> str:
    return os.path.join(user_dir(uid), "sessions.json")


def usage(uid) -> dict:
    """该用户占了多少磁盘（会话文件 + 附件）"""
    d = os.path.join(USERS_DIR, uid)
    total, files = 0, 0
    for root, _dirs, names in os.walk(d):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
                files += 1
            except Exception:
                pass
    return {"bytes": total, "files": files}
