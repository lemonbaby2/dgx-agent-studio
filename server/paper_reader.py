# -*- coding: utf-8 -*-
"""
DGX Agent Studio · v8 论文朗读（paper_reader）
================================================================
职责：PDF → 分节 → 分块 → TTS 音频 → 前端顺序播放，可跳段调速。

为什么不能用浏览器 speechSynthesis 直接读：
  · 大模型生成的总结动辄上万字，浏览器 TTS 在中文长文本上会中途断、
    且无法按「章节」定位
  · 老师要的是「朗诵论文」，需要能暂停、跳章、调速、后台播放

链路：
  PDF ──speech_api.pdf_text──> 全文
      ──_split_sections──> [{title, text}]     （Abstract / Intro / Method ...）
      ──_chunk──> [{i, section, text}]         每块 ≤ 420 字（TTS 自然停顿点）
      ──edge-tts──> ~/.dgx_studio/tts/<doc>/<i>.mp3
  前端拿到 chunk 列表 + audio_url，用 <audio> 顺序播放

踩过的坑：
  · edge-tts 装不进系统 python（PEP 668 externally-managed），
    必须用独立 venv（~/.ttsvenv），通过 subprocess 调它的 python
  · edge-tts 首次连接要 TLS 握手，偶发慢；单段限时 90s，超时标记
    该段失败，前端退回浏览器 TTS，不整篇失败
  · PDF 里的换行会把一句话切碎 → 先做 soft-join 再按句末标点切
  · 中英混排要按块判断语种选 voice，否则中文读成英文字母
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

STUDIO_DATA = os.path.join(os.path.expanduser("~"), ".dgx_studio")
TTS_DIR = os.path.join(STUDIO_DATA, "tts")

# 独立 venv（PEP 668 绕行）
TTS_PY_CANDIDATES = [
    os.path.expanduser("~/.ttsvenv/bin/python"),
    os.path.expanduser("~/.ttsvenv/bin/python3"),
]
TTS_CLI_REL = os.path.join("tools", "tts_cli.py")

VOICE_ZH = "zh-CN-XiaoxiaoNeural"
VOICE_ZH_M = "zh-CN-YunxiNeural"
VOICE_EN = "en-US-AriaNeural"

MAX_CHUNK_CHARS = 420

_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 环境探测
# ---------------------------------------------------------------------------
def _tts_python():
    for p in TTS_PY_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


def _tts_cli():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(os.path.dirname(here), TTS_CLI_REL)
    return p if os.path.isfile(p) else ""


def status():
    py, cli = _tts_python(), _tts_cli()
    ready = bool(py and cli)
    ver = ""
    if ready:
        try:
            p = subprocess.run([py, "-c", "import edge_tts,sys;"
                                "sys.stdout.write(getattr(edge_tts,'__version__','?'))"],
                               capture_output=True, text=True, timeout=20,
                               encoding="utf-8", errors="ignore")
            ver = (p.stdout or "").strip() if p.returncode == 0 else ""
        except Exception:
            ver = ""
    n = 0
    if os.path.isdir(TTS_DIR):
        for d in os.listdir(TTS_DIR):
            n += len([f for f in os.listdir(os.path.join(TTS_DIR, d))
                      if f.endswith(".mp3")]) if os.path.isdir(
                os.path.join(TTS_DIR, d)) else 0
    return {
        "ok": True,
        "tts_ready": ready and bool(ver),
        "engine": "edge-tts" if ver else "",
        "version": ver,
        "python": py,
        "cli": cli,
        "cache_mp3": n,
        "voices": {"zh": VOICE_ZH, "zh_male": VOICE_ZH_M, "en": VOICE_EN},
    }


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------
SECTION_PATTERNS = [
    (r"^\s*abstract\b", "摘要 Abstract"),
    (r"^\s*(?:\d+[\.\s]*)?introduction\b", "引言 Introduction"),
    (r"^\s*(?:\d+[\.\s]*)?(?:related\s+work|background)\b", "相关工作 Related Work"),
    (r"^\s*(?:\d+[\.\s]*)?(?:method|approach|methodology)\b", "方法 Method"),
    (r"^\s*(?:\d+[\.\s]*)?(?:experiment|evaluation|results)\b", "实验 Experiment"),
    (r"^\s*(?:\d+[\.\s]*)?(?:discussion)\b", "讨论 Discussion"),
    (r"^\s*(?:\d+[\.\s]*)?(?:conclusion|conclusions)\b", "结论 Conclusion"),
    (r"^\s*(?:references|bibliography)\b", "参考文献 References"),
    (r"^\s*摘\s*要", "摘要"),
    (r"^\s*引\s*言", "引言"),
    (r"^\s*(?:结\s*论|总\s*结)", "结论"),
    (r"^\s*参考文献", "参考文献"),
]


def _soft_join(text):
    """PDF 硬换行修复：中文行尾直接接，英文行尾无连字符加空格"""
    lines = [l.rstrip() for l in (text or "").splitlines()]
    out = []
    for l in lines:
        if not out:
            out.append(l)
            continue
        prev = out[-1]
        if not prev.strip():
            out.append(l)
            continue
        if not l.strip():
            out.append(l)
            continue
        if prev.endswith("-"):
            out[-1] = prev[:-1] + l.lstrip()
        elif re.search(r"[\u4e00-\u9fff]$", prev):
            out[-1] = prev + l.lstrip()
        else:
            out[-1] = prev + " " + l.lstrip()
    return "\n".join(out)


def _split_sections(text):
    """按常见论文章节标题切分。切不出就整体一节。

    ⚠️ 顺序很关键：必须先在**原始行**上找标题，再做 soft_join。
    反过来（先 join 再找）会把 "Abstract\\nThis paper..." 粘成
    "Abstract This paper..."，行首匹配全部失效 —— 实测只切出「全文」一节。
    """
    raw_lines = (text or "").splitlines()
    marks = []
    for i, l in enumerate(raw_lines):
        s = l.strip()
        if not s or len(s) > 60:
            continue
        low = s.lower()
        for pat, name in SECTION_PATTERNS:
            if re.search(pat, low):
                marks.append((i, name))
                break
    if len(marks) < 2:
        return [{"title": "全文", "text": _soft_join(text).strip()}]
    secs = []
    for k, (idx, name) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(raw_lines)
        body = _soft_join("\n".join(raw_lines[idx + 1:end])).strip()
        if body:
            secs.append({"title": name, "text": body})
    return secs or [{"title": "全文", "text": _soft_join(text).strip()}]


_SENT_RE = re.compile(r"(?<=[。！？；\.\!\?;])\s*")


def _chunk_text(t, limit=MAX_CHUNK_CHARS):
    """按句切块，单块 ≤ limit 字"""
    t = re.sub(r"\s+", " ", (t or "").strip())
    if not t:
        return []
    sents = [s.strip() for s in _SENT_RE.split(t) if s.strip()]
    out, buf = [], ""
    for s in sents:
        if len(s) > limit:                  # 超长单句硬切
            if buf:
                out.append(buf); buf = ""
            for i in range(0, len(s), limit):
                out.append(s[i:i + limit])
            continue
        if len(buf) + len(s) + 1 <= limit:
            buf = (buf + " " + s).strip() if buf else s
        else:
            out.append(buf)
            buf = s
    if buf:
        out.append(buf)
    return out


def _voice_for(text, voice=None):
    if voice:
        return voice
    zh = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    return VOICE_ZH if zh >= 4 else VOICE_EN


def _doc_id(path, text):
    raw = (str(path or "") + "|" + (text or "")[:200]).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def prepare(pdf_path=None, text=None, title=None, max_chars=120000,
            max_chunks=400, voice=None, rate=0):
    """解析 PDF 或直接给文本 → 切节切块 → 返回 chunk 清单（音频按需合成）"""
    from . import speech_api

    src_text = text or ""
    pages = 0
    if pdf_path:
        r = speech_api.pdf_text(pdf_path, max_chars=max_chars)
        if not r.get("ok") or not (r.get("text") or "").strip():
            return {"ok": False,
                    "error": r.get("error") or "PDF 没提取到文字（可能是扫描件）"}
        src_text = r["text"]
        pages = r.get("pages") or 0
    if not (src_text or "").strip():
        return {"ok": False, "error": "没有可读文本"}

    secs = _split_sections(src_text)
    chunks = []
    for si, sec in enumerate(secs):
        for piece in _chunk_text(sec["text"]):
            chunks.append({"section": sec["title"], "section_idx": si,
                           "text": piece, "chars": len(piece)})
    truncated = len(chunks) > max_chunks
    chunks = chunks[:max_chunks]
    for i, c in enumerate(chunks):
        c["i"] = i
        c["voice"] = _voice_for(c["text"], voice)

    did = _doc_id(pdf_path or title, src_text)
    d = os.path.join(TTS_DIR, did)
    os.makedirs(d, exist_ok=True)
    # 写一份元数据，便于恢复
    try:
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"doc": did, "title": title or "",
                       "pdf": pdf_path or "", "pages": pages,
                       "chars": len(src_text), "chunks": len(chunks)},
                      f, ensure_ascii=False, indent=1)
    except Exception:
        pass

    return {
        "ok": True, "doc": did, "pages": pages, "chars": len(src_text),
        "sections": [{"title": s["title"], "chars": len(s["text"])} for s in secs],
        "chunks": chunks, "total_chunks": len(chunks),
        "truncated": truncated, "rate": rate,
        "tts_ready": status()["tts_ready"],
    }


def _audio_path(doc, idx):
    # doc 来自内部 hex，但仍做一次白名单防穿越
    if not re.match(r"^[0-9a-f]{6,32}$", doc or ""):
        return None
    return os.path.join(TTS_DIR, doc, "%04d.mp3" % int(idx))


def synth(doc, idx, text=None, voice=None, rate=0, force=False):
    """合成单块。已有缓存直接返回。"""
    p = _audio_path(doc, idx)
    if not p:
        return {"ok": False, "error": "doc 标识非法"}
    if os.path.isfile(p) and os.path.getsize(p) > 800 and not force:
        return {"ok": True, "doc": doc, "i": int(idx), "cached": True,
                "bytes": os.path.getsize(p), "url": audio_url(doc, idx)}

    py, cli = _tts_python(), _tts_cli()
    if not (py and cli):
        return {"ok": False, "error": "edge-tts 未就绪（缺 ~/.ttsvenv 或 tools/tts_cli.py）",
                "fallback": "browser"}

    if not (text or "").strip():
        # 从 meta 找不到原文时，允许前端把文本一起传上来
        return {"ok": False, "error": "缺少待合成文本"}
    text = text.strip()[:3000]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tf = p + ".txt"
    try:
        with open(tf, "w", encoding="utf-8") as f:
            f.write(text)
        cmd = [py, cli, "--text-file", tf,
               "--voice", voice or _voice_for(text),
               "--rate", str(int(rate or 0)), "--out", p]
        t0 = time.time()
        pr = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                            encoding="utf-8", errors="ignore")
        if pr.returncode != 0 or not os.path.isfile(p):
            msg = ((pr.stdout or "") + (pr.stderr or "")).strip()[:400]
            return {"ok": False, "error": "TTS 合成失败：%s" % msg,
                    "fallback": "browser"}
        return {"ok": True, "doc": doc, "i": int(idx), "cached": False,
                "bytes": os.path.getsize(p),
                "elapsed": round(time.time() - t0, 2),
                "url": audio_url(doc, idx)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "TTS 超时（120s）", "fallback": "browser"}
    except Exception as e:
        return {"ok": False, "error": "TTS 异常：%s" % e, "fallback": "browser"}
    finally:
        try:
            os.remove(tf)
        except Exception:
            pass


def synth_many(doc, items, voice=None, rate=0):
    """批量合成（前端预取前 N 段用）。items: [{i, text}]"""
    out = []
    for it in (items or [])[:40]:
        try:
            out.append(synth(doc, it.get("i"), it.get("text"),
                             voice or it.get("voice"), rate))
        except Exception as e:
            out.append({"ok": False, "i": it.get("i"), "error": str(e)})
        # 每段合成后短暂让出，避免被远端限流
        time.sleep(0.05)
    ok = sum(1 for x in out if x.get("ok"))
    return {"ok": ok > 0, "done": ok, "total": len(out), "items": out}


def audio_url(doc, idx):
    return "/api/tts/audio?doc=%s&i=%d" % (doc, int(idx))


def read_audio(doc, idx):
    """给 HTTP 层读 mp3 字节"""
    p = _audio_path(doc, idx)
    if not p or not os.path.isfile(p):
        return None
    try:
        with open(p, "rb") as f:
            return f.read()
    except Exception:
        return None


def purge(doc=None):
    """清缓存：doc 指定只清该文档，否则全清"""
    try:
        if doc:
            d = os.path.join(TTS_DIR, doc)
            if os.path.isdir(d) and re.match(r"^[0-9a-f]{6,32}$", doc):
                shutil.rmtree(d, ignore_errors=True)
        else:
            shutil.rmtree(TTS_DIR, ignore_errors=True)
        return {"ok": True, "status": status()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 论文总结 → 朗读稿（读「总结」而不是读全文，老师说要「朗诵总结 idea」）
# ---------------------------------------------------------------------------
NARRATE_SYSTEM = """你是论文播报员。把给定的论文内容改写成**适合朗读的口播稿**。

