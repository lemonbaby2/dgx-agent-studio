# -*- coding: utf-8 -*-
"""
DGX Agent Studio · v8 具身仿真桥（embodied_api）
================================================================
职责：把「自然语言任务」接到 Isaac Sim 仿真 / ROS2 上。

设计原则（踩过的坑）：
  · 零第三方依赖（DGX 上是 aarch64 py3.12，装包成本高）
  · ROS2 命令必须走 `bash -lc`，且显式 source setup.bash —— 否则
    `ros2: command not found`，而 subprocess 的 returncode 仍是 127 不报错
  · Isaac Sim 在 .217 上只留下 ~/docker/isaac-sim 数据卷，镜像已被清理；
    真跑要重新拉 nvcr.io/nvidia/isaac-sim（12~20GB），所以「启动」是
    异步后台任务 + 状态轮询，绝不阻塞 HTTP 请求
  · 禁止任意 shell：ROS2 只允许白名单动词（pub/echo/服务调用），
    docker 只允许 isaac 相关容器名
  · 截图要 DISPLAY=:1（DGX 的 GNOME 在 tty2/Xorg :1，不是 :0）

对外 API：
    status()                     体检：ros2 / isaac / display / gpu
    isaac_images()               本地已有 isaac 镜像
    isaac_pull(tag)              后台拉镜像
    isaac_start(gui=True)        后台起 isaac（docker）
    isaac_stop()                 停
    isaac_logs(n)                取日志尾部
    ros_topics()                 topic 列表
    ros_topic_info(topic)        topic 类型与发布者
    ros_publish(topic, msg_type, payload)
    ros_echo(topic, n)
    ros_nodes()
    screenshot()                 屏幕截图 base64
    vla_plan(text, ...)          自然语言 → 可执行步骤（LLM 规划）
    vla_run(steps)               顺序执行步骤，返回逐步结果
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid

# ---------------------------------------------------------------------------
# 环境常量
# ---------------------------------------------------------------------------
ROS_DISTRO = "jazzy"
ROS_SETUP = "/opt/ros/%s/setup.bash" % ROS_DISTRO
WS_SETUPS = [
    os.path.expanduser("~/robot_ws/install/setup.bash"),
    os.path.expanduser("~/ros2_ws/install/setup.bash"),
]
DISPLAY = os.environ.get("DGX_DISPLAY") or ":1"

ISAAC_DATA = os.path.expanduser("~/docker/isaac-sim")
ISAAC_IMAGE_CANDIDATES = [
    "nvcr.io/nvidia/isaac-sim:6.0.1",
    "nvcr.io/nvidia/isaac-sim:5.1.0",
    "nvcr.io/nvidia/isaac-sim:5.0.0",
]
ISAAC_CONTAINER = "dgx-isaac-sim"

# pip 版（PyPI 有 aarch64 wheel，不需要 NGC API key —— 首选路径）
ISAAC_VENV = os.path.expanduser("~/isaacsim-venv")
ISAAC_VENV_PY = os.path.join(ISAAC_VENV, "bin", "python")
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISAAC_LAUNCHER = os.path.join(_APP_ROOT, "tools", "isaac_launch.py")

# ros2 只允许这些动词
ROS_VERBS = {"list", "info", "show", "echo", "pub", "node", "topic", "service",
             "param", "interface", "pkg", "run", "launch"}
# docker 只允许这些动作（且只针对 ISAAC_CONTAINER / isaac 镜像）
DOCKER_VERBS = {"ps", "images", "logs", "inspect", "start", "stop", "pull", "run"}

_JOB_LOCK = threading.RLock()
_JOBS = {}          # job_id -> {kind, proc, log[], started, status}


# ---------------------------------------------------------------------------
# 命令执行
# ---------------------------------------------------------------------------
# pip 版 Isaac Sim 6.2 在 aarch64 上必须的三个环境：
#   LD_PRELOAD            —— OpenMP 库加载顺序（不设会在 print 警告后直接 exit，
#                            表现为「import 无任何输出、也没有异常」，极难查）
#   OMNI_KIT_ACCEPT_EULA  —— 不设会卡在 "Do you accept the EULA? (Yes/No):" 等 stdin，
#                            systemd 下永远等不到，直接挂死
#   PRIVACY_CONSENT       —— 遥测同意，同理
ISAAC_ENV = {
    "LD_PRELOAD": "/lib/aarch64-linux-gnu/libgomp.so.1",
    "OMNI_KIT_ACCEPT_EULA": "YES",
    "ACCEPT_EULA": "Y",
    "PRIVACY_CONSENT": "Y",
}


def _clean_env(extra=None):
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["DISPLAY"] = DISPLAY
    env["ROS_DOMAIN_ID"] = env.get("ROS_DOMAIN_ID") or "0"
    if extra:
        env.update(extra)
    return env


def _ros_bash(cmd, timeout=30, quiet_stderr=True):
    """在 source 过 ROS2 的登录 shell 里执行命令。
    返回 (code, stdout+'\\n'+stderr)"""
    src = "source %s >/dev/null 2>&1" % ROS_SETUP
    for ws in WS_SETUPS:
        if os.path.isfile(ws):
            src += " && source %s >/dev/null 2>&1" % ws
    full = "%s && %s" % (src, cmd)
    try:
        p = subprocess.run(["bash", "-lc", full], capture_output=True,
                           text=True, timeout=timeout, env=_clean_env(),
                           encoding="utf-8", errors="ignore")
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if err and not quiet_stderr:
            out = (out + "\n[stderr]\n" + err).strip()
        elif err and not out:
            out = err
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return -1, "超时（%ss）" % timeout
    except Exception as e:
        return -1, "执行失败：%s" % e


def _sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout, env=_clean_env(),
                           encoding="utf-8", errors="ignore")
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return -1, "超时（%ss）" % timeout
    except Exception as e:
        return -1, "执行失败：%s" % e


def _guard_ros(verb):
    return verb in ROS_VERBS


# ---------------------------------------------------------------------------
# 体检
# ---------------------------------------------------------------------------
def _gpu_info():
    code, out = _sh(["nvidia-smi",
                     "--query-gpu=name,memory.used,memory.total,"
                     "utilization.gpu,power.draw", "--format=csv,noheader"], 8)
    if code != 0 or not out:
        return {"ok": False}
    parts = [x.strip() for x in out.splitlines()[0].split(",")]
    return {"ok": True, "name": parts[0] if parts else "",
            "mem_used": parts[1] if len(parts) > 1 else "",
            "mem_total": parts[2] if len(parts) > 2 else "",
            "util": parts[3] if len(parts) > 3 else "",
            "power": parts[4] if len(parts) > 4 else ""}


def isaac_pip_ready():
    """pip 版三级检测：
        L1 = import isaacsim                       → kernel + torch 就位
        L2 = from isaacsim import SimulationApp     → API 存在
        L3 = extscache 已装（isaacsim/exts 有内容） → 真能起 Kit 仿真
    只报 L1/L2 会误导：缺 extscache 时能 import 但一启动就报
    "Extension not found: isaacsim.simulation_app"。
    """
    if not os.path.isfile(ISAAC_VENV_PY):
        return {"ready": False, "level": 0, "reason": "无 ~/isaacsim-venv"}
    probe = ("import sys, os, glob\n"
             "try:\n"
             "    import isaacsim\n"
             "except Exception as e:\n"
             "    print('L1_FAIL', e); sys.exit(1)\n"
             "print('L1_OK')\n"
             "try:\n"
             "    from isaacsim import SimulationApp\n"
             "    print('L2_OK')\n"
             "except Exception as e:\n"
             "    print('L2_FAIL', e)\n"
             "sp = os.path.dirname(isaacsim.__file__)\n"
             "exts = os.path.join(sp, 'exts')\n"
             "n = 0\n"
             "if os.path.isdir(exts):\n"
             "    n = len([d for d in os.listdir(exts)\n"
             "             if os.path.isdir(os.path.join(exts, d))])\n"
             "print('EXTS', n)\n"
             "want = os.path.join(exts, 'isaacsim.simulation_app')\n"
             "print('SIMAPP_EXT', 1 if os.path.isdir(want) else 0)\n")
    try:
        p = subprocess.run([ISAAC_VENV_PY, "-c", probe],
                           capture_output=True, text=True, timeout=240,
                           env=_clean_env(ISAAC_ENV),
                           encoding="utf-8", errors="ignore")
        out = (p.stdout or "") + (p.stderr or "")
        l1 = "L1_OK" in out
        l2 = "L2_OK" in out
        mn = re.search(r"EXTS (\d+)", out)
        n_ext = int(mn.group(1)) if mn else 0
        has_simapp_ext = bool(re.search(r"SIMAPP_EXT 1", out))
        level = 0
        if l1:
            level = 1
        if l2:
            level = 2
        if has_simapp_ext and n_ext >= 3:
            level = 3
        reason = ""
        if not l1:
            m = re.search(r"L1_FAIL (.*)", out)
            reason = m.group(1)[:200] if m else out[-200:]
        elif level < 3:
            m = re.search(r"L2_FAIL (.*)", out)
            reason = ("缺 Kit 运行时扩展（isaacsim/exts 下只有 %d 个，"
                      "需装 extscache）%s"
                      % (n_ext, (m.group(1)[:120] if m else "")))
        return {"ready": level >= 3, "level": level, "l1": l1, "l2": l2,
                "exts": n_ext, "python": ISAAC_VENV_PY, "reason": reason,
                "hint": {0: "未安装",
                         1: "torch/kernel 就位，缺 app/gui/extscache 子包",
                         2: "API 就位，缺 extscache 运行时（起不来 Kit）",
                         3: "可启动"}.get(level, "")}
    except Exception as e:
        return {"ready": False, "level": 0, "reason": str(e)[:200]}


def _isaac_state():
    """Isaac 运行状态：pip 进程 + docker 容器 双判据"""
    code, out = _sh(["docker", "ps", "-a", "--filter",
                     "name=%s" % ISAAC_CONTAINER,
                     "--format", "{{.Names}}|{{.Status}}|{{.Image}}"], 10)
    container = None
    if code == 0 and out.strip():
        c = out.strip().splitlines()[0].split("|")
        container = {"name": c[0], "status": c[1] if len(c) > 1 else "",
                     "image": c[2] if len(c) > 2 else "",
                     "running": "Up" in (c[1] if len(c) > 1 else "")}
    code2, ps = _sh(["bash", "-lc",
                     "ps -eo pid,etime,cmd | grep -iE "
                     "'[i]saac_launch|[i]saac-sim|[k]it\\.[a-z]' | head -5"], 8)
    procs = [l.strip() for l in (ps or "").splitlines() if l.strip()]
    pip_run = any("isaac_launch" in p for p in procs)
    return {"container": container,
            "pip_mode": pip_run,
            "running": bool(container and container.get("running")) or bool(procs),
            "processes": procs[:5]}


def status():
    # 注意：ros2 没有 --version 子命令（踩过），判断可用性只能看
    # command -v ros2 + source 后的 ROS_DISTRO
    code, probe = _ros_bash(
        "command -v ros2 >/dev/null 2>&1 && echo READY || echo NO_ROS2; "
        "echo DISTRO=${ROS_DISTRO:-none}", 20)
    ros_ok = "READY" in (probe or "")
    distro = "none"
    m = re.search(r"DISTRO=(\S+)", probe or "")
    if m:
        distro = m.group(1)
    code2, disp = _sh(["bash", "-lc",
                       "DISPLAY=%s xdpyinfo >/dev/null 2>&1 && echo OK || echo NO"
                       % DISPLAY], 6)
    isaac = _isaac_state()
    code3, imgs = _sh(["docker", "images", "--format",
                       "{{.Repository}}:{{.Tag}}|{{.Size}}"], 10)
    isaac_imgs = [l for l in (imgs or "").splitlines()
                  if "isaac" in l.lower()]
    return {
        "ok": True,
        "ros2": {"ready": ros_ok, "distro": distro if distro != "none" else ROS_DISTRO,
                 "ros2_path": "found" if ros_ok else "not found"},
        "display": {"ready": disp.strip() == "OK", "value": DISPLAY},
        "isaac": isaac,
        "isaac_pip": (isaac_pip_ready() if os.path.isfile(ISAAC_VENV_PY)
                      else {"ready": False, "reason": "未安装 ~/isaacsim-venv"}),
        "isaac_images": isaac_imgs,
        "isaac_data_dir": ISAAC_DATA,
        "isaac_data_exists": os.path.isdir(ISAAC_DATA),
        "gpu": _gpu_info(),
        "jobs": list_jobs(),
    }


# ---------------------------------------------------------------------------
# 后台任务（拉镜像 / 起容器）
# ---------------------------------------------------------------------------
def _spawn_job(kind, argv, env_extra=None):
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "kind": kind, "cmd": " ".join(argv), "log": [],
           "started": time.time(), "status": "running", "returncode": None}
    with _JOB_LOCK:
        _JOBS[jid] = job

    def _run():
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 env=_clean_env(env_extra), encoding="utf-8",
                                 errors="ignore", bufsize=1)
            job["proc"] = p
            for line in p:
                line = (line or "").rstrip()
                if not line:
                    continue
                job["log"].append(line)
                if len(job["log"]) > 400:
                    job["log"] = job["log"][-400:]
            p.wait()
            job["returncode"] = p.returncode
            job["status"] = "done" if p.returncode == 0 else "failed"
        except Exception as e:
            job["log"].append("ERR %s" % e)
            job["status"] = "failed"
        finally:
            job["finished"] = time.time()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jid


def list_jobs():
    with _JOB_LOCK:
        out = []
        for j in _JOBS.values():
            d = {k: v for k, v in j.items() if k not in ("proc", "log")}
            d["tail"] = j["log"][-15:]
            out.append(d)
        return sorted(out, key=lambda x: x.get("started", 0), reverse=True)


def job(jid):
    with _JOB_LOCK:
        j = _JOBS.get(jid)
        if not j:
            return {"ok": False, "error": "任务不存在"}
        d = {k: v for k, v in j.items() if k != "proc"}
        return {"ok": True, "job": d}


def isaac_pull(tag=None):
    """后台拉 Isaac Sim 镜像。tag 为空则按候选顺序探测第一个可用的。"""
    tag = tag or ISAAC_IMAGE_CANDIDATES[0]
    if not re.match(r"^nvcr\.io/nvidia/isaac-sim:[\w.\-]+$", tag):
        return {"ok": False, "error": "只允许 nvcr.io/nvidia/isaac-sim:<tag>"}
    jid = _spawn_job("isaac-pull", ["docker", "pull", tag])
    return {"ok": True, "job_id": jid, "image": tag,
            "note": "12~20GB，走代理可能较慢；用 /api/embodied/job 轮询"}


def isaac_start(image=None, gui=True, usd=None, seconds=0, mode="auto"):
    """启动 Isaac Sim。

    mode:
      auto  —— 有 pip 版就用 pip（快、不必拉 20GB 镜像），否则 docker
      pip   —— 强制 pip 版
      docker—— 强制 docker 版
    """
    if mode in ("auto", "pip"):
        pip = isaac_pip_ready()
        if pip.get("ready"):
            if not os.path.isfile(ISAAC_LAUNCHER):
                return {"ok": False, "error": "缺 tools/isaac_launch.py"}
            argv = [ISAAC_VENV_PY, ISAAC_LAUNCHER]
            if not gui:
                argv.append("--headless")
            if usd:
                argv += ["--usd", usd]
            if seconds:
                argv += ["--seconds", str(int(seconds))]
            jid = _spawn_job("isaac-start-pip", argv, env_extra=ISAAC_ENV)
            return {"ok": True, "job_id": jid, "mode": "pip",
                    "python": ISAAC_VENV_PY, "gui": gui,
                    "note": "pip 版首次启动要加载 Kit，1-5 分钟"}
        if mode == "pip":
            return {"ok": False, "error": "pip 版不可用：" + str(pip.get("reason"))}

    # ---- docker 路径 ----
    image = image or ISAAC_IMAGE_CANDIDATES[0]
    if not re.match(r"^nvcr\.io/nvidia/isaac-sim:[\w.\-]+$", image):
        return {"ok": False, "error": "只允许 nvcr.io/nvidia/isaac-sim:<tag>"}
    code, out = _sh(["docker", "image", "inspect", image], 10)
    if code != 0:
        return {"ok": False,
                "error": "本地无镜像 %s，且 pip 版不可用" % image,
                "hint": ["pip install isaacsim==6.2.0.0（推荐，见部署文档）",
                         "或 docker pull 需 NGC API key（nvcr.io 匿名 401）"]}
    _sh(["docker", "rm", "-f", ISAAC_CONTAINER], 20)
    argv = ["docker", "run", "--name", ISAAC_CONTAINER, "--gpus", "all",
            "-e", "ACCEPT_EULA=Y",
            "-e", "PRIVACY_CONSENT=Y",
            "-e", "DISPLAY=%s" % DISPLAY,
            "-v", "/tmp/.X11-unix:/tmp/.X11-unix:rw",
            "-v", "%s:/root/.nvidia-omniverse" % ISAAC_DATA,
            "-v", os.path.expanduser("~/robot_ws") + ":/workspace/robot_ws",
            "--network", "host", "-it", "-d", image]
    if not gui:
        argv.insert(-2, "-e")
        argv.insert(-1, "ISAAC_HEADLESS=1")
    jid = _spawn_job("isaac-start", argv)
    return {"ok": True, "job_id": jid, "mode": "docker", "image": image, "gui": gui}


def isaac_stop():
    code, out = _sh(["docker", "stop", ISAAC_CONTAINER], 40)
    _sh(["docker", "rm", "-f", ISAAC_CONTAINER], 20)
    return {"ok": code == 0, "output": out}


def isaac_logs(n=60):
    """取仿真日志。docker 版读容器，pip 版读最近一次 pip 启动任务的输出。"""
    n = int(n)
    code, out = _sh(["docker", "logs", "--tail", str(n), ISAAC_CONTAINER], 20)
    if code == 0 and out.strip():
        return {"ok": True, "source": "docker", "output": out}
    # pip 模式：从后台任务里挑最近的 isaac-start-pip
    with _JOB_LOCK:
        pips = [j for j in _JOBS.values() if j.get("kind") == "isaac-start-pip"]
    if pips:
        j = sorted(pips, key=lambda x: x.get("started", 0))[-1]
        tail = "\n".join(j["log"][-n:]) or "(该任务还没有输出)"
        return {"ok": True, "source": "pip-job", "job": j["id"],
                "status": j.get("status"), "output": tail}
    return {"ok": False, "output":
            "没有 Isaac Sim 在运行。pip 版请先在「具身仿真」里点「启动仿真」；"
            "docker 版需要镜像（nvcr.io 需 NGC key，推荐走 pip）。"}


# ---------------------------------------------------------------------------
# ROS2 桥
# ---------------------------------------------------------------------------
def ros_topics():
    code, out = _ros_bash("timeout 8 ros2 topic list 2>/dev/null", 20)
    items = [l.strip() for l in (out or "").splitlines()
             if l.strip().startswith("/")]
    return {"ok": code == 0, "count": len(items), "topics": items}


def ros_nodes():
    code, out = _ros_bash("timeout 8 ros2 node list 2>/dev/null", 20)
    items = [l.strip() for l in (out or "").splitlines() if l.strip().startswith("/")]
    return {"ok": code == 0, "nodes": items}


def ros_topic_info(topic):
    if not topic or not re.match(r"^/[\w/]+$", topic):
        return {"ok": False, "error": "topic 名非法"}
    code, out = _ros_bash("timeout 6 ros2 topic info %s 2>/dev/null"
                          % shlex.quote(topic), 15)
    code2, typ = _ros_bash("timeout 6 ros2 topic type %s 2>/dev/null"
                           % shlex.quote(topic), 15)
    code3, hz = _ros_bash("timeout 5 ros2 topic hz %s 2>/dev/null"
                          % shlex.quote(topic), 12)
    return {"ok": code == 0, "topic": topic, "type": (typ or "").strip(),
            "info": out, "hz": (hz or "").strip()[:300]}


def ros_echo(topic, n=1):
    if not topic or not re.match(r"^/[\w/]+$", topic):
        return {"ok": False, "error": "topic 名非法"}
    n = max(1, min(int(n or 1), 20))
    code, out = _ros_bash(
        "timeout %d ros2 topic echo --once %s 2>/dev/null"
        % (6 + 3 * n, shlex.quote(topic)) if n == 1 else
        "timeout %d ros2 topic echo %s 2>/dev/null | head -80"
        % (6 + 3 * n, shlex.quote(topic)), 12 + 3 * n)
    return {"ok": code == 0, "topic": topic, "output": out[:4000]}


def ros_publish(topic, msg_type, payload):
    """发布一条消息。payload 为 dict 时转成 YAML 风格。
    这是「安全点」——只允许 topic pub，不允许 ros2 run 任意可执行。"""
    if not topic or not re.match(r"^/[\w/]+$", topic):
        return {"ok": False, "error": "topic 名非法"}
    if not msg_type or not re.match(r"^[\w/]+$", msg_type):
        return {"ok": False, "error": "消息类型非法（如 std_msgs/msg/String）"}
    if isinstance(payload, dict):
        body = "{" + ", ".join("%s: %s" % (k, json.dumps(v, ensure_ascii=False))
                               for k, v in payload.items()) + "}"
    else:
        body = str(payload)
    code, out = _ros_bash(
        "timeout 8 ros2 topic pub --once %s %s %s 2>&1"
        % (shlex.quote(topic), shlex.quote(msg_type), shlex.quote(body)), 20)
    return {"ok": code == 0, "topic": topic, "type": msg_type,
            "payload": body, "output": out[:2000]}


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------
def screenshot():
    """抓 DGX 桌面。GNOME 在 :1（tty2），不是 :0 —— 踩过。"""
    tmp = "/tmp/_studio_shot_%d.png" % int(time.time() * 1000)
    code, out = _sh(["bash", "-lc",
                     "DISPLAY=%s gnome-screenshot -f %s 2>&1" % (DISPLAY, tmp)], 15)
    if not os.path.isfile(tmp):
        code2, out2 = _sh(["bash", "-lc",
                           "DISPLAY=%s import -window root %s 2>&1"
                           % (DISPLAY, tmp)], 15)
        if not os.path.isfile(tmp):
            return {"ok": False, "error": "截图失败：%s %s" % (out, out2)}
    try:
        with open(tmp, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        os.remove(tmp)
        return {"ok": True, "image": "data:image/png;base64," + b64,
                "bytes": len(b64)}
    except Exception as e:
        return {"ok": False, "error": "读取截图失败：%s" % e}


# ---------------------------------------------------------------------------
# VLA：自然语言 → 可执行步骤
# ---------------------------------------------------------------------------
VLA_SYSTEM = """你是运行在 DGX Spark 上的具身智能体（VLA 规划器），
控制 Isaac Sim 里的机械臂仿真。你能执行的动作**只有**下面这几种：

