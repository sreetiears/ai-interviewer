# AI Interviewer (考研复试 AI 智能面试系统)

基于 **LiveKit 实时音频流** + **LangGraph 状态机** + **SiliconFlow 语音合成与识别 (STT / TTS)** 构建的考研复试模拟面试系统。

---

## 🌟 核心改进与特性

1. **安全配置与零硬编码**：
   - 移除代码中所有硬编码的 API Key 与 Base URL，统一由 `.env` 及 `config.py` 管理。
   - 提供标准环境配置模板 `.env.example`。

2. **LangGraph 核心状态机驱动**：
   - 彻底将考研复试全流程重构为 LangGraph 状态图 (`interview_graph.py`)，实现面试全生命周期管理：
     - **自我介绍阶段 (`self_intro`)**：智能审查考生介绍完整度，未达标时引导补充，合格后自动推进；
     - **专业知识问答 (`pro_qa`)**：出题、评估深度并决定是否一针见血追问（支持限制追问轮次）；
     - **综合素质考察 (`compre_qa`)**：考察读研动机、软技能与抗压能力，进行场景化探底；
     - **评分报告生成 (`report`)**：复试结束输出多维度数字化成绩单、核心不足与改进建议。
   - 通过 `langgraph_adapter.py` 无缝对接 LiveKit Agent 流式输出。
   - 保留向后兼容 Dify 模式（通过 `LLM_BACKEND=dify` 即可自由切换）。

3. **完全免费、开箱即用的个性化定制前端**：
   - 摆脱 livekit 官方前端需手工通过 CLI 粘贴密钥的限制；
   - 采用 LiveKit 官方开源免费浏览器 SDK (`livekit-client` JS UMD)，纯原生 HTML5/CSS3/JS，无需复杂前端构建工具；
   - 后端轻量级服务 (`server.py`) 提供一键 `/api/token` 免密入场接口；
   - 现代化沉浸式深色面试考场界面：考官/考生双头像发声光圈波纹动效、面试阶段指示器（1.自我介绍 -> 2.专业问答 -> 3.综合素质 -> 4.成绩单）、实时文字记录与一键麦克风开启。

---

## 🛠️ 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux / WSL:
# source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env` 并填入相应密钥：

```bash
cp .env.example .env
```

核心配置项说明：
```ini
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=secret

# LLM 核心驱动 (langgraph 或 dify)
LLM_BACKEND=langgraph
LLM_API_KEY=your_llm_api_key_here
LLM_BASE_URL=https://api.siliconflow.cn/v1
LLM_MODEL=Qwen/Qwen2.5-7B-Instruct

# 语音 STT / TTS (SiliconFlow)
SILICONFLOW_API_KEY=your_siliconflow_api_key_here
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
STT_MODEL=FunAudioLLM/SenseVoiceSmall
TTS_MODEL=FunAudioLLM/CosyVoice2-0.5B
TTS_VOICE=FunAudioLLM/CosyVoice2-0.5B:alex
```

### 3. 运行自动化测试

本项目配有完整的单元测试与状态机模拟测试（无需真实联网消耗 API Key）：

```bash
pytest tests/
```

### 4. 本地启动运行

#### 终端 1：启动 LiveKit Server
```bash
livekit-server --dev
```

#### 终端 2：启动定制前端与入场服务
```bash
python server.py
```
浏览器直接访问：`http://localhost:8080`，点击**“进入考场”**即可自动生成 Token 加入房间并开启语音。

#### 终端 3：启动 AI 面试官 Agent
```bash
python agent.py connect --room ai-room
```

---

## 📁 项目工程架构

```
.
├── agent.py                 # LiveKit 语音交互 Agent 入口
├── config.py                # 集中化配置管理 (零硬编码)
├── interview_graph.py       # LangGraph 考研复试状态图与节点逻辑
├── langgraph_adapter.py     # LangGraph 与 LiveKit 流式 LLM 适配器
├── server.py                # 定制前端 HTTP 服务 & Token 自动签发 API
├── frontend/
│   └── index.html           # 定制化深色沉浸式考研复试 Web 前端
├── tests/
│   ├── test_graph_nodes.py      # LangGraph 各阶段状态与跳转测试
│   ├── test_graph_structure.py  # 状态图路由编译测试
│   └── test_server.py           # Token 签发与接口测试
├── requirements.txt         # 项目依赖库清单
├── .env.example             # 环境变量配置模板
└── README.md                # 项目文档
```
