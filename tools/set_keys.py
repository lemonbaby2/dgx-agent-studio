#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一密钥注入工具（DGX 侧运行）

为什么需要它：
  providers.json 只在 Studio 主进程里被读取。agent 自己 spawn 出去的
  子进程（skill、终端命令、工具回路、自学习脚本）看不到里面的 key，
  导致「自主串联」到云端模型时全部 401。

  本工具把同一个 key 写到三个地方：
    1. ~/.dgx_studio/providers.json      ← Studio 主进程按请求读，改完立即生效
    2. ~/.dgx_studio/credentials.env     ← 任何 shell / python 子进程 source 即得
    3. systemd drop-in keys.conf         ← Studio 及其所有子孙进程继承（需 --restart）

用法:
  python3 tools/set_keys.py --list
  python3 tools/set_keys.py --ark  <火山方舟KEY>
  python3 tools/set_keys.py --ark K1 --qwen K2 --deepseek K3 --moonshot K4 --zhipu K5 --openai K6
  python3 tools/set_keys.py --from-file /path/to/keys.env
  python3 tools/set_keys.py --check              # 对每个已配 key 真发一次 /models
  python3 tools/set_keys.py --ark K1 --check --restart
  python3 tools/set_keys.py --del ark            # 清除某家密钥
"""
import argparse
import json
import os
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

try:
    from server import providers as pv
except Exception as e:
    print("[FATAL] 无法导入 server.providers: %s" % e)
    sys.exit(2)

DATA_DIR = os.path.expanduser("~/.dgx_studio")
ENV_FILE = os.path.join(DATA_DIR, "credentials.env")
DROPIN_DIR = os.path.expanduser(
    "~/.config/systemd/user/dgx-agent-studio.service.d")
DROPIN = os.path.join(DROPIN_DIR, "keys.conf")
SERVICE = "dgx-agent-studio"

# CLI 参数名 -> provider id
ARG2PID = {
    "ark": "ark",
    "qwen": "qwen",
    "deepseek": "deepseek",
    "moonshot": "moonshot",
    "zhipu": "zhipu",
    "openai": "openai",
    "custom": "custom",
}
# 每家对应的环境变量名（写 credentials.env / systemd 用）
PID2ENV = {pid: p["key_env"] for pid, p in pv.PROVIDERS.items() if p.get("key_env")}
PID2BASE_ENV = {pid: p["base_env"] for pid, p in pv.PROVIDERS.items()
                if p.get("base_env")}


def _ensure(path, mode=0o600):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def write_env_file(creds):
    """把已配的 key 落成 shell 可 source 的 credentials.env"""
    keys = creds.get("keys") or {}
    bases = creds.get("bases") or {}
    lines = [
        "# 由 tools/set_keys.py 自动生成，勿手改",
        "# 用法:  set -a; . ~/.dgx_studio/credentials.env; set +a",
        "",
    ]
    for pid in sorted(keys):
        env = PID2ENV.get(pid)
        if env:
            lines.append('export %s="%s"' % (env, keys[pid].replace('"', '\\"')))
    for pid in sorted(bases):
        benv = PID2BASE_ENV.get(pid)
        if benv:
            lines.append('export %s="%s"' % (benv, bases[pid].replace('"', '\\"')))
    lines.append("")
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, ENV_FILE)
    try:
        os.chmod(ENV_FILE, 0o600)
    except Exception:
        pass
    return ENV_FILE


def write_dropin(creds):
    """写 systemd drop-in，让 Studio 及其所有子孙进程继承 key"""
    keys = creds.get("keys") or {}
    bases = creds.get("bases") or {}
    os.makedirs(DROPIN_DIR, exist_ok=True)
    lines = ["[Service]"]
    for pid in sorted(keys):
        env = PID2ENV.get(pid)
        if env:
            lines.append('Environment="%s=%s"' % (env, keys[pid]))
    for pid in sorted(bases):
        benv = PID2BASE_ENV.get(pid)
        if benv:
            lines.append('Environment="%s=%s"' % (benv, bases[pid]))
    # 子进程也统一默认直连，避免本机代理把请求打成 000
    lines.append('Environment="NO_PROXY=*"')
    with open(DROPIN, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(DROPIN, 0o600)
    except Exception:
        pass
    return DROPIN


def do_restart():
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "restart", SERVICE]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        print("  $ %s -> rc=%d %s" % (" ".join(cmd), r.returncode,
                                      (r.stderr or "").strip()[:200]))
        if r.returncode != 0:
            return False
    r = subprocess.run(["systemctl", "--user", "is-active", SERVICE],
                       capture_output=True, text=True)
    print("  service = %s" % (r.stdout or "").strip())
    return "active" in (r.stdout or "")


def show_list():
    creds = pv.load_creds()
    keys = creds.get("keys") or {}
    print("%-10s %-20s %-10s %s" % ("ID", "名称", "状态", "掩码 / 说明"))
    print("-" * 78)
    for pid, p in pv.PROVIDERS.items():
        k = keys.get(pid) or (os.environ.get(p["key_env"]) if p.get("key_env") else "")
        if not p["needs_key"]:
            state, info = "无需", p["hint"][:36]
        elif k:
            state, info = "已配", pv.mask(k)
        else:
            state, info = "缺失", "未配置密钥"
        print("%-10s %-20s %-10s %s" % (pid, p["name"][:18], state, info))
    print()
    print("密钥文件: %s" % pv.CRED_FILE)
    print("环境文件: %s" % ENV_FILE)
    print("systemd : %s" % DROPIN)


def show_check():
    creds = pv.load_creds()
    have = [pid for pid in (creds.get("keys") or {}) if pid in pv.PROVIDERS]
    if not have:
        print("没有配置任何云端密钥，无从校验。")
        return
    for pid in have:
        print("== %s (%s)" % (pid, pv.PROVIDERS[pid]["name"]))
        ok, res = pv.list_cloud_models(pid, timeout=30)
        if ok:
            print("   ✅ 可用模型 %d 个，前 8 个：%s" % (len(res), ", ".join(res[:8])))
        else:
            print("   ❌ %s" % res)
        t = pv.test_provider(pid, timeout=45)
        if t.get("ok"):
            print("   对话连通 ✅ %ss 回复=%r" % (t.get("elapsed"), t.get("reply")))
        else:
            print("   对话 ❌ %s %s" % (t.get("error"), t.get("tip") or ""))


def parse_env_file(path):
    out = {}
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("export "):
                s = s[7:].strip()
            if "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main():
    ap = argparse.ArgumentParser(add_help=True)
    for name in ARG2PID:
        ap.add_argument("--" + name, metavar="KEY", help="%s 的密钥" % name)
    ap.add_argument("--from-file", metavar="PATH", help="从 .env 批量导入")
    ap.add_argument("--del", dest="delete", metavar="PID", help="清除某家密钥")
    ap.add_argument("--list", action="store_true", help="查看当前状态")
    ap.add_argument("--check", action="store_true", help="真发请求校验")
    ap.add_argument("--restart", action="store_true", help="写 systemd drop-in 并重启服务")
    a = ap.parse_args()

    changed = False

    if a.from_file:
        env = parse_env_file(a.from_file)
        for pid, envname in PID2ENV.items():
            if env.get(envname):
                pv.set_key(pid, env[envname])
                print("  导入 %-9s %s" % (pid, pv.mask(env[envname])))
                changed = True

    for argname, pid in ARG2PID.items():
        val = getattr(a, argname, None)
        if val:
            val = val.strip()
            if len(val) < 8:
                print("  ⚠️ %s 的 key 只有 %d 位，疑似不完整，仍写入" % (pid, len(val)))
            pv.set_key(pid, val)
            print("  写入 %-9s %s" % (pid, pv.mask(val)))
            changed = True

    if a.delete:
        if a.delete not in pv.PROVIDERS:
            print("未知供应商 %s" % a.delete)
        else:
            pv.set_key(a.delete, "")
            # 同步清掉本进程环境变量，否则状态表会从 env 回落显示出"已配"假象
            envname = PID2ENV.get(a.delete)
            if envname:
                os.environ.pop(envname, None)
            print("  已清除 %s" % a.delete)
            changed = True

    if changed:
        creds = pv.load_creds()
        f1 = write_env_file(creds)
        print("\n已同步环境文件: %s" % f1)
        if a.restart:
            f2 = write_dropin(creds)
            print("已写 systemd drop-in: %s" % f2)
            print("重启服务中…")
            do_restart()
        else:
            print("提示：主进程按请求实时读 providers.json，**不用重启即生效**；")
            print("      若要让 skill / 子进程也继承，加 --restart 写 drop-in。")

    print()
    show_list()
    if a.check:
        print()
        show_check()


if __name__ == "__main__":
    main()
