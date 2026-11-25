"""DGX Agent Studio v15 autonomous execution acceptance test."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "tools" else HERE
sys.path.insert(0, ROOT)

from server import agent_tools


def main():
    messages = [
        {
            "role": "system",
            "content": (
                "你是执行型智能体。必须亲自调用工具完成任务并验证结果；"
                "禁止让用户去终端运行命令。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请自主完成：在当前项目的 papers 目录下载论文 "
                "https://arxiv.org/pdf/2305.10453.pdf，保存为 2305.10453.pdf；"
                "然后检查文件大小和 PDF 文件头，确认确实下载成功。"
            ),
        },
    ]
    result = agent_tools.run_with_tools(
        "ollama",
        "qwen3-coder:30b",
        messages,
        use_tools=True,
        temperature=0,
        max_rounds=8,
        timeout=300,
    )
    candidates = [
        os.path.join(ROOT, "papers", "2305.10453.pdf"),
        "/home/gpu/Desktop/workspace/papers/2305.10453.pdf",
    ]
    target = next((path for path in candidates if os.path.isfile(path)), candidates[0])
    exists = os.path.isfile(target)
    size = os.path.getsize(target) if exists else 0
    header = b""
    if exists:
        with open(target, "rb") as handle:
            header = handle.read(5)
    summary = {
        "tools": [
            {
                "name": item.get("name"),
                "ok": item.get("ok"),
                "returncode": (item.get("result") or {}).get("returncode"),
            }
            for item in result.get("tools") or []
        ],
        "rounds": result.get("rounds"),
        "answer": result.get("content"),
        "file": target,
        "exists": exists,
        "size": size,
        "header": header.decode("ascii", "replace"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not (exists and size > 10000 and header == b"%PDF-"):
        raise SystemExit(2)
    if sum(1 for item in result.get("tools") or [] if item.get("ok")) < 2:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
