# -*- coding: utf-8 -*-
"""
voice_api · 语音（ASR 听写 / TTS 朗读）
--------------------------------------
现实约束（实测）：DGX 上 whisper/funasr/edge-tts/piper 全都没装，且 DGX 无外网
（pypi 直连 FAIL）。所以这里设计成 **多后端自动降级**：

ASR 后端优先级：
  1. remote  —— DGX GPU 上的 FunASR/SenseVoice 或 faster-whisper（精度高、走 GPU）
  2. local   —— 本机装的 faster-whisper / openai-whisper（本机有网，pip 可装）
  3. 都不可用 -> 明确报错 + 给安装命令，不装听不懂就假装支持

TTS 后端优先级：
  1. sapi    —— Windows 内置语音（PowerShell COM），**离线可用、零安装**，优先
  2. edge    —— edge-tts 在线音质更好，但需要外网
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid

_FFMPEG_HINT = "需要 ffmpeg 把音频转成 16k 单声道 wav"


# ---------------------------------------------------------------- 探测
def _has(mod: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _has_exe(name: str) -> bool:
    return bool(shutil.which(name))


def asr_backends(ssh_conf=None):
    """返回可用 ASR 后端列表 [(name, desc, ok)]"""
    out = []
    # 远端 DGX
    if ssh_conf:
        ok, info = _remote_import(ssh_conf, ["funasr", "faster_whisper", "whisper"])
        out.append(("remote", info or ("DGX %s" % ssh_conf.get("host")), ok))
    # 本机
    if _has("faster_whisper"):
        out.append(("local-faster-whisper", "本机 faster-whisper", True))
    if _has("whisper"):
        out.append(("local-whisper", "本机 openai-whisper", True))
    if _has("funasr"):
        out.append(("local-funasr", "本机 FunASR/SenseVoice", True))
    return out


def _remote_import(ssh_conf, mods, timeout=25):
    """探测远端 python 里有没有这些包"""
    host, user = ssh_conf.get("host"), ssh_conf.get("user", "nvidia")
    py = ssh_conf.get("python") or "python3"
    chk = ";".join(
        "python -c 'import %s' 2>/dev/null && echo HAVE_%s" % (m, m) for m in mods
    )
    # 用远端 python 跑一段检测
    script = (
        "for m in %s; do %s -c \"import $m\" >/dev/null 2>&1 && echo HAVE_$m; done"
        % (" ".join(mods), py)
    )
    try:
        p = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
             "%s@%s" % (user, host), script],
            capture_output=True, timeout=timeout)
        txt = (p.stdout or b"").decode("utf-8", "ignore")
        have = [m for m in mods if ("HAVE_%s" % m) in txt]
        if have:
            return True, "DGX 已装：" + ", ".join(have)
        return False, "DGX 未装 ASR（funasr/whisper 都没有）"
    except Exception as e:
        return False, "SSH 探测失败：%s" % e


# ---------------------------------------------------------------- 音频规整
def normalize_audio(src: str, timeout=120):
    """统一转 16kHz 单声道 wav，返回新路径"""
    ff = shutil.which("ffmpeg")
    if not ff:
        return None, _FFMPEG_HINT
    dst = os.path.join(tempfile.gettempdir(), "asr_%s.wav" % uuid.uuid4().hex[:10])
    cmd = [ff, "-v", "error", "-i", src, "-ar", "16000", "-ac", "1",
           "-c:a", "pcm_s16le", dst, "-y"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if p.returncode != 0:
            return None, (p.stderr or b"").decode("utf-8", "ignore")[:300]
        return dst, None
    except subprocess.TimeoutExpired:
        return None, "音频转换超时"
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------- ASR
def transcribe(audio_path: str, ssh_conf=None, lang="zh", timeout=300):
    """
    语音转文字。返回 {"ok":bool,"text":str,"backend":str,"error":str}
    """
    res = {"ok": False, "text": "", "backend": "", "error": ""}

    wav, err = normalize_audio(audio_path)
    if err:
        res["error"] = err
        return res
    try:
        # 1) 远端 DGX（GPU）
        if ssh_conf:
            ok, info = _remote_import(ssh_conf, ["funasr", "faster_whisper", "whisper"])
            if ok:
                txt, e = _asr_remote(ssh_conf, wav, lang, timeout)
                if txt:
                    res.update(ok=True, text=txt, backend=info)
                    return res
                res["error"] = e or "远端识别失败"

        # 2) 本机后端
        if _has("faster_whisper"):
            try:
                from faster_whisper import WhisperModel  # type: ignore
                m = WhisperModel(ssh_conf.get("size") or "small",
                                 device="cpu", compute_type="int8")
                segs, _info = m.transcribe(wav, language=("zh" if lang == "zh" else None))
                res.update(ok=True, text="".join(s.text for s in segs).strip(),
                           backend="本机 faster-whisper")
                return res
            except Exception as e:
                res["error"] = "本机 faster-whisper 失败：%s" % e
        elif _has("whisper"):
            try:
                import whisper  # type: ignore
                m = whisper.load_model("small")
                r = m.transcribe(wav, language=("zh" if lang == "zh" else None))
                res.update(ok=True, text=(r.get("text") or "").strip(),
                           backend="本机 openai-whisper")
                return res
            except Exception as e:
                res["error"] = "本机 whisper 失败：%s" % e

        if not res["error"]:
            res["error"] = (
                "没有可用的语音识别后端。DGX 和你本机都没装 whisper/funasr。\n"
                "最快见效：在本机执行 pip install faster-whisper（本机有网），"
                "装完重启 DGX Agent Studio 即可用「麦克风说话」；"
                "想走 DGX GPU 跑大批量音频，执行 _tools 里的 "
                "install_asr_dgx.sh（把包从本机传上去装）。"
            )
        return res
    finally:
        try:
            if wav and os.path.exists(wav):
                os.remove(wav)
        except Exception:
            pass


def _asr_remote(ssh_conf, wav, lang, timeout):
    """把 wav 传上去，在 DGX 用 funasr/whisper 识别"""
    host, user = ssh_conf.get("host"), ssh_conf.get("user", "nvidia")
    py = ssh_conf.get("python") or "python3"
    rid = uuid.uuid4().hex[:8]
    rtmp = "/tmp/_asr_%s" % rid
    script = r'''
import sys, json
p = sys.argv[1]
text = ""
try:
    from funasr import AutoModel
    m = AutoModel(model="iic/SenseVoiceSmall", trust_remote_code=True)
    r = m.generate(input=p, language="zh", use_itn=True)
    text = r[0].get("text", "") if r else ""
except Exception as e1:
    try:
        import whisper
        m = whisper.load_model("small")
        text = m.transcribe(p, language="zh").get("text", "")
    except Exception as e2:
        print(json.dumps({"err": "funasr:%s | whisper:%s" % (e1, e2)}))
        sys.exit(1)
print(json.dumps({"text": text}))
'''
    rscript = "%s/_asr_%s.py" % (rtmp, rid)
    try:
        subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no",
                        "%s@%s" % (user, host), "mkdir -p %s" % rtmp],
                       capture_output=True, timeout=30)
        subprocess.run(["scp", "-o", "StrictHostKeyChecking=no", "-q", wav,
                        "%s@%s:%s/in.wav" % (user, host, rtmp)],
                       capture_output=True, timeout=180, check=True)
        # 写脚本
        p = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no",
                            "%s@%s" % (user, host),
                            "cat > %s" % rscript],
                           input=script.encode("utf-8"),
                           capture_output=True, timeout=60)
        if p.returncode != 0:
            return "", "远端写脚本失败"
        p = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no",
                            "%s@%s" % (user, host),
                            "%s %s %s/in.wav" % (py, rscript, rtmp)],
                           capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "ignore").strip()
        subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no",
                        "%s@%s" % (user, host), "rm -rf %s" % rtmp],
                       capture_output=True, timeout=30)
        if not out:
            return "", "远端无输出"
        data = json.loads(out.splitlines()[-1])
        return (data.get("text") or "").strip(), data.get("err", "")
    except subprocess.CalledProcessError as e:
        return "", "音频上传失败：%s" % e
    except Exception as e:
        return "", "远端识别异常：%s" % e


# ---------------------------------------------------------------- TTS
def tts_sapi(text: str, rate=0):
    """
    Windows 内置语音朗读（离线、零依赖）。直接播放，不落 mp3。
    rate: -10 ~ 10
    """
    if os.name != "nt":
        return False, "仅 Windows 支持内置语音"
    safe = (text or "").replace("'", "''")[:4000]
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Rate = %d;"
        "$s.Speak('%s');" % (int(rate), safe.replace("\n", " ").replace("\r", " "))
    )
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps],
                           capture_output=True, timeout=180)
        return p.returncode == 0, "" if p.returncode == 0 else \
            (p.stderr or b"").decode("utf-8", "ignore")[:300]
    except Exception as e:
        return False, str(e)


def tts(text: str, backend="auto", voice="zh-CN-XiaoxiaoNeural", rate=0):
    """
    文本转语音。返回 {"ok","backend","file"/"played","error"}
    sapi 直接朗读；edge 生成 mp3。
    """
    res = {"ok": False, "backend": "", "error": "", "played": False}
    if backend in ("auto", "sapi"):
        if os.name == "nt":
            ok, err = tts_sapi(text, rate=rate)
            if ok:
                res.update(ok=True, backend="Windows 内置语音（离线）", played=True)
                return res
            res["error"] = err

    if backend in ("auto", "edge"):
        if not shutil.which("edge-tts"):
            res["error"] = (res["error"] + " | edge-tts 未安装"
                            "(pip install edge-tts，需要外网)").strip(" |")
            return res
        out = os.path.join(tempfile.gettempdir(), "tts_%s.mp3" % uuid.uuid4().hex[:8])
        cmd = ["edge-tts", "--voice", voice, "--text", text, "--write-media", out]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=180)
            if p.returncode == 0 and os.path.exists(out):
                res.update(ok=True, backend="edge-tts", file=out)
                return res
            res["error"] = (p.stderr or b"").decode("utf-8", "ignore")[:300]
        except Exception as e:
            res["error"] = str(e)
    return res
