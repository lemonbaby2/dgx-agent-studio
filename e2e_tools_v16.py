"""Acceptance checks for verified downloads and client-side file opening."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from server import agent_tools

TARGET = "/home/gpu/Desktop/workspace/stm32_manuals/RM0090_STM32F407.pdf"
URL = "https://www.st.com/resource/en/reference_manual/rm0090-stm32f4xx-reference-manual-stmicroelectronics.pdf"


def main():
    downloaded = agent_tools.dispatch("download_file", {"url": URL, "path": TARGET})
    opened = agent_tools.dispatch("open_file", {"path": TARGET, "mode": "preview"})
    bridged = agent_tools.dispatch("run_cmd", {"command": "xdg-open %s" % TARGET})
    result = {"download": downloaded, "open": opened, "xdg_bridge": bridged}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if downloaded.get("error") or downloaded.get("bytes", 0) < 100000:
        raise SystemExit(2)
    if not str(downloaded.get("header") or "").startswith("%PDF-"):
        raise SystemExit(3)
    for item in (opened, bridged):
        if item.get("action") != "preview" or item.get("path") != TARGET:
            raise SystemExit(4)


if __name__ == "__main__":
    main()
