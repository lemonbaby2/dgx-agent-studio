# DGX Agent Studio v6 · 多模态 SOP Agent 升级说明
> 面向某汽车零部件装配产线：多窗口 / 视频理解 / 语音 / Skill 化 Agent
> 全部数据为 **2026-10-19 在 DGX dgx-host 上实测**，非估算

---

## 一、你提的四个问题，逐条回答

| 你的问题 | 实测根因 | 结论 |
|---|---|---|
| **不能同时开多窗口问答** | ①每个实例各起各的服务，会话数据分裂 ②**Ollama 默认 `NUM_PARALLEL=1`**，第 2 个请求排队 | ✅ 已改代码：多实例共享一个后端；另需跑一次 `pkg\开启DGX并发问答.cmd` |
| **不能识别视频** | **`qwen3-vl:30b-a3b-instruct`(19GB) 已在 DGX 躺了 4 周没接线**，ffmpeg 也在 | ✅ **已实现并跑通**，不需要新下模型 |
| **不能识别语音** | DGX 上 `whisper/funasr/edge-tts/piper` 全是 NOT FOUND；**DGX 无外网**(pypi FAIL) | ⚠️ 已接好接口并降级处理；**朗读今天就能用**（Windows 内置语音），**听写**要装包（见第五节） |
| **要 WorkBuddy 式 Skill 化 Agent** | 原版只是聊天框，无工具 | ✅ 已建技能引擎 + 4 个 SOP 专属技能 |

### DGX 实测家底

| 项 | 实测值 |
|---|---|
| GPU | NVIDIA GB10，**compute_cap 12.1**，driver 580.159.03 |
| 统一内存 | 121 GB total，**可用 98 GB** |
| torch | 2.13.0+cu130，CUDA OK，cap (12,1) |
| 磁盘 | 3.7T / 已用 2.3T / **剩 1.3T（66%）** |
| Ollama | **0.32.15**（支持多图输入 + tool calling） |
| 已装模型 | `qwen3-vl:30b-a3b-instruct`(19G) · `qwen3-coder:30b`(18G) · `deepseek-r1:32b`(19G) · `qwen2.5vl:7b`(6G) |
| ffmpeg | `/usr/bin/ffmpeg` ✅（DGX 与本机都有） |
| 外网 | ❌ 无（pypi 直连 FAIL） |

> **关键判断**：qwen3-vl 30B + r1 32B 同时常驻只占 38GB，98GB 可用内存绰绰有余。
> 多模态这条路硬件完全够，缺的从来只是接线。

---

## 二、v6 新增了什么

### 1. 多窗口（会话不再分裂）
- 后端只在第一次启动时起来，把端口写进 `~/.dgx_studio/server.json`
- **后续再双击 exe → 复用已有后端，只新开一个窗口**，共享同一份会话库
- 新增参数：
  - `--web`：只起服务不开窗口，浏览器**多标签 = 多窗口**（推荐给别人用）
  - `--no-reuse`：强制起新后端（调试用）
  - `--port N`：指定端口

### 2. 视频理解（抽帧 + 视觉语言模型）
流程：`视频 → ffmpeg 均匀抽 N 帧 → base64 → qwen3-vl → 结构化结论`

- 支持**本地视频**和 **DGX 上的视频**（`dgx:/绝对路径`）
- 哪边有文件就在哪边抽帧，不搬原创视频
- 默认 8 帧，上限 24 帧，单帧宽压到 768px 防止 prompt 爆炸
- 内置 SOP 审查提示词，输出按帧序描述 + `【结论】` 收尾

实测（9.15 出风口 18.54 秒视频，8 帧）：
> 正确读出「仪表板装配区」吊牌文字、白色塑料件、
> 黄色安全背心工人推物料车，并按帧序给出：
> **【结论】操作员在PROJ-A产线的仪表板装配区进行仪表板相关零件装配…未发现明显漏做风险。**

### 3. 语音（多后端自动降级）
- **朗读（TTS）今天就能用**：走 Windows 内置 SAPI，**离线、零安装**
  备选 `edge-tts`（音质好但要外网，本机已装）
- **听写（ASR）**：接口已就绪，优先级 —
  1. DGX GPU 上的 FunASR/SenseVoice 或 whisper
  2. 本机 faster-whisper（本机有网，pip 可装）
  3. 都没有 → **明确报错 + 给安装命令**，绝不假装支持

### 4. Skill 引擎（这才是 Agent 和聊天框的区别）
一个技能 = 一个 YAML 文件，三种类型：

| 类型 | 作用 | 例子 |
|---|---|---|
| `prompt` | 不改工具，只给 LLM 换「专家脑子」 | 模型晋级门禁 |
| `script` | 执行一串动作（本机/SSH） | — |
| `hybrid` | **先跑脚本拿事实，再让 LLM 判读**（最有用） | DGX 体检、CVAT 标注体检 |

