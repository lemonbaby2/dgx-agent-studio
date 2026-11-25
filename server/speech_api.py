# -*- coding: utf-8 -*-
"""
speech_api · v7 语音交互 + 论文朗读
====================================
跑在 DGX 上（服务端），配合浏览器端录音与浏览器 TTS。

链路：
  浏览器 USB 麦克风 -> MediaRecorder(webm/opus)
    -> POST /api/speech/asr   (base64)
       -> ffmpeg 转 16k 单声道 wav
       -> faster-whisper(CPU/int8) 中文转写
  本机 PDF -> POST /api/pdf/read (base64)
       -> pymupdf 提取中文文本
       -> (可选) Ollama 讲解/摘要
  最终文字回到浏览器 -> speechSynthesis 朗读出声

设计要点：
· WhisperModel 全局缓存，避免每次请求重新加载（加载一次要好几秒）
· ffmpeg 不可用时直接报错，不假装成功
· 中文默认，language="zh"
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import uuid

# 必须在真正加载 faster_whisper 之前设好：DGX 直连 huggingface.co 不通，
# 不设镜像会在拉取模型时长时间空等（实测卡 4 分半才报错）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# 模型缓存：{size: WhisperModel}
_WHISPER = {}
_WHISPER_ERR = {}


def _has(mod: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def status():
    """返回各后端可用性，给前端显示徽标"""
    return {
        "ffmpeg": shutil.which("ffmpeg") or "",
        "asr": _has("faster_whisper") or _has("whisper"),
        "asr_kind": ("faster-whisper" if _has("faster_whisper")
                     else ("whisper" if _has("whisper") else "")),
        "pdf": _has("pymupdf") or _has("fitz"),
        "pdf_kind": ("pymupdf" if _has("pymupdf")
                     else ("fitz" if _has("fitz") else "")),
    }


# ---------------------------------------------------------------- 音频规整
def normalize_audio(src: str, timeout=120):
    """
    任意音频 -> 16kHz 单声道 **裸 PCM(s16le)**。
    注意：刻意不用 wav 容器，因为 faster-whisper 解码 wav 要走 PyAV，
    而 DGX 上的 PyAV 19 与其不兼容（av.open 的 metadata_errors 参数已被移除）。
    直接出裸 PCM 再用 numpy 读入，彻底绕开 PyAV。
    返回 (path, err)
    """
    ff = shutil.which("ffmpeg")
    if not ff:
        return None, "缺少 ffmpeg，无法转码音频"
    dst = os.path.join(tempfile.gettempdir(),
                       "asr_%s.pcm" % uuid.uuid4().hex[:10])
    cmd = [ff, "-v", "error", "-i", src, "-ar", "16000", "-ac", "1",
           "-f", "s16le", "-c:a", "pcm_s16le", dst, "-y"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if p.returncode != 0:
            return None, ((p.stderr or b"").decode("utf-8", "ignore")[:300]
                          or "ffmpeg 转码失败")
        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
            return None, "转码后音频为空（麦克风可能没录到声音）"
        return dst, None
    except subprocess.TimeoutExpired:
        return None, "音频转码超时"
    except Exception as e:
        return None, str(e)


def _pcm_to_f32(path: str):
    """裸 s16le PCM -> float32 数组（归一化到 [-1,1]）"""
    import numpy as np  # 只在需要时导入
    data = np.fromfile(path, dtype="<i2")
    return data.astype("float32") / 32768.0


def _write_b64(data_b64: str, suffix: str):
    """base64 -> 临时文件。返回路径"""
    raw = data_b64 or ""
    k = raw.find("base64,")
    if k >= 0:
        raw = raw[k + 7:]
    raw = "".join(raw.split())
    if not raw:
        return None
    try:
        blob = base64.b64decode(raw)
    except Exception:
        return None
    p = os.path.join(tempfile.gettempdir(),
                     "up_%s%s" % (uuid.uuid4().hex[:10], suffix))
    with open(p, "wb") as f:
        f.write(blob)
    return p


# ---------------------------------------------------------------- ASR
def _model(size="small"):
    """加载并缓存 WhisperModel"""
    if size in _WHISPER:
        return _WHISPER[size], None
    if size in _WHISPER_ERR:
        return None, _WHISPER_ERR[size]
    try:
        from faster_whisper import WhisperModel  # type: ignore
        m = WhisperModel(size, device="cpu", compute_type="int8")
        _WHISPER[size] = m
        return m, None
    except Exception as e:
        _WHISPER_ERR[size] = str(e)
        return None, str(e)


def transcribe(audio_path: str, size="small", lang="zh", timeout=300):
    """
    语音转文字。返回 {"ok","text","backend","error"}
    """
    res = {"ok": False, "text": "", "backend": "", "error": ""}
    wav, err = normalize_audio(audio_path)
    if err:
        res["error"] = err
        return res
    try:
        m, merr = _model(size)
        if m is None:
            if _has("whisper"):
                try:
                    import whisper  # type: ignore
                    mm = whisper.load_model(size)
                    r = mm.transcribe(_pcm_to_f32(wav), language=(lang or "zh"))
                    res.update(ok=True, text=(r.get("text") or "").strip(),
                               backend="openai-whisper(%s)" % size)
                    return res
                except Exception as e:
                    res["error"] = "whisper 失败：%s" % e
                    return res
            res["error"] = ("未装语音识别后端（%s）。"
                            "请在 DGX 执行：python3 -m pip install "
                            "--break-system-packages faster-whisper"
                            % (merr or "无"))
            return res
        audio = _pcm_to_f32(wav)
        if audio.size == 0:
            res["error"] = "音频为空"
            return res
        # 静音检测：避免对纯噪音做无意义推理（不依赖 VAD 模型，省一次下载）
        rms = float((audio ** 2).mean() ** 0.5) if audio.size else 0.0
        if rms < 0.003:
            res["error"] = ("几乎没检测到声音（RMS=%.5f），"
                            "请检查麦克风是否被选中、音量是否太低" % rms)
            return res
        # 传 ndarray 而非路径：绕开 PyAV 解码
        segs, _info = m.transcribe(
            audio,
            language=(lang or "zh"),
            vad_filter=False,
        )
        txt = "".join(s.text for s in segs).strip()
        res.update(ok=True, text=txt, rms=round(rms, 5),
                   backend="faster-whisper(%s, cpu/int8)" % size)
        return res
    except Exception as e:
        res["error"] = "识别异常：%s" % e
        return res
    finally:
        try:
            if wav and os.path.exists(wav):
                os.remove(wav)
        except Exception:
            pass


# ---------------------------------------------------------------- PDF
def pdf_text(pdf_path: str, max_pages=0, max_chars=60000):
    """
    提取 PDF 全文（中文可用）。返回 {"ok","text","pages","chars","error"}
    """
    res = {"ok": False, "text": "", "pages": 0, "chars": 0, "error": ""}
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except Exception:
            import fitz  # type: ignore
    except Exception as e:
        res["error"] = "未装 PDF 解析库（%s）。请执行：python3 -m pip install " \
                       "--break-system-packages pymupdf" % e
        return res
    try:
        doc = fitz.open(pdf_path)
        n = doc.page_count
        res["pages"] = n
        parts = []
        total = 0
        limit = n if not max_pages else min(n, int(max_pages))
        for i in range(limit):
            t = doc[i].get_text() or ""
            parts.append(t)
            total += len(t)
            if total >= max_chars:
                parts.append("\n...(已达 %d 字上限，后续页截断)" % max_chars)
                break
        doc.close()
        text = "\n".join(parts)
        res.update(ok=True, text=text[:max_chars], chars=len(text))
        return res
    except Exception as e:
        res["error"] = "PDF 解析失败：%s" % e
        return res


def pdf_meta(pdf_path: str):
    """取页数与标题，供前端提示"""
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except Exception:
            import fitz  # type: ignore
        doc = fitz.open(pdf_path)
        meta = doc.metadata or {}
        r = {"pages": doc.page_count, "title": (meta.get("title") or "")}
        doc.close()
        return r
    except Exception:
        return {"pages": 0, "title": ""}


# ---------------------------------------------------------------- 一体化
def speech_ask(base, model, audio_path, question=None, size="small",
               lang="zh", temperature=0.6, timeout=600):
    """
    语音问 -> 转写 -> LLM 回答。
    返回 {"ok","asr","answer","error","backend"}
    """
    out = {"ok": False, "asr": "", "answer": "", "error": "", "backend": ""}
    r = transcribe(audio_path, size=size, lang=lang)
    out["asr"] = r.get("text") or ""
    out["backend"] = r.get("backend") or ""
    if not r.get("ok"):
        out["error"] = r.get("error") or "识别失败"
        return out
    if not out["asr"]:
        out["error"] = "没听清（转写结果为空），请再说一遍"
        return out
    q = (question or "").strip()
    prompt = out["asr"] if not q else "%s\n\n（补充要求：%s）" % (out["asr"], q)
    try:
        from . import core
        ans, _t = core.chat_stream(
            base, model,
            [{"role": "user", "content": prompt}],
            temperature=temperature, timeout=timeout)
        out.update(ok=True, answer=ans)
    except Exception as e:
        out["error"] = "LLM 回答失败：%s" % e
    return out


def pdf_ask(base, model, pdf_path, question, max_chars=60000,
            temperature=0.5, timeout=900):
    """
    读论文 -> 交给 LLM 讲解。返回 {"ok","answer","pages","chars","error"}
    """
    out = {"ok": False, "answer": "", "pages": 0, "chars": 0, "error": ""}
    r = pdf_text(pdf_path, max_chars=max_chars)
    out["pages"] = r.get("pages", 0)
    out["chars"] = r.get("chars", 0)
    if not r.get("ok") or not r.get("text", "").strip():
        out["error"] = r.get("error") or "PDF 没提取到文字（可能是扫描件/纯图片）"
        return out
    ctx = r["text"]
    sysmsg = ("你是论文讲解助手，面向工程师。用中文回答，"
              "结论先行、分点说明、保留关键数字与模型名。")
    prompt = ("下面是论文全文（共 %d 页，%d 字）。\n\n%s\n\n"
              "请回答：%s" % (out["pages"], out["chars"], ctx,
                             question or "请概括这篇论文的核心内容"))
    try:
        from . import core
        ans, _t = core.chat_stream(
            base, model,
            [{"role": "system", "content": sysmsg},
             {"role": "user", "content": prompt}],
            temperature=temperature, timeout=timeout)
        out.update(ok=True, answer=ans)
    except Exception as e:
        out["error"] = "LLM 讲解失败：%s" % e
    return out
