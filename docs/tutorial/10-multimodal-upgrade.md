# 第 10 章：多模态升级

当前系统只“读文字”：STT 把声音变成文本后，语气、停顿、紧张、表情全部丢失。一个考生用平铺直叙的声音说“我很热爱科研”，和用颤抖的声音说同一句话，系统给出的评价完全一样。

这一章讲三件事：

1. 为什么不能简单把 STT→LLM→TTS 换成一个语音大模型；
2. 三种可行的多模态架构；
3. 如何分阶段落地，以及合规红线。

> 更完整的候选模型对比、价格/延迟/许可证信息见 [../reports/multimodal-alternatives.md](../reports/multimodal-alternatives.md)。本章偏教学与实现。

---

## 10.1 当前链路的三个盲区

```text
麦克风 → VAD → STT → 文本 → LangGraph → 文本 → TTS → 扬声器
                      ▲
                      └── 只保留词，不保留“怎么说”
```

| 盲区 | 丢失的信息 | 对面试评价的影响 |
| --- | --- | --- |
| 副语言 | 语速、停顿、音高、音量、填充词 | 无法判断流畅度、紧张度、犹豫 |
| 视觉 | 眼神、表情、姿态、点头 | 无法判断投入度、自信（且争议大） |
| 交互 | 抢话、沉默、打断 | 只能靠 VAD 的固定阈值，无法自适应 |

### 为什么不直接换成语音到语音

语音到语音模型（Qwen-Omni、GPT Realtime、Gemini Live、GLM-Realtime、豆包等）能同时听语气、直接说话，延迟低、插话自然。但本项目有一个硬约束：

> 每阶段固定题量、最多 N 次追问、结束时输出固定结构的成绩单。

这要求**回合可控、台词可控、状态可复现**。而全双工实时模型通常：

- 自己管理回合检测，外部很难强制“现在必须换阶段”；
- 直接输出语音，不保证逐字念出你给的台词；
- 不支持外接独立 TTS（LiveKit 文档明确提到全双工模型的限制）；
- 工具调用失败/延迟会直接破坏状态机。

所以：**全语音到语音适合“自由对话练习”，不适合作为高风险结构化面试的默认引擎。**

---

## 10.2 三种架构

### 方案 A：级联主链路 + 感知旁路（推荐先做）

保留现有 STT→LangGraph→TTS，在旁边加一条“感知”支路：

```text
麦克风 ──► LiveKit ──► STT ──► LangGraph ──► TTS ──► 考官语音
                 │
                 └──► 音频特征（语速/停顿/音高/能量）
                              │
浏览器摄像头 ──► 本地 MediaPipe ──► 派生特征（眨眼/视线/姿态）
                              │
                              ▼
                    报告节点合并「表达维度」
```

**不改状态机**。感知结果只进报告，不进“是否追问”的决策。

优点：

- 成本低：音频特征用 Parselmouth/Praat 或简单 DSP；视频用 MediaPipe 本地算；
- 可解释：每个分数都能对应到“停顿比例 18%”“语速 4.2 字/秒”；
- 隐私友好：原始音视频不出浏览器/本机，只上传派生特征。

缺点：

- LLM 仍然“听不到”语气，追问不会因为犹豫而改变；
- 需要新增数据结构和报告维度。

适合：第一版升级，1–2 周可做。

### 方案 B：实时感知 + LangGraph 决策（中期）

让一个实时模型只负责“听”和“理解”，输出结构化感知；LangGraph 仍然决定下一句；TTS 仍然念精确台词。

```text
用户音频 ──► 实时模型（Qwen-Omni / GLM-Realtime / Gemini Live）
     │                 │
     │                 ▼
     │          TurnPerception(JSON)
     │                 │
     └──► STT ──► LangGraph ◄──┘
                       │
                       └──► TTS（逐字可控）
```

优点：

- 保留题量/阶段/台词控制；
- 拿到情绪、犹豫、回答完整度等副语言信号；
- 可以让追问更自然（例如“你刚才停顿了一下，是哪里不确定吗？”）。

缺点：

- 两路模型成本；
- 两路转写可能不一致，必须指定“唯一权威转写”；
- 同步与延迟复杂；
- 供应商隐私/合规要单独评估。

适合：A 稳定后，以 feature flag 试点。

### 方案 C：全语音到语音，LangGraph 退化为工具层

```text
用户音频 ──► RealtimeModel（听+说+回合）
                   │ function call
                   ▼
            LangGraph tools:
              next_question()
              record_answer()
              finalize_report()
```

优点：延迟最低、插话自然、能利用副语言。

缺点：

- 不能保证固定台词；
- 回合检测交给模型，题量/阶段约束变弱；
- 工具调用失败会破坏状态；
- 评测与复现困难；
- 全双工模型可能不允许外接独立 TTS。

