# DGX Agent Studio

> 一个装在自己机器上的 **action 型 Agent 工作台**：把本地大模型（Ollama）、SSH 远程执行、文件系统、Git、视频理解、语音、论文阅读串成一条可执行链路——不是聊天框，是能动手的助手。

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Deps](https://img.shields.io/badge/dependencies-标准库%20only-brightgreen)](#为什么零依赖)
[![Backend](https://img.shields.io/badge/backend-Ollama%20SSE-000000?logo=ollama)](https://ollama.com/)
[![Skills](https://img.shields.io/badge/skills-45%20个%20YAML%20编排-8E6C3E)](skops/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## 它解决什么问题

市面上的 Agent 产品大多有两类毛病：要么只会在网页里聊天、够不到你的机器；要么要装一堆框架、跑起来比用起来还费劲。

DGX Agent Studio 走另一条路：**后端只用 Python 标准库**，前端是纯静态文件，窗口用 WebView2，模型走本机 Ollama。整包能压成一个 exe，双击就用，不需要 Node、不需要 Docker、不需要云。

它在有本地 GPU 的机器上尤其合适——模型不上传、数据不出门、断网可用。

| 维度 | 常见云端 Agent | DGX Agent Studio |
|---|---|---|
| 数据 | 上传到服务商 | **全本地**，GPU 上跑，不联网也能用 |
| 依赖 | 一堆框架与运行时 | **Python 标准库**，零第三方框架 |
| 动手能力 | 只回文字 | SSH 远程执行、读写文件、跑 Git、生成图 |
| 能力扩展 | 改代码 | 往 `skops/` 丢一个 YAML 就是一个新技能 |
| 多模态 | 看服务商脸色 | 视频理解、语音合成/听写、图片生成全在本地 |

---

## 架构

```
┌──────────────────────────────────────────────┐
│  web/  纯静态前端（无构建步骤）                 │
│  index.html · v17update.js · v16skills.js     │
└────────────────┬─────────────────────────────┘
                 │ HTTP + SSE 流式
┌────────────────▼─────────────────────────────┐
│  server/app.py  ThreadingHTTPServer           │
│  ├── core.py        配置 / Ollama 客户端      │
│  ├── skills.py      Skill 引擎（YAML 编排）    │
│  ├── agent_tools.py 工具集（SSH/Shell/Python） │
│  ├── fs_api.py      文件系统（含授权目录）      │
│  ├── git_api.py     Git 操作                  │
│  ├── video_api.py   视频理解（ffmpeg + VL 模型）│
│  ├── speech/voice   语音听写与合成             │
│  ├── image_api.py   图片生成（ComfyUI）        │
│  ├── paper_reader.py 论文解析与问答            │
│  ├── embodied_api.py 具身/机器人接口           │
│  ├── authz.py       权限与授权目录             │
│  └── capacity.py    容量与并发管理             │
└────────────────┬─────────────────────────────┘
                 │
        ┌────────▼────────┐        ┌──────────────┐
        │  Ollama (本机)   │        │  SSH 远程主机 │
        │  qwen3 / r1 / vl │        │  GPU 服务器   │
        └─────────────────┘        └──────────────┘
```

### 为什么零依赖

后端全部用 `http.server`、`urllib`、`json`、`threading` 这些标准库模块。好处很实际：

- 目标机器常常**没有外网**（GPU 服务器装包失败是常态），零依赖意味着不需要 pip
- 打包成 exe 时不用带一堆 wheel，体积可控
- 出问题只看自己的代码，不用排查框架

`pywebview` 是唯一的可选依赖——有它就用原生窗口，没有就退回系统浏览器，功能不减。

---

## Skill 引擎：45 个技能怎么组织

`skops/` 下每个 YAML 就是一个技能，不需要写 Python：

```yaml
id: dgx-health
name: DGX 体检
description: 一键体检 GPU、内存、磁盘、Ollama 与常驻服务
trigger: [dgx体检, 服务器体检, gpu状态, 显存, 磁盘满了]
keywords: [体检, 健康, gpu, 显存, 磁盘, 服务]
type: hybrid            # prompt / steps / hybrid
system: |
  你是运维专家。判读时遵守这些铁律：
  · GPU 是否真忙看 power.draw，不能只看 utilization（会撒谎）
  · 输出格式：先一句话结论，再分段列数字，最后给待办
prompt: |
  下面是实时体检数据，请判读：{{input}}
steps:
  - name: 磁盘余量
    kind: ssh
    cmd: "df -h /; du -sh /home/* | sort -rh | head -8"
    timeout: 60
  - name: 显存与功耗
    kind: ssh
    cmd: "nvidia-smi --query-gpu=utilization.gpu,memory.used,power.draw --format=csv"
```

三个字段决定行为：

| 字段 | 作用 |
|---|---|
| `type` | `prompt` 纯对话 / `steps` 纯执行 / `hybrid` 先采集再让模型判读 |
| `steps` | 采集步骤，`kind` 支持 `ssh` / `shell` / `python` / `http` |
| `{{input}}` | 步骤输出注入 prompt 的位置 |

匹配靠 `trigger`（精确意图）与 `keywords`（模糊召回）双层，命中即执行。

内置 45 个技能覆盖：主机体检、CVAT 审计、局域网运维、模型网关、代码修复、Agent 自演进、短剧流水线、算法开发、电路设计、文档导出、边缘部署、多模态视觉…

---

## 能力清单

| 能力 | 实现 | 说明 |
|---|---|---|
| 流式对话 | Ollama `/api/chat` SSE | 多实例共享同一后端，会话不分裂 |
| 工具调用 | Function Calling | 模型可主动调 SSH / 文件 / Git |
| 远程执行 | SSH | 授权目录白名单，越界即拒 |
| 文件系统 | `fs_api` | 读写删改，带路径校验 |
| Git | `git_api` | 状态、提交、日志，可跨仓操作 |
| 视频理解 | ffmpeg 抽帧 + VL 模型 | 不需额外下模型，复用已有 qwen3-vl |
| 语音 | Windows 内置 TTS + 可选听写 | 合成开箱可用；听写需装 whisper/funasr |
| 图片生成 | ComfyUI HTTP | 指向本机 ComfyUI 的 checkpoints 目录 |
| 论文阅读 | `paper_reader` | PDF 解析 + 分段问答 |
| 具身接口 | `embodied_api` | 机器人/仿真侧的统一入口 |
| 权限 | `authz` + `users` | 目录与操作级授权 |
| 容量 | `capacity` | 显存/并发/队列管理 |

---

## 快速开始

```bash
git clone https://github.com/lemonbaby2/dgx-agent-studio.git
cd dgx-agent-studio
python main.py
```

默认起在 `127.0.0.1:18910`，自动打开窗口。要求本机已装 [Ollama](https://ollama.com/) 并拉过至少一个模型：

```bash
ollama pull qwen3-coder:30b     # 代码
ollama pull deepseek-r1:70b     # 深推理
ollama pull qwen3-vl:30b        # 视觉/视频
```

### 配置

首次运行会在 `~/.dgx_studio/config.json` 生成配置。**仓库里的默认值已脱敏为占位地址**，实际使用时改成你自己的：

```json
{
  "base_url": "http://127.0.0.1:11434",
  "model": "deepseek-r1:70b",
  "ssh_host": "192.0.2.10",
  "ssh_user": "gpu",
  "workspaces": ["~/workspace"]
}
```

也可以用环境变量覆盖：

| 变量 | 作用 |
|---|---|
| `DGX_OLLAMA_NUM_CTX` | 上下文长度，默认 16384 |
| `DGX_OLLAMA_KEEP_ALIVE` | 模型驻留时长，默认 30m |
| `DGX_OLLAMA_THINK` | 是否输出思考过程，`1` 开启 |

### 打包成 exe

```bash
python -m PyInstaller DGX-Agent-Studio.spec --clean --noconfirm
```

---

## 目录结构

```
dgx-agent-studio/
├── main.py                  # 入口：DPI 适配 + 起服务 + 开窗口
├── server/
│   ├── app.py               # HTTP 路由与 SSE
│   ├── core.py              # 配置、Ollama 客户端、模型回落
│   ├── skills.py            # YAML 技能引擎
│   ├── agent_tools.py       # 工具集
│   ├── fs_api.py / git_api.py / image_api.py
│   ├── video_api.py / speech_api.py / voice_api.py
│   ├── paper_reader.py / embodied_api.py
│   ├── authz.py / users.py / capacity.py
│   └── provider_api.py / providers.py
├── web/                     # 纯静态前端
├── skops/                   # 45 个 YAML 技能
├── tools/                   # 辅助脚本
└── DGX-Agent-Studio.spec    # PyInstaller 配置
```

---

## 脱敏说明

本仓库是**脱敏公开版**，与作者本机运行版本存在以下差异，原因是内网拓扑与客户信息不适合公开：

| 项 | 处理 |
|---|---|
| 内网 IP | `192.168.1.x` → `127.0.0.1`（默认后端）或 `192.0.2.x`（RFC 5737 文档专用段） |
| 主机名 | 真实 DGX 主机名 → `dgx-host` / `dgx-host-2` |
| 用户 home | `/home/<真实用户>` → `/home/gpu` |
| 目录名 | 工作目录统一为 `workspace` |
| 客户标识 | 具体客户与产线编号 → 泛化表述 |
| 明文口令 | 已替换为 `REPLACE_ME` |

**这些替换不影响代码逻辑**，只影响默认连接目标。部署时按上一节改配置文件即可。

---

## 已知限制

- 语音听写在无外网的机器上需要手动装包（whisper / funasr），合成不受影响
- 视频理解依赖 ffmpeg，需自行安装
- 多窗口并发需要 Ollama 侧放开 `NUM_PARALLEL`（默认 1，第二个请求会排队）
- Windows 之外的平台没有实测，`pywebview` 与 DPI 适配是按 Windows 写的

---

## 许可

MIT，见 [LICENSE](LICENSE)。
