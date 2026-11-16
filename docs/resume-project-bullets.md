# 简历可用：项目经历文案（与当前实现一致）

**考研复试 AI 模拟面试系统｜独立开发**  
`LiveKit Agents` · `LangGraph` · `Chroma` · `Python` · `SQLite`

- **双文档匹配 RAG：** 以考生薄弱点档案 / 加强方向为查询文档，以预置专业课 Markdown 讲义为知识库文档；SiliconFlow 兼容 embedding + Chroma 向量检索，并按 topic 元数据过滤，将命中段落注入专业课出题提示（关闭或失败时自动降级）。
- **实时语音面试：** 浏览器接入 LiveKit，STT → LangGraph 四阶段状态机 → 流式 TTS；支持 quick / real 题量与追问。
- **结构化长期记忆：** 场次成绩、知识点掌握度与已问历史写入 SQLite；下一场按薄弱点继续练，可与 RAG 检索联动出题。
- **可演示匹配接口：** 提供 `POST /api/rag/match`，无需跑完整语音链路即可展示「文档 A 匹配文档 B」；配套离线 FakeEmbedding 测试。

> 边界：综合素质题与成绩单不走 RAG；不宣称「已测得提分」等无证据指标。