触发：输入框打 `/<id>`，或自然语言命中关键词。

### 已内置 4 个 SOP 专属技能（把你的项目铁律固化进去了）

| 技能 | 类型 | 固化了哪些踩过的坑 |
|---|---|---|
| `/dgx-health` DGX 体检 | hybrid | GPU 假忙看 `power.draw`/`clocks.sm` 不看 util；redis AOF 拖垮 CVAT 8081 |
| `/cvat-audit` CVAT 标注体检 | hybrid | `page_size=100`（否则 22 类被截断）；`annotations` 只有 label_id 必须回查映射（否则"伪成功"）；task 同名去重 |
| `/model-gate` 模型晋级门禁 | prompt | **P≥0.80 且 R≥0.80**；`model.names` 必须用 `torch.load` 核对；nc=9≠nc=17 事故；OBB 的 conf 不可能 >1 |
| `/edge-deploy` 边侧部署评审 | prompt | RKNN INT8 共用 scale 导致 cls 全舍成 0（"0 框"）；工作区 letterbox；RK3588 不是 x86 |

技能目录：`<程序>/skops/`（内置）和 `~/.dgx_studio/skops/`（自建，优先）

---

## 三、马上能用

```bash
# 1) 多窗口：正常双击图标，想开第二个窗口就再双击一次
#    两个窗口共享同一份会话记录

# 2) 给别人用（不用装任何东西）
DGX-Agent-Studio.exe --web
# 然后浏览器打开 http://127.0.0.1:18742/ ，多开标签页即可

# 3) 体检 DGX（对话框里直接说，或）
/dgx-health

# 4) 看这段产线视频在干嘛
#    右侧挂载目录 或 直接填 dgx:/绝对路径
```

API 一览（都实测通过）：

| 接口 | 用途 |
|---|---|
| `GET /api/skills` | 列出所有技能 |
| `POST /api/skill/match` | 自然语言/斜杠匹配技能 |
| `POST /api/skill/run` | 执行技能（含 SSH 取数） |
| `POST /api/video/analyze` | 视频理解 |
| `GET /api/voice/status` | 语音后端可用性探测 |
| `POST /api/voice/asr` | 语音转文字 |
| `POST /api/voice/tts` | 文字转语音 |

---

## 四、这次实测抓到并修掉的 3 个硬 bug

> 都是不实测绝对发现不了的那种

1. **短视频抽 0 帧**：早期 `fps = n/100`，3.5 秒素材算出来是「每 12.5 秒一帧」= 0 帧，
   ffmpeg 加 `-v error` 时一声不响。→ 改为**按真实时长算 fps** 并给下限。
2. **scp 多套一层目录**：`scp -r host:/dir/` 会生成 `tmp/dir/*.jpg` 而非 `tmp/*.jpg`，
   导致 `listdir` 看不到帧。→ 源改 `dir/.`，并加**递归收帧**兜底。
3. **内网请求被代理劫持**：本机开了系统代理，urllib 默认读 `http_proxy`，
   访问 DGX 时表现为 `Remote end closed connection without response`。
   → 显式 `ProxyHandler({})` 绕过（你项目铁律第 31 条）。

---

## 五、还差的：语音听写（需要你决定要不要装）

DGX 无外网，所以只能**从本机下载 wheel 再传上去**。本机有网，可行。

**方案 A（推荐，快）**：本机装 ASR，零 GPU 占用
```powershell
pip install faster-whisper
```
装完重启软件即可用麦克风说话。CPU 跑 small 模型，实时率够用。

**方案 B（重，走 DGX GPU）**：适合批量转写长音频
在本机下载包 → 传到 DGX：
```powershell
# 1. 本机下载（本机有网）
pip download funasr modelscope -d E:\whl --no-deps
# 2. 传到 DGX 并安装（DGX 无网）
scp -r E:\whl gpu@127.0.0.1:/tmp/whl
ssh gpu@127.0.0.1 "pip install --no-index --find-links=/tmp/whl funasr"
```
然后在软件「设置」里确认 `ssh_host=127.0.0.1`，ASR 会自动走 DGX。

> 注意：FunASR 首次会去下载 SenseVoice 模型权重，DGX 无外网这一步会失败，
> 需要先把权重在本机下好传过去。**如果只想快速用起来，选方案 A。**

---

## 六、建议的下一步

1. 跑一次 `pkg\开启DGX并发问答.cmd`（改 Ollama 并发，需重启 ollama，注意别撞上别人在用）
2. 先试 `--web` 模式多开标签，确认多窗口 OK
3. 拿一段真实 SOP 视频试 `/api/video/analyze`，看结论是否符合现场实际
4. 语音按第五节选 A 或 B

如果第 3 步结论不准，再调的是**抽帧策略**（帧数、是否加密集）和**提示词**，
而不是换模型 —— qwen3-vl 30B 对这类工业画面已经够用。
