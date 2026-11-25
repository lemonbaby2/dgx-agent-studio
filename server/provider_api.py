# -*- coding: utf-8 -*-
"""供应商管理 REST（v11）—— 豆包 / 千问 / 各家 Key 的配置与体检"""
import time

from . import providers as pv

try:
    from . import agent_tools
except Exception:
    agent_tools = None

_last_test = {}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def list_providers():
    """供应商清单 + 本地模型 + 每个云厂商的配置状态"""
    items = pv.provider_list()
    try:
        ollama_models = pv.list_ollama_models(timeout=10)
    except Exception:
        ollama_models = []
    for it in items:
        if it["id"] == "ollama":
            it["models"] = ollama_models
            it["online"] = bool(ollama_models)
        it["last_test"] = _last_test.get(it["id"]) or None
    return {"ok": True, "providers": items,
            "cred_file": pv.CRED_FILE}


def set_cred(pid, key=None, base=None):
    """写入密钥/地址。永远只回掩码"""
    if pid not in pv.PROVIDERS:
        return {"ok": False, "error": "未知供应商 %s" % pid}
    if key is not None:
        pv.set_key(pid, key)
    if base is not None:
        pv.set_base(pid, base)
    return {"ok": True, "id": pid, "key_mask": pv.mask(key or pv.get_key(pid)),
            "base": pv.get_base(pid)}


def delete_key(pid):
    if pid not in pv.PROVIDERS:
        return {"ok": False, "error": "未知供应商 %s" % pid}
    pv.set_key(pid, "")
    return {"ok": True, "id": pid, "cleared": True}


def test(pid, model=None, timeout=45):
    """真实连通测试（会花一次最少的 token）"""
    r = pv.test_provider(pid, model, timeout=timeout)
    r["ts"] = _now()
    _last_test[pid] = {"ok": bool(r.get("ok")), "model": model or "",
                       "ts": r["ts"],
                       "error": (r.get("error") or "")[:150]}
    return r


def models(pid, refresh=False, timeout=25):
    """拉该供应商可用模型清单"""
    if pid == "ollama":
        ms = pv.list_ollama_models(timeout=timeout)
        return {"ok": bool(ms), "models": ms, "source": "local"}
    ok, res = pv.list_cloud_models(pid, timeout=timeout)
    if ok:
        return {"ok": True, "models": res, "source": "cloud", "count": len(res)}
    return {"ok": False, "error": str(res)[:300], "models": []}


def tools():
    """Agent 工具清单"""
    if not agent_tools:
        return {"ok": False, "error": "工具层未加载", "tools": []}
    return {"ok": True, "tools": agent_tools.TOOLS,
            "names": agent_tools.tool_names(),
            "max_rounds": agent_tools.MAX_ROUNDS}