适合：单独的“自由对话练习”模式，不要作为结构化复试的默认。

---

## 10.3 方案 A 的实现草图

### 10.3.1 新增 `perception/` 模块

```text
perception/
├── __init__.py
├── audio_features.py     # 从音频帧算语速/停顿/音高/能量
├── video_features.py     # 浏览器端派生特征的上传/校验（可选）
└── schemas.py            # TurnPerception / DeliveryMetrics
```

### 10.3.2 数据结构

```python
@dataclass
class DeliveryMetrics:
    speech_rate_cps: float          # 字/秒
    pause_ratio: float              # 停顿时间 / 总说话时间
    mean_pause_ms: float
    pitch_variability: float        # 音高标准差 / 均值
    filler_ratio: float             # 嗯/啊/那个 / 总字数
    answer_duration_ms: int
```

```python
@dataclass
class TurnPerception:
    transcript: str
    delivery: DeliveryMetrics
    affect_labels: list[str]        # 可选：犹豫/紧张/平静（需谨慎）
    confidence: float               # 感知置信度
```

### 10.3.3 音频特征从哪来

LiveKit Agents 的 STT 已经消费了音频；要同时算特征，有三条路：

1. **在 agent 侧旁路订阅同一路音频**：用 LiveKit `rtc.AudioStream` 读帧，送 `audio_features`。不依赖 STT 实现。
2. **在浏览器端用 Web Audio API**：算能量/过零率/简单音高，通过 data channel 上传。适合轻量特征，隐私最好。
3. **离线后处理**：录下音频，面试结束后用 Parselmouth/Praat 批量算。最简单，但不是实时。

教学建议：先做 3（离线、可验证），再做 1（实时）。

### 10.3.4 把特征接进图

**不要**把 `DeliveryMetrics` 塞进 `user_query` 里让 LLM 自己解析。新增一个 state 字段：

```python
class InterviewState(TypedDict):
    ...
    delivery_metrics: dict[str, Any]
```

适配器在调用图之前合并：

```python
input_state = {
    "user_query": self._query,
    "interview_mode": graph_llm.interview_mode,
    "delivery_metrics": graph_llm.perception.latest(),  # 可选
}
```

报告节点在提示词里加一段：

```text
以下是考生的表达特征（仅供参考，不要据此推断人格或受保护属性）：
- 语速：4.2 字/秒
- 停顿比例：18%
- 平均停顿：620ms
- 填充词比例：3%
请把这些特征写进“表达逻辑”维度的评语，但不要给出医学/心理诊断。
```

注意：**只进报告，不进追问决策**。否则一次误判的“紧张”会导致不公平的追问。

### 10.3.5 视频特征的边界

浏览器端 MediaPipe Face Landmarker 可以给 478 个关键点 / 52 个 blendshape。可以派生：

- 眨眼频率；
- 视线是否大致朝向摄像头；
- 头部姿态（点头/摇头）；
- 微笑强度；
- 坐姿稳定性。

但必须：

- **默认关闭**，明确告知并取得同意；
- 原始视频不出浏览器，只上传聚合特征；
- 不推断种族、性别、年龄、健康、情绪等敏感属性；
- 不把视觉分数用于自动淘汰；
- 提供人工复核与申诉。

---

## 10.4 方案 B 的实现草图

### 10.4.1 LiveKit 的实时模型接口

LiveKit Agents 1.5.x 暴露了 `RealtimeModel` / `RealtimeSession` 基类。一个最小适配器需要实现：

```python
class MyRealtimeModel(RealtimeModel):
    async def session(self) -> RealtimeSession: ...
```

`RealtimeSession` 通常需要：

- `push_audio(frame)`：把用户音频送进去；
- `push_video(frame)`：可选；
- `generate_reply()`：请求回复；
- `interrupt()`：打断；
- 事件：`generation_created`、`audio`、`text`、`tool_call` 等。

不同供应商协议差异很大（WebSocket JSON、二进制、OpenAI Realtime 事件），不建议自己从零写；优先找官方 LiveKit 插件。

### 10.4.2 作为“旁路感知”而不是主 LLM

即使有实时模型插件，也不要把 `AgentSession(llm=...)` 换成它。保留 `LangGraphLLM` 作为主 LLM：

```python
session = voice.AgentSession(
    stt=...,
    llm=LangGraphLLM(...),          # 仍然是主链路
    tts=...,
    ...
)
```

另起一个 `RealtimePerceptionClient`，订阅同一路用户音频，输出：

```json
{
  "transcript": "...",
  "delivery": {...},
  "affect": ["hesitant"],
  "confidence": 0.72
}
```

`LangGraphLLM` 在图调用前把它合并进 state。

### 10.4.3 双路转写冲突

实时模型和 STT 都会给文本。必须选一个“权威转写”：

