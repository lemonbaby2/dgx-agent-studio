# -*- coding: utf-8 -*-
"""
DGX Agent Studio · 本地文件系统 API
------------------------------------------------
安全模型：
  · 只有「工作区」内的路径可访问。工作区 = 用户在界面里显式添加的目录，
    持久化在 %USERPROFILE%\\.dgx_studio\\workspaces.json
  · 任何路径先 resolve（展开符号链接、消掉 ..），再校验是否落在某个工作区内
  · 写操作先备份（同名 .bak，最多保留 3 份）
  · 删除走 Windows 回收站（可还原）
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import shutil
import time

from . import core

MAX_TEXT_BYTES = 2 * 1024 * 1024      # 单文件读取上限 2MB
MAX_SEARCH_FILES = 20000              # 内容搜索扫描文件数上限
MAX_TREE_ENTRIES = 2000               # 单层目录列举上限
PREVIEW_IMAGE_MAX = 12 * 1024 * 1024  # 图片预览上限

# 不进入目录树 / 不搜索的目录名
SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows", "winsxs",
    "node_modules", ".git", "__pycache__", ".venv", "venv", "appdata",
    "programdata", "$windows.~bt", "$windows.~ws", "recovery",
    "perflogs", "config.msi", "msocache", "documents and settings",
}
SKIP_DIRS_L = {d.lower() for d in SKIP_DIRS}

TEXT_EXTS = {
    ".py", ".txt", ".md", ".json", ".js", ".ts", ".html", ".css", ".csv",
    ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml", ".sh", ".bat", ".cmd",
    ".ps1", ".xml", ".sql", ".c", ".h", ".cpp", ".java", ".go", ".rs",
    ".log", ".gitignore", ".env", ".spec", ".service", ".srt", ".svg",
    ".vue", ".jsx", ".tsx", ".lua", ".r", ".m", ".ipynb", ".dart",
}
CODE_EXTS = {".py", ".js", ".ts", ".html", ".css", ".json", ".sh", ".c",
             ".h", ".cpp", ".java", ".go", ".rs", ".sql", ".vue", ".jsx",
             ".tsx", ".lua", ".dart", ".spec", ".service"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".svg"}
BINARY_DOC_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".zip",
                   ".7z", ".pt", ".onnx", ".rknn", ".hef", ".engine"}

LANG_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".html": "html", ".css": "css", ".json": "json", ".md": "markdown",
    ".sh": "bash", ".bat": "batch", ".cmd": "batch", ".ps1": "powershell",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini",
    ".sql": "sql", ".xml": "xml", ".c": "c", ".h": "c", ".cpp": "cpp",
    ".go": "go", ".rs": "rust", ".java": "java", ".lua": "lua",
    ".csv": "csv", ".log": "text", ".txt": "text", ".svg": "xml",
}


# ---------------------------------------------------------------------------
# 工作区
# ---------------------------------------------------------------------------
def _norm(p):
    return os.path.normcase(os.path.normpath(os.path.realpath(os.path.abspath(p))))


def load_workspaces():
    d = core._read_json(core.WORKSPACES_PATH, {}) or {}
    return d.get("workspaces") or []


def save_workspaces(ws):
    core._write_json(core.WORKSPACES_PATH,
                     {"version": 1, "workspaces": ws, "updated": time.time()})


def add_workspace(path):
    """添加工作区；返回 (ok, msg, workspaces)"""
    if not path or not os.path.isdir(path):
        return False, "目录不存在", load_workspaces()
    ws = load_workspaces()
    n = _norm(path)
    if any(_norm(w["path"]) == n for w in ws):
        return False, "该目录已在工作区列表中", ws
    item = {"path": os.path.abspath(path), "name": os.path.basename(
        os.path.abspath(path).rstrip("\\/")) or os.path.abspath(path),
        "added": time.time()}
    ws.append(item)
    save_workspaces(ws)
    return True, "已添加", ws


def remove_workspace(path):
    ws = load_workspaces()
    n = _norm(path)
    ws = [w for w in ws if _norm(w["path"]) != n]
    save_workspaces(ws)
    return ws


def resolve_in_workspace(path):
    """把任意路径约束到工作区内。
    成功返回绝对路径；失败返回 None。"""
    if not path:
        return None
    try:
        rp = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    except Exception:
        return None
    nrp = _norm(rp)
    for w in load_workspaces():
        nw = _norm(w["path"])
        if nrp == nw or nrp.startswith(nw + os.sep):
            return rp
    return None


def default_roots():
    """界面默认展示的根：工作区 + 常用个人目录（只读提示用）"""
    roots = []
    for w in load_workspaces():
        roots.append({"path": w["path"], "name": w["name"], "kind": "workspace",
                      "added": w.get("added", 0)})
    return roots


# ---------------------------------------------------------------------------
# 目录树 / 列举
# ---------------------------------------------------------------------------
def _entry(path, with_stat=True):
    try:
        st = os.stat(path)
    except Exception:
        return None
    name = os.path.basename(path.rstrip("\\/")) or path
    ext = os.path.splitext(name)[1].lower()
    is_dir = os.path.isdir(path)
    e = {
        "name": name,
        "path": path,
        "is_dir": is_dir,
        "ext": ext if not is_dir else "",
        "size": 0 if is_dir else st.st_size,
        "mtime": int(st.st_mtime),
        "kind": "dir" if is_dir else _file_kind(ext),
        "lang": LANG_MAP.get(ext, ""),
    }
    if is_dir:
        try:
            e["has_child"] = any(True for _ in os.scandir(path))
        except Exception:
            e["has_child"] = False
    return e


def _file_kind(ext):
    if ext in TEXT_EXTS:
        return "text"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in BINARY_DOC_EXTS:
        return "binary"
    return "other"


def list_dir(path):
    """列举一层目录；path 必须落在工作区内"""
    rp = resolve_in_workspace(path)
    if not rp:
        return {"ok": False, "error": "路径不在工作区内（请先在左侧添加目录）"}
    if not os.path.isdir(rp):
        return {"ok": False, "error": "不是目录"}
    try:
        items = []
        for name in sorted(os.listdir(rp), key=str.lower):
            if name.lower() in SKIP_DIRS_L and os.path.isdir(os.path.join(rp, name)):
                continue
            e = _entry(os.path.join(rp, name))
            if e:
                items.append(e)
            if len(items) >= MAX_TREE_ENTRIES:
                break
        dirs = [e for e in items if e["is_dir"]]
        files = [e for e in items if not e["is_dir"]]
        items = dirs + files
    except PermissionError:
        return {"ok": False, "error": "没有访问权限"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    ws = load_workspaces()
    root = _nearest_root(rp, ws)
    return {"ok": True, "path": rp, "root": root, "items": items,
            "count": len(items)}


def _nearest_root(path, ws=None):
    ws = ws or load_workspaces()
    n = _norm(path)
    best = None
    for w in ws:
        nw = _norm(w["path"])
        if n == nw:
            return {"path": w["path"], "name": w["name"]}
        if n.startswith(nw + os.sep):
            if best is None or len(nw) > len(_norm(best["path"])):
                best = {"path": w["path"], "name": w["name"]}
    return best


# ---------------------------------------------------------------------------
# 读文件
# ---------------------------------------------------------------------------
def read_text(path, max_bytes=MAX_TEXT_BYTES):
    """读文本；自动探测编码（utf-8-sig → utf-8 → gbk → latin-1）"""
    rp = resolve_in_workspace(path)
    if not rp:
        return {"ok": False, "error": "路径不在工作区内"}
    if not os.path.isfile(rp):
        return {"ok": False, "error": "不是文件"}
    ext = os.path.splitext(rp)[1].lower()
    if ext in IMAGE_EXTS:
        return {"ok": False, "error": "这是图片，请用 /api/fs/image",
                "kind": "image"}
    size = os.path.getsize(rp)
    if size > max_bytes:
        return {"ok": False, "error": "文件过大（%.1f MB > %.0f MB），请用外部编辑器打开"
                % (size / 1048576, max_bytes / 1048576), "size": size}
    raw = b""
    try:
        with open(rp, "rb") as f:
            raw = f.read(max_bytes + 1)
    except PermissionError:
        return {"ok": False, "error": "没有读取权限"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    if len(raw) > max_bytes:
        return {"ok": False, "error": "文件过大，已中止", "size": size}
    truncated = size > max_bytes
    if b"\x00" in raw[:8192]:
        return {"ok": False, "error": "二进制文件（含 NUL 字节），请用外部编辑器打开",
                "size": size}
    text, enc = None, "utf-8"
    for cand in ("utf-8-sig", "utf-8"):
        try:
            text = raw.decode(cand)
            enc = cand
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        for cand in ("gbk", "big5", "latin-1"):
            try:
                text = raw.decode(cand)
                enc = cand
                break
            except UnicodeDecodeError:
                continue
    if text is None:
        return {"ok": False, "error": "无法解码（未知编码）", "size": size}
    lines = text.splitlines()
    e = _entry(rp)
    return {"ok": True, "path": rp, "name": e["name"], "kind": "text",
            "encoding": enc, "size": size, "lines": len(lines),
            "truncated": truncated, "lang": e["lang"], "content": text,
            "mtime": e["mtime"]}


def read_image_b64(path):
    """读图片为 base64（供 <img src> 用）"""
    import base64
    rp = resolve_in_workspace(path)
    if not rp:
        return {"ok": False, "error": "路径不在工作区内"}
    if not os.path.isfile(rp):
        return {"ok": False, "error": "不是文件"}
    ext = os.path.splitext(rp)[1].lower()
    if ext not in IMAGE_EXTS:
        return {"ok": False, "error": "不是支持的图片格式"}
    size = os.path.getsize(rp)
    if size > PREVIEW_IMAGE_MAX:
        return {"ok": False, "error": "图片过大（%.1f MB）" % (size / 1048576)}
    try:
        with open(rp, "rb") as f:
            b = f.read()
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
            ".svg": "image/svg+xml", ".ico": "image/x-icon"}.get(ext,
                                                                  "application/octet-stream")
    return {"ok": True, "path": rp, "name": os.path.basename(rp),
            "size": size, "mime": mime,
            "data": "data:%s;base64,%s" % (mime, base64.b64encode(b).decode("ascii"))}


def stat_path(path):
    rp = resolve_in_workspace(path)
    if not rp:
        return {"ok": False, "error": "路径不在工作区内"}
    e = _entry(rp)
    if not e:
        return {"ok": False, "error": "无法访问"}
    e["ok"] = True
    e["root"] = _nearest_root(rp)
    return e


# ---------------------------------------------------------------------------
# 写文件 / 新建 / 重命名 / 删除
# ---------------------------------------------------------------------------
def _backup(rp):
    """写前备份：同名 .bak，保留最近 3 份"""
    if not os.path.isfile(rp):
        return None
    try:
        with open(rp, "rb") as f:
            old = f.read()
    except Exception:
        return None
    ts = time.strftime("%Y%m%d_%H%M%S")
    bk = "%s.bak.%s" % (rp, ts)
    try:
        with open(bk, "wb") as f:
            f.write(old)
    except Exception:
        return None
    # 清理旧备份（保留最近 3 个）
    try:
        base = os.path.basename(rp) + ".bak."
        olds = sorted([os.path.join(os.path.dirname(rp), x)
                       for x in os.listdir(os.path.dirname(rp))
                       if x.startswith(base)])
        for p in olds[:-3]:
            os.remove(p)
    except Exception:
        pass
    return bk


def write_text(path, content, encoding="utf-8"):
    rp = resolve_in_workspace(path)
    if not rp:
        return {"ok": False, "error": "路径不在工作区内"}
    if os.path.isdir(rp):
        return {"ok": False, "error": "目标是目录"}
    bk = _backup(rp)
    try:
        with open(rp, "w", encoding=encoding, newline="") as f:
            f.write(content or "")
    except PermissionError:
        return {"ok": False, "error": "没有写入权限（文件可能被占用或只读）"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    e = _entry(rp)
    return {"ok": True, "path": rp, "backup": bk, "size": e["size"],
            "mtime": e["mtime"], "lines": e.get("size", 0)}


def create_file(path, content=""):
    rp = resolve_in_workspace(path)
    if not rp:
        return {"ok": False, "error": "路径不在工作区内"}
    if os.path.exists(rp):
        return {"ok": False, "error": "已存在同名文件"}
    try:
        with open(rp, "w", encoding="utf-8") as f:
            f.write(content or "")
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    return {"ok": True, "path": rp}


def mkdir(path):
    rp = resolve_in_workspace(path)
    if not rp:
        return {"ok": False, "error": "路径不在工作区内"}
    if os.path.exists(rp):
        return {"ok": False, "error": "已存在"}
    try:
        os.makedirs(rp)
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    return {"ok": True, "path": rp}


def rename(path, new_name):
    rp = resolve_in_workspace(path)
    if not rp:
        return {"ok": False, "error": "路径不在工作区内"}
    if not new_name or re.search(r'[\\/:*?"<>|]', new_name):
        return {"ok": False, "error": "新文件名含非法字符"}
    dst = os.path.join(os.path.dirname(rp), new_name)
    if not resolve_in_workspace(dst):
        return {"ok": False, "error": "目标不在工作区内"}
    if os.path.exists(dst):
        return {"ok": False, "error": "目标已存在"}
    try:
        os.rename(rp, dst)
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    return {"ok": True, "path": dst}


# --- 回收站（Windows SHFileOperationW）---
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


FO_DELETE = 3
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004


def _to_recycle_bin(paths):
    """多选删除走回收站。返回 (成功数, 错误信息)"""
    if not paths:
        return 0, "无文件"
    # pFrom 需以双 NUL 结尾
    buf = "\0".join(paths) + "\0\0"
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = buf
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    op.fAnyOperationsAborted = 0
    op.hNameMappings = None
    op.lpszProgressTitle = None
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        return 0, "SHFileOperation 返回 %s" % rc
    if op.fAnyOperationsAborted:
        return 0, "用户取消了操作"
    return len(paths), None


def trash(paths):
    ok = []
    bad = []
    for p in paths or []:
        rp = resolve_in_workspace(p)
        if not rp or not os.path.exists(rp):
            bad.append({"path": p, "error": "路径不在工作区或不存在"})
            continue
        ok.append(rp)
    if not ok:
        return {"ok": False, "error": "没有可删除的路径", "detail": bad}
    n, err = _to_recycle_bin(ok)
    if err:
        return {"ok": False, "error": err, "detail": bad}
    return {"ok": True, "deleted": n, "detail": bad}


def hard_delete(paths):
    """真删除（仅当回收站不可用时由前端二次确认后调用）"""
    done, errs = [], []
    for p in paths or []:
        rp = resolve_in_workspace(p)
        if not rp or not os.path.exists(rp):
            errs.append({"path": p, "error": "路径不在工作区或不存在"})
            continue
        try:
            if os.path.isdir(rp):
                shutil.rmtree(rp)
            else:
                os.remove(rp)
            done.append(rp)
        except Exception as ex:
            errs.append({"path": p, "error": str(ex)})
    return {"ok": not errs, "deleted": len(done), "detail": errs}


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
def search_by_name(keyword, root=None, limit=200):
    kw = (keyword or "").lower()
    if not kw:
        return {"ok": False, "error": "关键词为空"}
    roots = [root] if root else [w["path"] for w in load_workspaces()]
    if root:
        rp = resolve_in_workspace(root)
        if not rp:
            return {"ok": False, "error": "根路径不在工作区内"}
        roots = [rp]
    out = []
    for r in roots:
        if len(out) >= limit:
            break
        for dirpath, dirnames, filenames in os.walk(r):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS_L]
            for name in dirnames + filenames:
                if kw in name.lower():
                    full = os.path.join(dirpath, name)
                    e = _entry(full)
                    if e:
                        out.append(e)
                    if len(out) >= limit:
                        break
            if len(out) >= limit:
                break
    return {"ok": True, "items": out, "count": len(out)}


def search_content(keyword, root=None, limit=100, max_files=MAX_SEARCH_FILES):
    kw = keyword or ""
    if not kw:
        return {"ok": False, "error": "关键词为空"}
    roots = [root] if root else [w["path"] for w in load_workspaces()]
    if root:
        rp = resolve_in_workspace(root)
        if not rp:
            return {"ok": False, "error": "根路径不在工作区内"}
        roots = [rp]
    out = []
    scanned = 0
    for r in roots:
        for dirpath, dirnames, filenames in os.walk(r):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS_L]
            for name in filenames:
                if scanned >= max_files or len(out) >= limit:
                    break
                ext = os.path.splitext(name)[1].lower()
                if ext not in TEXT_EXTS and ext not in CODE_EXTS:
                    continue
                full = os.path.join(dirpath, name)
                scanned += 1
                try:
                    if os.path.getsize(full) > 1024 * 1024:
                        continue
                    with open(full, "rb") as f:
                        raw = f.read()
                    if b"\x00" in raw[:4096]:
                        continue
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        try:
                            text = raw.decode("gbk")
                        except UnicodeDecodeError:
                            continue
                except Exception:
                    continue
                lines = text.splitlines()
                hits = []
                for i, ln in enumerate(lines):
                    if kw.lower() in ln.lower():
                        hits.append({"line": i + 1,
                                     "text": ln.strip()[:200]})
                        if len(hits) >= 5:
                            break
                if hits:
                    out.append({"path": full, "name": name, "hits": hits,
                                "total_hits": sum(1 for l in lines
                                                  if kw.lower() in l.lower())})
            if scanned >= max_files or len(out) >= limit:
                break
        if scanned >= max_files or len(out) >= limit:
            break
    return {"ok": True, "items": out, "count": len(out), "scanned": scanned}


# ---------------------------------------------------------------------------
# 引用上下文构建（把文件内容拼进 prompt）
# ---------------------------------------------------------------------------
def build_refs(refs):
    """refs: [{path, name}] → 返回 (上下文文本, 明细列表)"""
    parts, detail = [], []
    for r in refs or []:
        p = r.get("path")
        if not p:
            continue
        ext = os.path.splitext(p)[1].lower()
        if ext in IMAGE_EXTS:
            detail.append({"path": p, "name": r.get("name") or
                           os.path.basename(p), "kind": "image"})
            continue
        res = read_text(p, max_bytes=200 * 1024)
        if not res.get("ok"):
            detail.append({"path": p, "name": r.get("name") or
                           os.path.basename(p), "kind": "error",
                           "error": res.get("error")})
            continue
        content = res["content"]
        if res.get("truncated") or len(content) > 120000:
            content = content[:120000] + "\n...（已截断）"
        parts.append("===== 文件：%s（%d 行，%s）=====\n%s"
                     % (res["path"], res["lines"], res["encoding"], content))
        detail.append({"path": p, "name": res["name"], "kind": "text",
                       "lines": res["lines"], "chars": len(res["content"])})
    return "\n\n".join(parts), detail
