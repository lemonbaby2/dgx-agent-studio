# -*- coding: utf-8 -*-
"""本地生图能力（ComfyUI 后端）

设计要点：
  1. ComfyUI 跑在 127.0.0.1:8191（8188 被 ipcamera_lan_bridge 占了，别再抢）
  2. 中文提示词 sd 系模型看不懂 —— 先用本地 Ollama 翻成英文再送模型
  3. 出图直接落到 web/static/gen/ 下，前端用 /static/gen/xxx.png 就能显示
  4. 全程不依赖任何云端密钥，断网可用
"""
import glob
import json
import os
import random
import time
import urllib.parse
import urllib.request

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = (os.environ.get("COMFY_URL") or "http://127.0.0.1:8191").rstrip("/")
CKPT_DIR = "/home/gpu/ComfyUI/models/checkpoints"
OUT_DIR = os.path.join(APP_DIR, "web", "static", "gen")
OUT_URL_PREFIX = "/static/gen"

# 翻译用的本地模型
TRANS_MODEL = os.environ.get("COMFY_TRANSLATE_MODEL", "qwen3-coder:30b")

_CJK = ("\u4e00", "\u9fff")


def _opener():
    # 本机代理会把 127.0.0.1 打成 000，必须 all 显式绕过
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(url, data=None, timeout=120, method=None):
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def has_cjk(s):
    return any(c >= _CJK[0] and c <= _CJK[1] for c in (s or ""))


def pick_checkpoint():
    """优先 SDXL-Turbo > sd-turbo > 任意现成的"""
    try:
        files = sorted(os.path.basename(p) for p in
                       glob.glob(os.path.join(CKPT_DIR, "*.safetensors")))
    except Exception:
        files = []
    if not files:
        return ""
    for kw in ("sd_xl_turbo", "sdxl_turbo", "sd_turbo", "turbo"):
        for f in files:
            if kw in f.lower():
                return f
    return files[0]


def _is_turbo(ckpt):
    return "turbo" in (ckpt or "").lower()


def _is_xl(ckpt):
    c = (ckpt or "").lower()
    return "xl" in c or "sdxl" in c


def status():
    """ComfyUI 是否在线 + 有哪些模型"""
    out = {"ok": False, "comfy": COMFY, "checkpoints": [],
           "current": pick_checkpoint()}
    try:
        st = _http(COMFY + "/system_stats", timeout=8)
        out["ok"] = True
        out["device"] = ((st.get("devices") or [{}])[0]).get("name") or ""
        out["vram_total"] = ((st.get("devices") or [{}])[0]).get("vram_total")
    except Exception as e:
        out["error"] = str(e)[:200]
    try:
        out["checkpoints"] = sorted(os.path.basename(p) for p in
                                    glob.glob(os.path.join(CKPT_DIR, "*.safetensors")))
    except Exception:
        pass
    return out


def translate(prompt, timeout=60):
    """中文提示词 → 英文（SD 系模型只认英文）；失败则原样返回"""
    if not has_cjk(prompt):
        return prompt, False
    try:
        from . import providers as pv
        sys_msg = ("You are a prompt translator for Stable Diffusion. "
                   "Translate the user's Chinese description into a concise "
                   "English image-generation prompt. Keep subject, style, colors, "
                   "composition, lighting. Output ONLY the English prompt, "
                   "comma-separated keywords, no quotes, no explanation.")
        txt, _th, _tc = pv.chat_once(
            "ollama", TRANS_MODEL,
            [{"role": "system", "content": sys_msg},
             {"role": "user", "content": prompt}],
            temperature=0.2, timeout=timeout, max_tokens=300)
        txt = (txt or "").strip().strip('"').strip()
        # 去掉模型有时带的前缀
        for pre in ("English prompt:", "Prompt:", "英文提示词："):
            if txt.lower().startswith(pre.lower()):
                txt = txt[len(pre):].strip()
        return (txt or prompt), bool(txt)
    except Exception:
        return prompt, False


