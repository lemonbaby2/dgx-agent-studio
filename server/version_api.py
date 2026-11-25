"""版本与更新信息（v16.3）。

设计原则：
1. **单一事实源**：版本号只写在这一个文件，前端/exe/安装包全部向它取值。
2. **只报不推**：后端不在运行中替换自己的代码。这里只提供"有没有新版本"的事实，
   真升级由运维脚本或安装包完成 —— 避免长任务跑到一半被自动重启打断。
3. **客户端可比**：返回 machine 可读字段（version_tuple / build_ts），
   让 Windows 客户端不必解析字符串。
"""
import hashlib
import json
import os
import time

APP_VERSION = "16.4.0"
CHANNEL = "stable"
# 低于此版本的客户端会被要求升级（登录/工具协议不兼容时上调）
MIN_CLIENT = "16.0.0"

FEATURES = [
    "skills-wbimport",     # 外部技能库导入（14 个方法论技能）
    "image-gen",           # ComfyUI + sd-turbo 本地生图
    "version-api",         # /api/version + /api/update/manifest
    "client-selfupdate",   # Windows 客户端自更新
    "pdf-preview",         # 客户端内嵌 PDF 预览
]

_BUILD_TS = None


def _vt(v):
    out = []
    for p in str(v).split(".")[:4]:
        try:
            out.append(int("".join(ch for ch in p if ch.isdigit()) or 0))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def _sha256(path, limit=None):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
            n += len(b)
            if limit and n >= limit:
                break
    return h.hexdigest()


def _package(app_dir):
    """找 web/static 下的客户端安装包，返回最新一个。"""
    for d in (os.path.join(app_dir, "web", "static", "dist"),
              os.path.join(app_dir, "web", "static")):
        if not os.path.isdir(d):
            continue
        cands = [os.path.join(d, f) for f in os.listdir(d)
                 if f.lower().endswith(".exe")]
        if not cands:
            continue
        cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        p = cands[0]
        try:
            st = os.stat(p)
            return {
                "file": os.path.basename(p),
                "url": "/" + os.path.relpath(p, os.path.join(app_dir, "web")).replace(os.sep, "/"),
                "bytes": st.st_size,
                "mtime": int(st.st_mtime),
                "sha256": _sha256(p),
            }
        except OSError:
            continue
    return None


def info(app_dir):
    global _BUILD_TS
    if _BUILD_TS is None:
        try:
            _BUILD_TS = int(os.path.getmtime(os.path.join(app_dir, "server", "version_api.py")))
        except OSError:
            _BUILD_TS = int(time.time())
    pkg = _package(app_dir)
    return {
        "ok": True,
        "app": "DGX Agent Studio",
        "version": APP_VERSION,
        "version_tuple": list(_vt(APP_VERSION)),
        "min_client": MIN_CLIENT,
        "min_client_tuple": list(_vt(MIN_CLIENT)),
        "channel": CHANNEL,
        "build_ts": _BUILD_TS,
        "features": FEATURES,
        "package": pkg,
        "server_time": int(time.time()),
    }


def manifest(app_dir):
    """给 Windows 客户端用的更新清单（可直接落成 latest.json）。"""
    i = info(app_dir)
    return {
        "schema": 1,
        "version": i["version"],
        "version_tuple": i["version_tuple"],
        "channel": i["channel"],
        "build_ts": i["build_ts"],
        "min_client": i["min_client"],
        "notes": "",
        "package": i["package"],
    }


def write_manifest(app_dir, dest=None):
    dest = dest or os.path.join(app_dir, "web", "static", "dist", "latest.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    m = manifest(app_dir)
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    os.replace(tmp, dest)
    return dest, m
