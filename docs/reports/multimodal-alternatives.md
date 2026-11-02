# 多模态替代方案报告（未应用）

- **日期**：2026-11-02
- **范围**：把当前 `STT(SenseVoice) → 文本 LLM(Qwen2.5-7B) → TTS(CosyVoice2)` 升级为更强的多模态系统。
- **状态**：**只给方案，不改代码**。仓库当前仍是级联文本链路。
- **结论先行**：
  1. 不建议整体替换为全双工语音到语音模型；它会削弱本项目最核心的“题量/阶段/台词可控”。
  2. 首选“级联主链路 + 多模态感知旁路”：保留 LangGraph 决策与逐字 TTS，把韵律/停顿/（可选）视频特征送进报告。
  3. 中期可试点“实时模型只负责听/理解，LangGraph 决策，TTS 说话”。
  4. 全语音到语音只适合另开“自由对话练习”模式。
- **合规提醒**：真实招聘/升学场景涉及高风险 AI 法规；视频默认关闭，不做情绪推断产品承诺。

---

## 1. 当前链路与盲区

```text
麦克风 → VAD → STT → 文本 → LangGraph → 文本 → TTS → 扬声器
```

| 维度 | 现状 | 丢失 |
| --- | --- | --- |
| 词汇内容 | SenseVoice 转写 | 口音/专业术语可能识别错 |
| 副语言 | 无 | 语速、停顿、音高、音量、填充词 |
| 视觉 | 无 | 眼神、表情、姿态 |
| 交互 | 固定 VAD 阈值 | 抢话、沉默、打断的自适应 |
| 评分 | 纯文本 | “表达流畅度/紧张度”只能靠词猜 |

当前 `interview_graph.py` 的成绩单三维度（专业知识、表达逻辑、综合素质）全部由文本推断。表达逻辑维度最需要副语言证据。

---

## 2. 候选方案总表

> 公开资料整理，**价格/延迟/语言支持以供应商控制台为准**；标注“未核实”的项不要写进产品承诺。

### 2.1 端到端 / 语音到语音

| 方案 | 形态/许可 | 中文 | 流式/双工 | 工具调用 | 接入 | 主要限制 |
| --- | --- | --- | --- | --- | --- | --- |
| **Qwen3-Omni**（2025-09） | 30B-A3B MoE，Apache-2.0 | 语音输入 19 语言、输出 10 语言，含中文 | 文本+语音实时流 | 有音频函数调用示例 | vLLM-Omni / DashScope | 自建门槛高；本地 vLLM 历史上仅 Thinker 可跑 |
| **Qwen3.8-Omni-Flash-Realtime**（阿里云，2026） | 托管实时 API | 中文优先，支持方言/克隆音色（以文档为准） | WebSocket / WebRTC / AOQ；全双工 | MCP + function calling + web search | DashScope | 云锁定；实时协议与普通 Chat Completions 不同 |
| **Qwen2.5-Omni**（2025-03） | 7B/3B，Apache-2.0 | 中文基准覆盖 | Thinker-Talker 流式 | 未明确 | vLLM（仅 Thinker）/ DashScope | 老一代，新项目优先 Qwen3 系 |
| **GLM-4-Voice**（2024-12） | 9B，代码 Apache-2.0、权重单独协议 | 中英端到端，支持方言/情绪/语速指令 | 解码器流式 | 未提及 | 本地 Demo/Docker | 需 CUDA；无官方 LiveKit 插件 |
| **GLM-Realtime**（智谱） | 托管 `glm-realtime-flash/air` | 文档中文；语言支持未明示 | WSS 实时音视频 | 支持（当前仅音频模式） | `wss://open.bigmodel.cn/api/paas/v4/realtime` | 延迟/价格/会话时长未公开 |
| **Step-Audio 2 mini**（2025-08） | 约 7B，Apache-2.0 | 中文 ASR CER 3.19；中文 S2S 基准强 | 流式推理；未明确全双工 | 支持（vLLM 解析器） | vLLM / Docker / StepFun | 完整版未开源，仅 mini 可自建 |
| **MiniCPM-o 2.6 / 4.5** | 8–9B，Apache-2.0 | 中英实时对话；4.5 明确全双工 | 连续音视频流 | 未提及 | HF/vLLM/SGLang/Ollama/llama.cpp | 4.5 硬件要求未公布；2.6 已被取代 |
| **GPT Realtime 2.1** | 托管 | 语言清单未给 | 语音到语音，WebRTC/WS | 函数、MCP、handoff | LiveKit OpenAI Realtime 插件 | 中文质量需实测；音频价格高 |
| **Gemini Live（3.8）** | 托管 | Live API 称 70 语言 | 音频到音频，WSS；图像 ≤1FPS | 函数调用、Google Search | LiveKit Gemini Live 插件 | 中文面试质量未单独核实 |
| **豆包实时语音 O2.0/SC2.0** | 托管 | 中文优先，克隆音色主要保证中文 | WSS 二进制，双向流式 | 文档未提及 | `wss://openspeech.bytedance.com/api/v3/realtime/dialogue` | 不支持视频；12K 上下文；价格未列 |