要求：
· 中文，口语化但不啰嗦，像给同行做 3 分钟口头汇报
· 结构：一句话定位 → 它要解决什么问题 → 核心方法（用比喻解释）→ 
  关键实验数字 → 最大局限 → 一句话价值判断
· 每段不超过 120 字，段间空一行（便于逐段合成语音）
· 不要出现 Markdown 符号、公式、参考文献编号、表格
· 数字保留（如 mAP 0.816、32 层、3.5 倍）
· 只输出口播稿正文，不要开场白解释你在做什么
"""


def narrate_script(pdf_path=None, text=None, base=None, model=None,
                   chat_stream=None, max_chars=70000):
    """PDF → 口播稿（给 TTS 读）。论文「朗诵总结」走这条。"""
    from . import speech_api

    src = text or ""
    pages = 0
    if pdf_path:
        r = speech_api.pdf_text(pdf_path, max_chars=max_chars)
        if not r.get("ok") or not (r.get("text") or "").strip():
            return {"ok": False,
                    "error": r.get("error") or "PDF 没提取到文字"}
        src, pages = r["text"], r.get("pages") or 0
    if not (src or "").strip():
        return {"ok": False, "error": "没有可读文本"}
    if not chat_stream:
        return {"ok": False, "error": "未注入 LLM 调用器"}

    msgs = [{"role": "system", "content": NARRATE_SYSTEM},
            {"role": "user",
             "content": "论文正文如下，请写成口播稿：\n\n" + src[:max_chars]}]
    try:
        ans, _ = chat_stream(base or "http://127.0.0.1:11434",
                             model or "deepseek-r1:70b", msgs,
                             temperature=0.5, timeout=900)
    except Exception as e:
        return {"ok": False, "error": "口播稿生成失败：%s" % e}
    ans = (ans or "").strip()
    if not ans:
        return {"ok": False, "error": "口播稿为空"}
    # 顺手切块，前端可直接开播
    prep = prepare(text=ans, title="口播稿", max_chunks=120)
    if not prep.get("ok"):
        return {"ok": False, "error": "口播稿切块失败"}
    prep.update({"ok": True, "script": ans, "pages": pages,
                 "source_chars": len(src)})
    return prep