1. ros_topics                     列出当前 ROS2 topic
2. ros_echo      {"topic": "/xxx"} 读一条消息
3. ros_pub       {"topic": "/xxx", "type": "std_msgs/msg/String", "payload": {...}}
4. screenshot                     抓一帧仿真画面
5. isaac_logs    {"n": 40}        看仿真日志
6. wait          {"seconds": 2}   等待

输出必须是**严格 JSON 数组**，每项形如：
  {"action": "ros_topics", "why": "先确认有哪些可控话题"}

规则：
· 最多 6 步；不许输出上面 6 种之外的 action
· 不确定话题名时，第 1 步必须是 ros_topics，不要瞎猜
· 每步 why 用中文一句话说明目的
· 只输出 JSON 数组本身，不要解释、不要 ```json 包裹
"""


def vla_plan(text, base=None, model=None, chat_stream=None):
    """调 LLM 生成步骤计划。chat_stream 由 app.py 注入（复用 core.chat_stream）"""
    if not (text or "").strip():
        return {"ok": False, "error": "任务描述为空"}
    if not chat_stream:
        return {"ok": False, "error": "未注入 LLM 调用器"}
    base = base or "http://127.0.0.1:11434"
    model = model or "deepseek-r1:70b"
    # 先给模型喂一次真实 topic 列表，避免瞎猜
    tps = ros_topics()
    ctx = ("当前 ROS2 topic 列表：\n" + "\n".join(tps.get("topics") or [])
           if tps.get("topics") else "当前没有检测到 ROS2 topic（仿真可能未启动）")
    msgs = [{"role": "system", "content": VLA_SYSTEM},
            {"role": "user", "content": "%s\n\n任务：%s" % (ctx, text)}]
    try:
        # deepseek-r1:70b 空闲时约 3-5 分钟；机器在装包/训练时会被拖到 10 分钟以上，
        # 所以给到 900s，宁可慢也别误判失败
        ans, _ = chat_stream(base, model, msgs, temperature=0.2, timeout=900)
    except Exception as e:
        return {"ok": False, "error": "规划失败：%s" % e,
                "hint": "若提示 timed out，多半是 DGX 上有别的重活在抢 CPU/IO"}
    steps = _extract_json_array(ans)
    if steps is None:
        return {"ok": False, "error": "模型输出不是合法 JSON 数组",
                "raw": (ans or "")[:1500]}
    # 过滤非法 action
    clean = []
    for s in steps[:6]:
        if isinstance(s, dict) and s.get("action") in (
                "ros_topics", "ros_echo", "ros_pub", "screenshot",
                "isaac_logs", "wait"):
            clean.append(s)
    return {"ok": True, "task": text, "steps": clean, "topics_seen": len(tps.get("topics") or [])}


def _extract_json_array(s):
    s = (s or "").strip()
    if not s:
        return None
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\[.*\]", s, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else None
    except Exception:
        return None


def vla_run(steps, stop_on_error=True):
    """顺序执行步骤，逐步返回结果（可视化 + 可审计）"""
    if not isinstance(steps, list):
        return {"ok": False, "error": "steps 必须是数组"}
    results = []
    for i, s in enumerate(steps[:6], 1):
        act = (s or {}).get("action")
        r = {"step": i, "action": act, "why": (s or {}).get("why") or ""}
        t0 = time.time()
        try:
            if act == "ros_topics":
                r["result"] = ros_topics()
            elif act == "ros_echo":
                r["result"] = ros_echo(s.get("topic"), s.get("n") or 1)
            elif act == "ros_pub":
                r["result"] = ros_publish(s.get("topic"), s.get("type"),
                                          s.get("payload"))
            elif act == "screenshot":
                shot = screenshot()
                # 每步都带上图（前端要能看见），不是只留最后一步
                r["result"] = {"ok": shot.get("ok"),
                               "bytes": shot.get("bytes"),
                               "image": shot.get("image"),
                               "error": shot.get("error")}
            elif act == "isaac_logs":
                r["result"] = isaac_logs(int(s.get("n") or 40))
            elif act == "wait":
                time.sleep(min(float(s.get("seconds") or 1), 10))
                r["result"] = {"ok": True}
            else:
                r["result"] = {"ok": False, "error": "不允许的 action"}
        except Exception as e:
            r["result"] = {"ok": False, "error": str(e)}
        r["elapsed"] = round(time.time() - t0, 2)
        results.append(r)
        if stop_on_error and not (r.get("result") or {}).get("ok", False):
            break
    return {"ok": True, "count": len(results), "results": results}