### 2.2 选型直觉

| 目标 | 优先看 |
| --- | --- |
| 中文质量 + 可自建 | Step-Audio 2 mini、Qwen3-Omni、MiniCPM-o |
| 快速上线、少运维 | GLM-Realtime、豆包、DashScope Qwen-Omni、Gemini Live |
| 工具调用生态成熟 | GPT Realtime、Gemini Live（中文需自测） |
| 成本敏感 | Gemini Live 的分钟价通常最低；自建 7B–9B 需单卡到多卡 |

**不要只看 benchmark**：面试场景的长尾是口音、专业术语、停顿、反问、沉默。必须用你自己的 30–50 条录音做横评。

---

## 3. LiveKit Agents 的集成现实

LiveKit 把实时模型分两类：

- **Turn-based**：等用户说完再回复，大多数模型属于此类。
- **Full-duplex**：边听边说，自行管理回合。LiveKit 文档明确指出这类模型**不能**使用自定义回合检测、**不能**外接独立 TTS、**不能**逐字念固定台词。

接入方式：

1. **原生插件**：OpenAI Realtime、Gemini Live、AWS Nova Sonic、NVIDIA PersonaPlex、Phonic、Grok Voice、Ultravox 等。
2. **自定义插件**：实现 `livekit.agents.llm.RealtimeModel` / `RealtimeSession`。本项目安装的 1.5.2 已暴露这两个基类；Qwen/GLM/豆包目前没有官方 LiveKit 插件，需要自己适配或走旁路 WebSocket。
3. **混合模式**：实时模型设置 `modality=["text"]`，再外接 TTS。模型负责听/理解，TTS 负责精确发音。

对本项目：

- `LangGraphLLM` 实现的是 `llm.LLM`，不是 `RealtimeModel`。直接把 `AgentSession(llm=...)` 换成实时模型，LangGraph 就失去主链路控制。
- 更合理：**实时模型不占 `llm` 槽位**，作为独立感知服务跑旁路；主链路仍是 `STT → LangGraphLLM → TTS`。
- 若试点混合模式，新增 `RealtimePerceptionClient`，输出结构化 `TurnPerception`；`LangGraphLLM` 在图调用前合并；必须指定唯一权威转写。

---

## 4. 面试评测的多模态信号

### 4.1 音频（最值得先做）

可计算特征：

- 语速（字/秒）；
- 停顿比例、平均停顿时长；
- 基频/音高变化；
- 能量/音量；
- 填充词比例（嗯/啊/那个）；
- 回答时长。

工具：

- **Parselmouth**：Python 调 Praat，算 pitch/intensity/spectrogram；
- VAD/强制对齐：得到停顿边界；
- 简单 DSP：能量、过零率。

价值：对“表达流畅度、紧张度、条理性”的反馈比纯文本可靠，成本低。

边界：不要把音高/语速直接等同于“自信/能力”；口音、语速偏好、残障都会带来偏差。

### 4.2 视频（价值高、风险也高）

可计算特征：

- 面部动作单元（AU）；
- 眨眼频率；
- 头部姿态；
- 视线方向；
- 微笑强度；
- 坐姿稳定性。

工具：

- **OpenFace 2.2**：AU、gaze、head pose，支持实时摄像头；
- **MediaPipe Face Landmarker**：478 个 3D 关键点、52 个 blendshape，浏览器 `LIVE_STREAM`。

研究证据：

- HireNet 在 7,000+ 真实异步视频面试上用文本/音频/视频三模态预测可录用性；
- 但 HireNet 也发现早/晚期多模态融合并未稳定超过单模态——“堆模态”不等于更好；
- 2026 年的 E-AVI、PhoenixNest-Video、PolyInterview 强调“时间戳多模态证据 + 可解释评分”，而不是端到端黑盒。

结论：视频可以做**辅助反馈**，不适合做自动淘汰；默认关闭。

---

## 5. 三种迁移架构

### 方案 A：级联主链路 + 感知旁路（推荐先做）

```text
麦克风 ──► LiveKit ──► STT ──► LangGraph ──► TTS ──► 考官语音
                 │
                 └──► 音频特征（Parselmouth/VAD）
浏览器摄像头 ──► MediaPipe（本地）──► 派生特征 ──► 数据通道
                                                   │
                                   报告节点合并「表达维度」
```