def _workflow(ckpt, pos, neg, w, h, steps, cfg, seed, sampler, scheduler):
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": pos, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": neg, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": int(w), "height": int(h), "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": int(seed), "steps": int(steps), "cfg": float(cfg),
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": 1.0, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"images": ["6", 0], "filename_prefix": "dgs"}},
    }


def generate(prompt, negative="", width=0, height=0, steps=0, seed=None,
             timeout=300, do_translate=True):
    """出图。返回 {ok, path, url, seconds, prompt_en, ckpt, ...}"""
    t0 = time.time()
    if not (prompt or "").strip():
        return {"ok": False, "error": "提示词为空"}
    st = status()
    if not st.get("ok"):
        return {"ok": False,
                "error": "ComfyUI 未就绪（%s）。检查：systemctl --user status comfyui"
                         % st.get("error", "")}

    ckpt = pick_checkpoint()
    if not ckpt:
        return {"ok": False, "error": "checkpoints 目录里没有模型文件"}

    turbo = _is_turbo(ckpt)
    xl = _is_xl(ckpt)

    # 提示词英译
    prompt_en, translated = (prompt, False)
    if do_translate:
        prompt_en, translated = translate(prompt)

    # 参数默认值按模型档位来
    if not width or not height:
        width, height = (1024, 1024) if xl else (512, 512)
    if not steps:
        steps = 4 if turbo else 25
    cfg = 1.0 if turbo else 7.0
    sampler = "euler_ancestral" if turbo else "dpmpp_2m"
    scheduler = "normal" if turbo else "karras"
    if seed is None:
        seed = random.randint(1, 2 ** 31 - 1)

    wf = _workflow(ckpt, prompt_en, negative or
                   "lowres, blurry, watermark, text, deformed, extra limbs",
                   width, height, steps, cfg, seed, sampler, scheduler)

    try:
        r = _http(COMFY + "/prompt", data=json.dumps({"prompt": wf}).encode("utf-8"),
                  timeout=60)
    except Exception as e:
        return {"ok": False, "error": "提交任务失败：%s" % str(e)[:200]}
    pid = r.get("prompt_id")
    if not pid:
        return {"ok": False, "error": "ComfyUI 拒绝了任务：%s"
                % json.dumps(r, ensure_ascii=False)[:300]}

    # 轮询结果
    imgs = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            h = _http(COMFY + "/history/" + pid, timeout=20)
        except Exception:
            h = {}
        item = (h or {}).get(pid) or {}
        outs = (item.get("outputs") or {})
        for _nid, o in outs.items():
            for im in (o.get("images") or []):
                if im.get("filename"):
                    imgs.append(im)
        if imgs:
            break
        if (item.get("status") or {}).get("status_str") == "error":
            return {"ok": False, "error": "ComfyUI 执行出错"}
        time.sleep(0.6)

    if not imgs:
        return {"ok": False, "error": "出图超时（%.0fs）" % (time.time() - t0)}

    # 取回图片并落到 web/static/gen/ 下
    im = imgs[0]
    q = urllib.parse.urlencode({"filename": im["filename"],
                                "subfolder": im.get("subfolder", ""),
                                "type": im.get("type", "output")})
    try:
        with _opener().open(COMFY + "/view?" + q, timeout=60) as resp:
            blob = resp.read()
    except Exception as e:
        return {"ok": False, "error": "取图失败：%s" % str(e)[:200]}

    os.makedirs(OUT_DIR, exist_ok=True)
    name = "gen_%s_%d.png" % (time.strftime("%Y%m%d_%H%M%S"), int(time.time() * 1000) % 1000)
    path = os.path.join(OUT_DIR, name)
    with open(path, "wb") as f:
        f.write(blob)

    return {"ok": True, "path": path,
            "url": "%s/%s" % (OUT_URL_PREFIX, name),
            "file": name, "bytes": len(blob),
            "prompt_zh": prompt, "prompt_en": prompt_en,
            "translated": translated, "ckpt": ckpt,
            "size": "%dx%d" % (width, height), "steps": steps, "seed": seed,
            "seconds": round(time.time() - t0, 2)}
