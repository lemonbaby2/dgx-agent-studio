# -*- coding: utf-8 -*-
"""服务端 API 冒烟测试：不起窗口，直接压接口"""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from server import app as srv   # noqa: E402

httpd, port, url = srv.run_server()
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.6)

BASE = "http://127.0.0.1:%d" % port
OK, FAIL = [], []


def chk(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("   " + str(detail) if detail else ""))


def get(p):
    try:
        with urllib.request.urlopen(BASE + p, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return 0, {"error": str(e)}


def post(p, body):
    try:
        req = urllib.request.Request(BASE + p, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


print("=" * 62)
print("DGX Agent Studio · API 冒烟测试  (%s)" % BASE)
print("=" * 62)

# 1. 静态首页
st, txt = get("/")
raw = urllib.request.urlopen(BASE + "/", timeout=5).read().decode("utf-8", "ignore")
chk("GET / 返回前端页面", "DGX Agent Studio" in raw, len(raw))

# 2. 静态资源
for f in ("/app.css", "/app.js", "/panel.js"):
    try:
        b = urllib.request.urlopen(BASE + f, timeout=5).read()
        chk("GET %s" % f, len(b) > 500, "%d bytes" % len(b))
    except Exception as e:
        chk("GET %s" % f, False, e)

# 3. 配置
st, d = get("/api/config")
chk("GET /api/config", d.get("ok") and d.get("config"), d.get("ver"))

# 4. 模型
st, d = get("/api/models")
models = d.get("models") or []
chk("GET /api/models（DGX 在线）", bool(models),
    "%d 个：%s" % (len(models), ",".join(m["name"] for m in models[:5])))

# 5. 工作区
st, d = get("/api/fs/roots")
chk("GET /api/fs/roots", d.get("ok"), "workspaces=%d" % len(d.get("workspaces") or []))

# 6. 添加工作区
ws = os.path.join(HERE, "web")
st, d = post("/api/ws/add", {"path": ws})
chk("POST /api/ws/add", d.get("ok"), d.get("msg") or d.get("error"))

# 7. 列目录
st, d = get("/api/fs/list?path=" + urllib.request.quote(ws))
chk("GET /api/fs/list", d.get("ok") and d.get("items") is not None,
    "%d 项" % len(d.get("items") or []))

# 8. 读文件
idx = os.path.join(ws, "index.html")
st, d = get("/api/fs/read?path=" + urllib.request.quote(idx))
chk("GET /api/fs/read", d.get("ok") and "DGX Agent Studio" in (d.get("content") or ""),
    "编码=%s 行数=%s" % (d.get("encoding"), d.get("lines")))

# 9. 读图片（svg）
st, d = get("/api/fs/image?path=" + urllib.request.quote(os.path.join(ws, "index.html")))
chk("GET /api/fs/image 拒非图片", not d.get("ok"), d.get("error"))

# 10. 搜索
st, d = post("/api/fs/search", {"keyword": "app", "root": ws, "limit": 20})
chk("POST /api/fs/search", d.get("ok"), "%d 个匹配" % d.get("count", 0))

# 11. 路径防穿越
st, d = get("/api/fs/read?path=" + urllib.request.quote("C:\\Windows\\system.ini"))
chk("路径防穿越（工作区外拒绝）", not d.get("ok"), d.get("error"))

# 12. 浏览盘符
st, d = post("/api/ws/browse", {})
chk("POST /api/ws/browse", d.get("ok"), "%d 个盘符" % len(d.get("items") or []))

# 13. 会话
st, d = get("/api/sessions")
chk("GET /api/sessions", d.get("ok") and d.get("sessions"), "共 %d 个" % len(d.get("sessions") or []))
sid = d["sessions"][0]["id"]

st, d = post("/api/session/rename", {"id": sid, "title": "冒烟测试会话"})
chk("POST /api/session/rename", d.get("ok"))

# 14. SSE 对话
print("\n  --- SSE 对话测试（流式）---")
t0 = time.time()
try:
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({"id": sid, "text": "只回答两个字：收到",
                         "show_think": True}).encode(),
        headers={"Content-Type": "application/json"})
    got_delta = got_end = False
    acc = ""
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data: "):
                continue
            obj = json.loads(line[6:])
            if obj.get("type") == "delta":
                got_delta = True
                acc += obj.get("text", "")
            if obj.get("type") == "end":
                got_end = True
    chk("POST /api/chat（SSE 流式）", got_delta and got_end,
        "%.1fs，回复 %d 字：%s" % (time.time() - t0, len(acc), acc[:40].replace("\n", " ")))
except Exception as e:
    chk("POST /api/chat（SSE 流式）", False, e)

# 15. 命令执行 + 危险命令拦截
st, d = post("/api/exec/check", {"command": "rm -rf /"})
chk("危险命令拦截", d.get("dangerous") is True)
st, d = post("/api/exec/check", {"command": "echo hello"})
chk("安全命令放行", d.get("dangerous") is False)
st, d = post("/api/exec", {"command": "echo smoke_ok"})
chk("POST /api/exec", d.get("ok") and "smoke_ok" in d.get("output", ""),
    repr(d.get("output"))[:60])

# 16. 清理测试工作区
post("/api/ws/remove", {"path": ws})

print("\n" + "=" * 62)
print("结果：%d 通过 / %d 失败" % (len(OK), len(FAIL)))
if FAIL:
    print("失败项：", FAIL)
print("=" * 62)
httpd.shutdown()
sys.exit(1 if FAIL else 0)