- 推荐：**STT 是权威**，实时模型只提供副语言标签；
- 或者：实时模型是权威，STT 只做兜底；
- 不要两路都塞给 LLM，模型会困惑。

---

## 10.5 评测：怎么知道多模态真的有用

不要只看“感觉更准”。做一个小型对照实验：

### 数据

- 同一批录音（建议 ≥ 30 条，覆盖不同口音/语速/性别）；
- 人工评分：专业知识、表达逻辑、综合素质；
- 文本-only 评分：当前系统；
- 文本+副语言评分：方案 A；
- 实时感知评分：方案 B（可选）。

### 指标

| 指标 | 含义 |
| --- | --- |
| MAE | 与人工总分的平均绝对误差 |
| Spearman ρ | 排序一致性 |
| 分维度 MAE | 看是哪个维度受益 |
| 稳定性 | 同一录音重复跑的方差 |
| 公平性 | 按性别/口音/地区分组的误差差异 |

### 预期

- 副语言特征对“表达逻辑/流畅度”提升明显；
- 对“专业知识”提升有限；
- 视觉特征收益不确定，且公平性风险最高。

如果提升不显著，宁可不做——多模态会增加成本、延迟和合规负担。

---

## 10.6 合规与公平性

这不是“以后再说”的事，而是设计输入。

| 法规/风险 | 要求 | 对本项目的影响 |
| --- | --- | --- |
| 纽约 Local Law 144 | 招聘/选拔 AEDT 需偏见审计、公开摘要、提前通知 | 如果用于真实招聘/升学，需审计与告知 |
| 欧盟 AI Act | 招聘/选拔/候选人评估属高风险；工作/教育场景情绪推断原则上禁止 | 不要做“情绪识别”产品承诺 |
| 伊利诺伊 AI Video Interview Act | 告知、同意、人工复核、删除请求 | 视频默认关闭，提供删除 |
| 公平性研究 | 人类视频面试评分本身有视觉偏差 | 不要把视觉分数当“客观” |

工程底线：

- 原始音视频最小化采集、最短保留；
- 派生特征可解释、可删除；
- 不推断受保护属性；
- 不自动淘汰；
- 人工复核与申诉通道；
- 对音频/视频特征做分组公平性评估。

---

## 10.7 分阶段路线

| 阶段 | 时间 | 做什么 | 验收 |
| --- | --- | --- | --- |
| 1 | 1–2 周 | 方案 A：离线音频特征 + 报告“表达”维度 | 用户反馈更有用；人工对照 MAE 下降 |
| 2 | 2–4 周 | 方案 B 离线评估：Qwen-Omni/GLM/豆包/Step-Audio 横评 | 延迟、成本、中文质量、稳定性有数据 |
| 3 | 视结果 | 自由练习模式试点方案 C | 不影响结构化面试默认链路 |
| 贯穿 | — | 合规、公平性、隐私 | 上线前完成 |

---

## 10.8 检查点

1. 为什么全双工语音模型不适合作为结构化面试的默认 LLM？
2. 方案 A 中，感知特征为什么“只进报告，不进追问决策”？
3. 双路转写冲突时，应该怎么选权威转写？
4. 如果副语言评分对“专业知识”没有提升，还值得做吗？
5. 视频特征至少需要哪些合规措施？

---

## 参考

- [LiveKit Realtime Models](https://docs.livekit.io/agents/models/realtime/)
- [Qwen3-Omni](https://github.com/QwenLM/Qwen3-Omni)
- [Qwen-Omni-Realtime（阿里云）](https://help.aliyun.com/en/model-studio/realtime)
- [Step-Audio 2](https://github.com/stepfun-ai/Step-Audio2)
- [GLM-4-Voice](https://github.com/zai-org/GLM-4-Voice)
- [GLM-Realtime](https://docs.bigmodel.cn/cn/asyncapi/realtime.md)
- [MiniCPM-o](https://github.com/OpenBMB/MiniCPM-o)
- [OpenAI Realtime](https://developers.openai.com/api/docs/guides/realtime)
- [Gemini Live API](https://ai.google.dev/gemini-api/docs/live)
- [豆包实时语音](https://docs.volcengine.com/docs/6561/1594356)
- [MediaPipe Face Landmarker](https://developers.google.com/edge/mediapipe/solutions/vision/face_landmarker)
- [OpenFace](https://github.com/TadasBaltrusaitis/OpenFace)
- [Parselmouth](https://github.com/YannickJadoul/Parselmouth)
- [HireNet (arXiv:1907.11062)](https://arxiv.org/abs/1907.11062)
- [NYC Local Law 144](https://www.nyc.gov/site/dca/about/automated-employment-decision-tools.page)
- [EU AI Act](https://eur-lex.europa.eu/eli/reg/2024/1689/oj)
