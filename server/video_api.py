# -*- coding: utf-8 -*-
"""
video_api · 视频理解
--------------------
视频 -> ffmpeg 均匀抽帧 -> 多帧塞给视觉语言模型(qwen3-vl) -> 自然语言答案。

设计要点：
1. 视频不需要任何专用视频模型，抽帧 + 多图 VLM 即可
   （DGX 上现成的 qwen3-vl:30b-a3b-instruct 原生支持多图输入）。
2. ffmpeg 可在本地跑，也可走 SSH 在 DGX 上跑 —— 哪边有文件就在哪边抽，
   避免把几十上百 MB 的原视频搬来搬去。
3. 帧数与时长的取舍：默认 8 帧均匀采样，长视频自动降采样，
   防止 prompt 体积炸掉（每帧 ~200-400KB base64）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request

# v12（2026-10-26）：qwen2.5vl:7b 已下线（给 70b 腾资源），视觉能力统一由
# qwen3-vl:30b-a3b-instruct 承担 —— 图像理解 / OCR / 目标检测都在它身上。
VL_PREFERRED = [
    "qwen3-vl:30b-a3b-instruct",
]

DEFAULT_FRAMES = 8
MAX_FRAMES = 24
# 单帧 JPEG 宽度上限，超过会拉爆 prompt
MAX_FRAME_W = 768


# ------------------------------------------------------------------ 工具
def which_ffmpeg() -> str:
    """本机 ffmpeg 路径，找不到返回空串"""
    exe = shutil.which("ffmpeg") or ""
    return exe


def _run(cmd, timeout=120):
    """跑命令，返回 (ok, stdout+stderr)"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "ignore") + \
              (p.stderr or b"").decode("utf-8", "ignore")
        return p.returncode == 0, out.strip()
    except subprocess.TimeoutExpired:
        return False, "命令超时"
    except FileNotFoundError:
        return False, "命令不存在"
    except Exception as e:
        return False, str(e)


def is_remote(path: str) -> bool:
    """判断是否为远端路径：dgx:/xxx  或  user@host:/xxx"""
    return bool(re.match(r"^(dgx:|[a-z0-9_.-]+@[a-z0-9_.-]+:)", path or ""))


# ------------------------------------------------------------------ 抽帧
def probe_duration(video: str, ssh=None, timeout=60):
    """用 ffprobe 拿视频时长(秒)。ssh 为 (host,user) 时走远端。"""
    if ssh:
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no",
               "%s@%s" % (ssh[1], ssh[0]),
               "ffprobe -v error -show_entries format=duration "
               "-of default=nk=1:nw=1 '%s'" % _remote_path(video)]
    else:
        probe = shutil.which("ffprobe") or ""
        if not probe:
            return None, "找不到 ffprobe"
        cmd = [probe, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nk=1:nw=1", video]
    ok, out = _run(cmd, timeout=timeout)
    if not ok:
        return None, out or "ffprobe 失败"
    try:
        return float(out.strip().splitlines()[-1]), None
    except Exception:
        return None, "时长解析失败：%s" % out[:200]


def _remote_path(p: str) -> str:
    """dgx:/a/b -> /a/b ;  user@host:/a/b -> /a/b"""
    return p.split(":", 1)[1] if ":" in p else p


def extract_frames(video: str, n=DEFAULT_FRAMES, ssh=None, workdir=None,
                   timeout=180):
    """
    均匀抽 n 帧 JPEG，返回本地 jpg 路径列表。
    ssh=(host,user) 时在远端抽帧并回传，否则本机抽。
    """
    n = max(1, min(int(n or DEFAULT_FRAMES), MAX_FRAMES))

    # 关键：必须先拿【真实时长】再算采样率，否则短视频会抽到 0 帧（见 _fps_for 注释）
    dur, _ = probe_duration(video, ssh=ssh)
    fps = _fps_for(n + 1, dur)

    tmp = workdir or tempfile.mkdtemp(prefix="dgxframes_")
    scale = "scale='min(%d,iw)':-1" % MAX_FRAME_W

    if ssh:
        host, user = ssh
        rvideo = _remote_path(video)
        rtmp = "/tmp/_dgx_frames_%d" % os.getpid()
        # 远端抽帧
        rf = ("rm -rf %s && mkdir -p %s && ffmpeg -v error -i '%s' "
              "-vf \"fps=%.4f,%s\" -q:v 4 %s/f%%03d.jpg -y"
              % (rtmp, rtmp, rvideo, fps, scale, rtmp))
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "%s@%s" % (user, host), rf]
        ok, out = _run(cmd, timeout=timeout)
        if not ok:
            return [], "远端抽帧失败：%s" % out[:300]
        # 回传
        # ⚠️ 坑：scp -r 的源如果写成 host:/dir/（带斜杠尾巴的反面写法），
        #    会在目标里再套一层同名子目录，导致后面 os.listdir(tmp) 看不到帧。
        #    这里显式用 "dir/." 表示拷「目录里的内容」，而不是目录本身。
        try:
            p = subprocess.run(
                ["scp", "-o", "StrictHostKeyChecking=no", "-q", "-r",
                 "%s@%s:%s/." % (user, host, rtmp), tmp],
                capture_output=True, timeout=timeout)
            if p.returncode != 0:
                return [], "帧回传失败：%s" % (p.stderr or b"").decode("utf-8", "ignore")[:300]
        except Exception as e:
            return [], "帧回传异常：%s" % e
        # 清远端临时目录
        subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no",
                        "%s@%s" % (user, host), "rm -rf %s" % rtmp],
                       capture_output=True, timeout=30)
    else:
        ff = which_ffmpeg()
        if not ff:
            return [], "本机找不到 ffmpeg"
        cmd = [ff, "-v", "error", "-i", video,
               "-vf", "fps=%.4f,%s" % (fps, scale),
               "-q:v", "4", os.path.join(tmp, "f%03d.jpg"), "-y"]
        ok, out = _run(cmd, timeout=timeout)
        if not ok:
            return [], "抽帧失败：%s" % out[:300]

    # 递归收帧：兼容 scp 可能多套一层子目录的情况，比只看顶层稳
    files = []
    for root, _dirs, names in os.walk(tmp):
        for f in names:
            if f.lower().endswith((".jpg", ".jpeg")):
                files.append(os.path.join(root, f))
    files.sort()

    # 数量裁剪到 n 张（ffmpeg fps 估算可能略多/少）
    if len(files) > n:
        step = len(files) / float(n)
        files = [files[int(i * step)] for i in range(n)]
    return files, None