改动：

- 新增 `perception/` 模块；
- state 增加 `delivery_metrics`；
- `report` 事件增加表达维度；
- 前端可选采集视频特征（默认关闭）。

优点：不碰状态机、成本低、可解释、隐私友好。
缺点：LLM 仍听不到语气，追问不因犹豫而改变。
适合：第一版升级，1–2 周。

### 方案 B：实时感知 + LangGraph 决策（中期）

```text
用户音频 ──► 实时模型（Qwen/GLM/豆包/Gemini）──► TurnPerception(JSON)
     │                                              │
     └──► STT ──► LangGraph ◄──────────────────────┘
                     │
                     └──► TTS（精确台词）
```

改动：

- 新增 `RealtimePerceptionClient`；
- `LangGraphLLM` 在图调用前合并感知结果；
- 报告提示词加入副语言摘要；
- 处理双路转写不一致与延迟。

优点：保留逐字可控 + 题量预算，同时拿到情绪/犹豫/完整度。
缺点：两路成本、同步复杂、供应商合规要单独评估。
适合：A 稳定后，以 feature flag 试点。

### 方案 C：全语音到语音，LangGraph 退化为工具层

```text
用户音频 ──► RealtimeModel（听+说+回合）
                   │ function call
                   ▼
            LangGraph tools：next_question / record_answer / finalize_report
```

改动：

- `AgentSession(llm=RealtimeModel)`；
- 把图节点暴露为工具；
- 用 guardrail 约束题量与阶段；
- 成绩单仍由 LangGraph 生成。

优点：延迟最低、插话/附和自然、能利用副语言。
缺点：不能保证固定台词；回合检测交给模型；工具调用失败破坏状态；评测/复现困难；全双工模型可能限制独立 TTS。
适合：单独的“自由对话练习”模式，不作为高风险结构化面试默认引擎。

---

## 6. 推荐路线

| 阶段 | 时间 | 做什么 | 验收 |
| --- | --- | --- | --- |
| 1 | 1–2 周 | 方案 A：音频韵律 + 报告表达维度 | 用户反馈更有用；与人工评分的 MAE 下降 |
| 2 | 2–4 周 | 方案 B 离线评估：同一批录音横评 Qwen/GLM/豆包/Step-Audio/Gemini | 延迟、成本、中文质量、稳定性有数据 |
| 3 | 视结果 | 自由练习模式试点方案 C | 不影响结构化面试默认链路 |
| 贯穿 | — | 合规、公平性、隐私 | 上线前完成 |

### 阶段 1 的最小实现

1. 用离线录音跑 Parselmouth，得到每轮 `DeliveryMetrics`；
2. 在 `interview_graph` 的 state 加 `delivery_metrics`；
3. 报告提示词加一段“表达特征”，只影响表达逻辑维度；
4. 前端不采集视频，先验证音频特征是否真的提升反馈质量；
5. 如果提升不明显，停止，不进入阶段 2。

---

## 7. 评测设计

### 数据

- 同一批录音 ≥ 30 条，覆盖不同口音/语速/性别/专业；
- 人工评分：专业知识、表达逻辑、综合素质；
- 四组结果：
  - 纯文本（当前）；
  - 文本 + 音频副语言（A）；
  - 文本 + 实时感知（B）；
  - 纯实时模型（C，若试点）。

### 指标

| 指标 | 含义 |
| --- | --- |
| MAE | 与人工总分的平均绝对误差 |
| Spearman ρ | 排序一致性 |
| 分维度 MAE | 哪个维度受益 |
| 稳定性 | 同一录音重复跑的方差 |
| 公平性 | 按性别/口音/地区分组的误差差异 |
| 延迟 | 首 token / 首音频帧 / 整轮 |
| 成本 | 每分钟音频的模型+传输成本 |

### 判定

- 副语言对“表达逻辑”应有明显提升；
- 对“专业知识”提升有限是正常的；
- 视觉特征收益不确定，且公平性风险最高；
- 如果 A 的提升不显著，不要进入 B/C。

---

## 8. 合规与公平性

| 法规/风险 | 要求 | 影响 |
| --- | --- | --- |
| 纽约 Local Law 144 | 招聘/选拔 AEDT 需偏见审计、公开摘要、提前 10 个工作日通知 | 真实招聘/升学场景需审计与告知 |
| 欧盟 AI Act | 招聘/选拔/候选人评估属高风险；工作/教育场景情绪推断原则上禁止 | 不做“情绪识别”产品承诺 |
| 伊利诺伊 AI Video Interview Act | 告知、同意、人工复核、删除请求 | 视频默认关闭，提供删除 |
| 公平性研究 | 人类视频面试评分本身有视觉偏差 | 不把视觉分数当“客观” |

工程底线：

- 原始音视频最小化采集、最短保留；
- 派生特征可解释、可删除；
- 不推断受保护属性；
- 不自动淘汰；
- 人工复核与申诉；
- 对音频/视频特征做分组公平性评估；
- 明确告知考生“AI 如何评分、哪些数据被使用”。

---

## 9. 未核实 / 需复核

以下内容来自公开资料整理，落地前需以官方文档/控制台复核：

- GLM-Realtime 的实际中文质量、延迟、价格、会话时长上限；
- GPT Realtime 2.1、Gemini Live 3.8 在中文面试场景的质量；
- Qwen3.8-Omni-Flash-Realtime 的完整计费与并发限制；
- 豆包 O2.0/SC2.0 的函数调用能力；
- 伊利诺伊 AI Video Interview Act 原文（本次抓取失败）；
- MediaPipe “端上处理”的隐私属性（文档未明确承诺）；
- E-AVI / PhoenixNest-Video / PolyInterview 的准确率均为作者声明，未见独立复现；
- 各模型价格随地区和合同变化，表中不写具体数字。

---

## 10. 结论

- **现在不要换主链路**。本项目的价值在“可控的结构化面试”，全双工模型会削弱这一点。
- **先做方案 A**：音频韵律 + 报告表达维度，成本最低、风险最小、可解释。
- **用数据决定是否进入 B**：如果副语言对评分没有显著提升，停止。
- **C 只用于自由练习**，不要用于高风险结构化面试。
- **合规与公平性先于功能上线**，视频默认关闭。

---

## Sources

- [Qwen3-Omni (GitHub)](https://github.com/QwenLM/Qwen3-Omni)
- [Qwen2.5-Omni (GitHub)](https://github.com/QwenLM/Qwen2.5-Omni)
- [Qwen-Omni-Realtime（阿里云）](https://help.aliyun.com/en/model-studio/realtime)
- [vLLM-Omni Realtime Duplex API](https://docs.vllm.ai/projects/vllm-omni/en/latest/serving/realtime_openai_duplex_api/)
- [Step-Audio 2 (GitHub)](https://github.com/stepfun-ai/Step-Audio2)
- [GLM-4-Voice (GitHub)](https://github.com/zai-org/GLM-4-Voice)
- [GLM-Realtime 文档](https://docs.bigmodel.cn/cn/asyncapi/realtime.md)
- [MiniCPM-o 2.6 模型卡](https://huggingface.co/openbmb/MiniCPM-o-2_6)
- [MiniCPM-o (GitHub)](https://github.com/OpenBMB/MiniCPM-o)
- [LiveKit Realtime Models](https://docs.livekit.io/agents/models/realtime/)
- [OpenAI Realtime 指南](https://developers.openai.com/api/docs/guides/realtime)
- [OpenAI API 定价](https://developers.openai.com/api/docs/pricing)
- [Gemini Live API](https://ai.google.dev/gemini-api/docs/live)
- [Gemini API 定价](https://ai.google.dev/gemini-api/docs/pricing)
- [豆包端到端实时语音文档](https://docs.volcengine.com/docs/6561/1594356)
- [NYC Local Law 144 / AEDT](https://www.nyc.gov/site/dca/about/automated-employment-decision-tools.page)
- [EU AI Act 全文](https://eur-lex.europa.eu/eli/reg/2024/1689/oj)
- [EU AI Act Article 5](https://artificialintelligenceact.eu/article/5/)
- [EU AI Act Annex III](https://artificialintelligenceact.eu/annex/3/)
- [Illinois AI Video Interview Act](https://www.ilga.gov/legislation/ilcs/ilcs3.asp?ActID=4015)
- [MediaPipe Face Landmarker](https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker)
- [OpenFace](https://github.com/TadasBaltrusaitis/OpenFace)
- [Parselmouth](https://github.com/YannickJadoul/Parselmouth)
- [HireNet (arXiv:1907.11062)](https://arxiv.org/abs/1907.11062)
- [Agrawal et al. (arXiv:2006.07909)](https://arxiv.org/abs/2006.07909)
- [Leong et al. (arXiv:1911.13248)](https://arxiv.org/abs/1911.13248)
- [Booth et al. (arXiv:2305.02629)](https://arxiv.org/abs/2305.02629)
- [E-AVI (arXiv:2609.20001)](https://arxiv.org/abs/2609.20001)
- [PhoenixNest-Video (arXiv:2609.02231)](https://arxiv.org/abs/2609.02231)
- [PolyInterview (arXiv:2607.10310)](https://arxiv.org/abs/2607.10310)
- [STT×LLM×TTS 组合评测 (arXiv:2507.16835)](https://arxiv.org/abs/2507.16835)