def _fps_for(n, duration):
    """
    按【真实时长】算 ffmpeg 的 fps，保证抽到 n 帧。

    ⚠️ 踩过的坑：早期版本写死 assume=100 秒 -> fps = n/100 = 0.08，
    遇到 3.5 秒的短素材算出来就是「每 12.5 秒一帧」= 0 帧。
    ffmpeg 加 -v error 时不报错，目录空空如也，极难排查。
    所以必须按实际时长算，并给 fps 下限保底。
    """
    if not duration or duration <= 0:
        duration = 60.0            # 拿不到时长时的兜底假设
    # 下限：至少覆盖整段（每 duration 秒 1 帧）；上限：别抽太密
    return max(1.0 / max(duration, 0.5), min(n / float(duration), 30.0))


def frames_to_b64(files, max_bytes=400_000):
    """jpeg -> base64 列表，自动丢弃过大的帧"""
    out = []
    for f in files:
        try:
            with open(f, "rb") as fh:
                b = fh.read()
            if len(b) > max_bytes:
                continue
            out.append(base64.b64encode(b).decode("ascii"))
        except Exception:
            continue
    return out


# ------------------------------------------------------------------ 调用 VLM
def pick_vl_model(base, available=None):
    """在可用模型里挑一个视觉模型"""
    names = available or []
    for pref in VL_PREFERRED:
        for nm in names:
            if nm == pref or nm.startswith(pref.split(":")[0]):
                return nm
    # 退而求其次：名字里带 vl / vision / llava / minicpm 的
    for nm in names:
        low = nm.lower()
        if any(k in low for k in ("vl", "vision", "llava", "minicpm")):
            return nm
    return ""


def vlm_describe(base, model, images_b64, question, timeout=600):
    """
    调 ollama /api/chat，多图 + 问题 -> 文本。同步返回（不走 SSE，
    因为视频分析一般是"一次性出结论"，流式意义不大且易断）。
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": question, "images": images_b64}
        ],
        "stream": False,
        # 同理限制上下文：不给 num_ctx 的话 Ollama 会按可用内存自动放大到 128K，
        # 多图 VL 场景会直接吃爆内存（见 core.chat_stream 里的注释）
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }
    # ⚠️ 铁律：本机开着系统代理（Clash 之类），urllib 默认会读 http_proxy 环境变量
    #    去代理内网的请求，表现为 "Remote end closed connection without response"。
    #    访问 DGX 这种局域网地址必须显式禁用代理。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        base.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    msg = (data.get("message") or {}).get("content") or ""
    # 去掉 <think>...</think>
    return re.sub(r"<think>.*?</think>", "", msg, flags=re.S).strip()


# ------------------------------------------------------------------ 主入口
VIDEO_PROMPT = """你是某汽车零部件装配产线的 SOP 视觉审查助手。
下面是同一段作业视频按时间顺序均匀抽取的 {n} 帧画面。

请回答：{q}

要求：
1. 严格依据画面内容回答，不要臆测；画面看不清就说"无法判断"。
2. 如果涉及操作顺序/动作，请按帧的时间顺序描述（第1帧、第2帧...）。
3. 最后用一行给出结论，格式：【结论】xxxx
"""


def analyze_video(base, model, video, question, frames=DEFAULT_FRAMES,
                  ssh=None, available=None, timeout=600):
    """
    视频理解主流程。返回 dict。
    ssh=(host,user) 表示视频在远端 DGX 上。
    """
    res = {"ok": False, "model": model, "frames": 0}
    m = model or pick_vl_model(base, available)
    if not m:
        res["error"] = ("DGX 上没找到视觉模型。可用的 Models 里需要有 "
                        "qwen3-vl / qwen2.5vl 这类 VL 模型。")
        return res
    res["model"] = m

    dur, err = probe_duration(video, ssh=ssh)
    if dur:
        res["duration"] = round(dur, 2)

    tmp = tempfile.mkdtemp(prefix="dgxframes_")
    try:
        files, err = extract_frames(video, n=frames, ssh=ssh, workdir=tmp)
        if err:
            res["error"] = err
            res["hint"] = ("若是远端路径，用 dgx:/绝对路径 或 user@host:/绝对路径；"
                           "确认远端装了 ffmpeg。")
            return res
        if not files:
            res["error"] = "没抽到任何帧（视频格式不支持或文件损坏）"
            return res
        imgs = frames_to_b64(files)
        if not imgs:
            res["error"] = "帧读取失败"
            return res
        res["frames"] = len(imgs)

        prompt = VIDEO_PROMPT.format(n=len(imgs), q=question or "请描述这段视频的操作流程")
        try:
            res["answer"] = vlm_describe(base, m, imgs, prompt, timeout=timeout)
            res["ok"] = True
        except Exception as e:
            res["error"] = "视觉模型调用失败：%s" % e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res
